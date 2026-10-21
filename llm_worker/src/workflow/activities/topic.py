"""The steps of an answer about a topic: route, gather, summarize.

Stream work (README, rule 12), blocking, on the worker's thread pool.

- route asks the chat model whether a message is about a fact or a topic,
  and which words name the topic.
- gather turns those words into stored terms (rag.topics.lemmas) and finds
  the chunks: a term in the keywords of chunks (chunk_mentions_current) is
  selective and gives its chunks; a term that is only a theme of whole
  documents (document_themes_current, in more than MAX_SHARE of their
  chunks, so never a keyword of one chunk) narrows the documents. Only
  themes of whole documents: the topic is too broad to gather. Nothing
  found, or fewer chunks than min_chunks: the question is answered as a fact.
- summarize reads the chunks a group at a time and takes notes (map);
  condenses the notes a batch at a time while they are too many for one
  call; groups them into themes; writes a paragraph per theme from its notes
  (reduce). A heartbeat after every call.

Payloads between the steps are keys of chunks, not their texts (principle 1).
"""

import logging

from temporalio import activity

from rag.mentions import MAX_SHARE
from rag.topics import (
    CONDENSE_PROMPT,
    MAP_PROMPT,
    MAP_SCHEMA,
    MAX_NOTES_CHARS,
    PARAGRAPH_PROMPT,
    PARAGRAPH_SCHEMA,
    ROUTE_PROMPT,
    ROUTE_SCHEMA,
    THEMES_PROMPT,
    THEMES_SCHEMA,
    assemble,
    cites,
    groups,
    lemmas,
    note_batches,
    notes_from,
    render_notes,
    themes_from,
)
from workflow.activities.index import connect
from workflow.contracts import Broad, Gathered, GatherInput, Generated, Route, RouteInput, Source, SummarizeInput
from workflow.llm import LLM

log = logging.getLogger(__name__)


class TopicSteps:
    def __init__(self, connect, llm: LLM):
        # A ClickHouse client per call: activities run on several threads at once.
        self.connect, self.llm = connect, llm

    @classmethod
    def from_env(cls) -> "TopicSteps":
        return cls(connect, LLM.from_env())

    @activity.defn(name="route")
    def route(self, input: RouteInput) -> Route:
        answer = self.llm.chat_json(
            [{"role": "system", "content": ROUTE_PROMPT}, {"role": "user", "content": input.question}], ROUTE_SCHEMA
        )
        terms = [t.strip() for t in answer.get("terms") or [] if isinstance(t, str) and t.strip()][:3]
        kind = "topic" if answer.get("kind") == "topic" and terms else "fact"
        return Route(kind=kind, terms=terms)

    @activity.defn(name="gather")
    def gather(self, input: GatherInput) -> Gathered:
        stored = list(dict.fromkeys(s for s in map(lemmas, input.terms) if s))
        if not stored:
            return Gathered(mode="none")
        client = self.connect()

        chunks: dict[str, set[tuple[str, str, int]]] = {}
        for term, file_id, index_run_id, chunk_id in client.query(
            "SELECT term, file_id, index_run_id, chunk_id FROM chunk_mentions_current WHERE term IN {t:Array(String)}",
            parameters={"t": stored},
        ).result_rows:
            chunks.setdefault(term, set()).add((file_id, index_run_id, chunk_id))
        broad = [
            Broad(term=t, file_id=f, name=n, chunks=c)
            for t, f, n, c in client.query(
                "SELECT t.term, t.file_id, f.name, t.chunks FROM document_themes_current AS t "
                "INNER JOIN (SELECT file_id, name FROM files FINAL) AS f ON f.file_id = t.file_id "
                "WHERE t.term IN {t:Array(String)} AND t.share >= {s:Float32} ORDER BY t.chunks DESC",
                parameters={"t": stored, "s": MAX_SHARE},
            ).result_rows
        ]
        broad_terms = {b.term for b in broad} - set(chunks)
        unknown = set(stored) - set(chunks) - broad_terms

        if not chunks:
            # Every term a theme of whole documents: too broad. Otherwise a
            # word that names nothing stored: answer the question as a fact.
            return Gathered(mode="too_broad", terms=stored, broad=broad) if broad and not unknown else Gathered(
                mode="none", terms=stored)

        sets = list(chunks.values())
        found = set.intersection(*sets) or set.union(*sets)
        if broad_terms:
            files = {b.file_id for b in broad if b.term in broad_terms}
            found = {c for c in found if c[0] in files} or found
        if len(found) < input.min_chunks:
            # Too few for a topic: the chunks closest to the question say more.
            log.info("topic %s: %d chunks found, answered as a fact", stored, len(found))
            return Gathered(mode="none", terms=stored, found=len(found))
        keys = sorted(found)
        if len(keys) > input.max_chunks:
            keys = self.closest(client, input.question, keys, input.max_chunks)
        names = dict(client.query(
            "SELECT file_id, name FROM files FINAL WHERE file_id IN {f:Array(String)}",
            parameters={"f": sorted({k[0] for k in keys})},
        ).result_rows)
        log.info("topic %s: %d chunks found, %d taken", stored, len(found), len(keys))
        return Gathered(
            mode="chunks", terms=stored, found=len(found), broad=broad,
            sources=[Source(file_id=f, index_run_id=r, chunk_id=c, name=names.get(f, "")) for f, r, c in keys],
        )

    def closest(self, client, question: str, keys: list[tuple[str, str, int]], n: int) -> list[tuple[str, str, int]]:
        """The n keys closest to the question, in the order of the text."""
        [vector] = self.llm.embed([question])
        rows = client.query(
            "SELECT file_id, run_id, chunk_id FROM doc_chunks WHERE (run_id, chunk_id) IN {k:Array(Tuple(String, UInt32))} "
            "AND embed_model = {m:String} ORDER BY cosineDistance(embedding, {q:Array(Float32)}) LIMIT {n:UInt32}",
            parameters={"k": [(r, c) for _, r, c in keys], "m": self.llm.embed_model, "q": vector, "n": n},
        ).result_rows
        return sorted((f, r, c) for f, r, c in rows)

    @activity.defn(name="summarize")
    def summarize(self, input: SummarizeInput) -> Generated:
        texts = dict(((r, c), t) for r, c, t in self.connect().query(
            "SELECT run_id, chunk_id, text FROM doc_chunks WHERE (run_id, chunk_id) IN {k:Array(Tuple(String, UInt32))}",
            parameters={"k": [(s.index_run_id, s.chunk_id) for s in input.sources]},
        ).result_rows)
        q, calls = input.question, 0

        def ask(prompt: str, schema: dict, given: str | None = None) -> dict:
            """One call; with given text (passages, notes), the prompt is the system message and the text the user's."""
            nonlocal calls
            messages = ([{"role": "system", "content": prompt}, {"role": "user", "content": given}]
                        if given is not None else [{"role": "user", "content": prompt}])
            answer = self.llm.chat_json(messages, schema)
            calls += 1
            activity.heartbeat(calls)
            return answer

        notes: list[tuple[int, str]] = []
        for group in groups(list(enumerate(input.sources, 1))):
            passages = "\n\n".join(f"[{i}] {s.name}\n{texts.get((s.index_run_id, s.chunk_id), '')}" for i, s in group)
            notes += notes_from(ask(MAP_PROMPT.format(question=q), MAP_SCHEMA, passages), {i for i, _ in group})
        if not notes:
            return Generated(text="", model=self.llm.chat_model)

        # The second level: too many notes for one call are condensed until they fit.
        while len(render_notes(notes, limit=None)) > MAX_NOTES_CHARS:
            condensed = []
            for batch in note_batches(notes, MAX_NOTES_CHARS // 2):
                answer = ask(CONDENSE_PROMPT, MAP_SCHEMA, render_notes(batch, limit=None))
                condensed += notes_from(answer, {p for p, _ in batch})
            if not condensed or len(render_notes(condensed, limit=None)) >= len(render_notes(notes, limit=None)):
                break  # no shorter: render_notes cuts at the limit instead
            notes = condensed

        passages = {p for p, _ in notes}
        themes = themes_from(ask(THEMES_PROMPT.format(question=q, notes=render_notes(notes)), THEMES_SCHEMA), passages)
        if not themes:
            themes = [(q, sorted(passages))]
        paragraphs = []
        for theme, mine in themes:
            text = render_notes([n for n in notes if n[0] in mine])
            for _ in range(2):  # one more try for a paragraph that cites nothing
                paragraph = str(ask(PARAGRAPH_PROMPT.format(theme=theme), PARAGRAPH_SCHEMA, text)
                                .get("paragraph") or "").strip()
                if cites(paragraph, mine):
                    paragraphs.append((theme, paragraph))
                    break
        log.info("summarized %d chunks: %d notes, %d themes, %d paragraphs, %d calls",
                 len(input.sources), len(notes), len(themes), len(paragraphs), calls)
        return Generated(text=assemble(paragraphs), model=self.llm.chat_model)
