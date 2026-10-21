"""The steps of an answer about a topic: route, gather, summarize.

Stream work (README, rule 12), blocking, on the worker's thread pool.

- route asks the chat model whether a message is about a fact, a topic, or
  the user's own story to interpret (answered as a fact), what it asks of
  the documents as a short query, and which words of the query name the
  topic.
- gather turns those words into stored terms (rag.topics.lemmas), takes
  with each the stored terms that name the same subject (the closest by
  vector in term_vectors_visible, picked by the chat model) and finds the
  chunks: a term in the keywords of chunks (chunk_mentions_visible), its
  own or a similar one's, is
  selective and gives its chunks; a term that is only a theme of whole
  documents (document_themes_visible, in more than MAX_SHARE of their
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
    KINDS,
    MAP_PROMPT,
    MAP_SCHEMA,
    MAX_NOTES_CHARS,
    PARAGRAPH_PROMPT,
    PARAGRAPH_SCHEMA,
    ROUTE_PROMPT,
    ROUTE_SCHEMA,
    SIMILAR_CANDIDATES,
    SIMILAR_MAX_DISTANCE,
    SIMILAR_PROMPT,
    SIMILAR_SCHEMA,
    THEMES_PROMPT,
    THEMES_SCHEMA,
    assemble,
    cites,
    groups,
    lemmas,
    note_batches,
    notes_from,
    render_notes,
    similar_from,
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
        kind = answer.get("kind")
        if kind not in KINDS or (kind == "topic" and not terms):
            kind = "fact"
        query = answer.get("query")
        return Route(kind=kind, terms=terms, query=query.strip() if isinstance(query, str) else "")

    @activity.defn(name="gather")
    def gather(self, input: GatherInput) -> Gathered:
        stored = list(dict.fromkeys(s for s in map(lemmas, input.terms) if s))
        # Only the user's files, through the views of what they may see; with
        # no user, nothing (fail closed).
        if not stored or not input.user_id:
            return Gathered(mode="none")
        user = input.user_id
        client = self.connect()
        similar = {t: self.similar(client, t, input.question, user) for t in stored}

        # The chunks of a term: those its own or a similar term is a keyword of.
        variants = {t: [t, *similar[t]] for t in stored}
        by_variant: dict[str, set[tuple[str, str, int]]] = {}
        for term, file_id, index_run_id, chunk_id in client.query(
            "SELECT term, file_id, index_run_id, chunk_id FROM chunk_mentions_visible(user_id = {u:String}) "
            "WHERE term IN {t:Array(String)}",
            parameters={"t": sorted({v for vs in variants.values() for v in vs}), "u": user},
        ).result_rows:
            by_variant.setdefault(term, set()).add((file_id, index_run_id, chunk_id))
        chunks = {t: set().union(*(by_variant.get(v, set()) for v in vs)) for t, vs in variants.items()}
        chunks = {t: found for t, found in chunks.items() if found}
        similar = {t: s for t, s in similar.items() if s}
        broad = [
            Broad(term=t, file_id=f, name=n, chunks=c)
            for t, f, n, c in client.query(
                "SELECT t.term, t.file_id, f.name, t.chunks FROM document_themes_visible(user_id = {u:String}) AS t "
                "INNER JOIN (SELECT file_id, name FROM files_visible(user_id = {u:String})) AS f ON f.file_id = t.file_id "
                "WHERE t.term IN {t:Array(String)} AND t.share >= {s:Float32} ORDER BY t.chunks DESC",
                parameters={"t": stored, "s": MAX_SHARE, "u": user},
            ).result_rows
        ]
        broad_terms = {b.term for b in broad} - set(chunks)
        unknown = set(stored) - set(chunks) - broad_terms

        if not chunks:
            # Every term a theme of whole documents: too broad. Otherwise a
            # word that names nothing stored: answer the question as a fact.
            return Gathered(mode="too_broad", terms=stored, broad=broad, similar=similar) if broad and not unknown \
                else Gathered(mode="none", terms=stored, similar=similar)

        # The chunks of all the terms when they are enough for a topic, else
        # of any: сон and страх were keywords of 2 chunks together, of 23 apart,
        # and 2 sent the question to a fact.
        sets = list(chunks.values())
        both = set.intersection(*sets)
        found = both if len(both) >= input.min_chunks else set.union(*sets)
        if broad_terms:
            files = {b.file_id for b in broad if b.term in broad_terms}
            found = {c for c in found if c[0] in files} or found
        if len(found) < input.min_chunks:
            # Too few for a topic: the chunks closest to the question say more.
            log.info("topic %s (similar %s): %d chunks found, answered as a fact", stored, similar, len(found))
            return Gathered(mode="none", terms=stored, found=len(found), similar=similar)
        keys = sorted(found)
        if len(keys) > input.max_chunks:
            keys = self.closest(client, input.question, keys, input.max_chunks, user)
        names = dict(client.query(
            "SELECT file_id, name FROM files_visible(user_id = {u:String}) WHERE file_id IN {f:Array(String)}",
            parameters={"f": sorted({k[0] for k in keys}), "u": user},
        ).result_rows)
        log.info("topic %s (similar %s): %d chunks found, %d taken", stored, similar, len(found), len(keys))
        return Gathered(
            mode="chunks", terms=stored, found=len(found), broad=broad, similar=similar,
            sources=[Source(file_id=f, index_run_id=r, chunk_id=c, name=names.get(f, "")) for f, r, c in keys],
        )

    def similar(self, client, term: str, query: str, user: str) -> list[str]:
        """The stored terms that name what `term` does, other than itself: the
        closest by vector are the candidates, the chat model picks. Nothing
        stored close to it (or no vectors yet): no call to the model."""
        [vector] = self.llm.embed([term])
        candidates = [t for t, _ in client.query(
            "SELECT term, min(cosineDistance(embedding, {q:Array(Float32)})) AS distance "
            "FROM term_vectors_visible(user_id = {u:String}) "
            "WHERE embed_model = {m:String} AND term != {t:String} GROUP BY term HAVING distance <= {d:Float32} "
            "ORDER BY distance LIMIT {n:UInt32}",
            parameters={"q": vector, "m": self.llm.embed_model, "t": term, "d": SIMILAR_MAX_DISTANCE,
                        "n": SIMILAR_CANDIDATES, "u": user},
        ).result_rows]
        if not candidates:
            return []
        prompt = SIMILAR_PROMPT.format(term=term, query=query, candidates="\n".join(candidates))
        return similar_from(self.llm.chat_json([{"role": "user", "content": prompt}], SIMILAR_SCHEMA), candidates)

    def closest(self, client, question: str, keys: list[tuple[str, str, int]], n: int,
                user: str) -> list[tuple[str, str, int]]:
        """The n keys closest to the question, in the order of the text."""
        [vector] = self.llm.embed([question])
        rows = client.query(
            "SELECT file_id, run_id, chunk_id FROM doc_chunks_owned(user_id = {u:String}) "
            "WHERE (run_id, chunk_id) IN {k:Array(Tuple(String, UInt32))} "
            "AND embed_model = {m:String} ORDER BY cosineDistance(embedding, {q:Array(Float32)}) LIMIT {n:UInt32}",
            parameters={"k": [(r, c) for _, r, c in keys], "m": self.llm.embed_model, "q": vector, "n": n, "u": user},
        ).result_rows
        return sorted((f, r, c) for f, r, c in rows)

    @activity.defn(name="summarize")
    def summarize(self, input: SummarizeInput) -> Generated:
        # Texts of the user's files only: a key of someone else's chunk reads
        # nothing, and with no user nothing is read (fail closed).
        texts = dict(((r, c), t) for r, c, t in self.connect().query(
            "SELECT run_id, chunk_id, text FROM doc_chunks_owned(user_id = {u:String}) "
            "WHERE (run_id, chunk_id) IN {k:Array(Tuple(String, UInt32))}",
            parameters={"k": [(s.index_run_id, s.chunk_id) for s in input.sources], "u": input.user_id},
        ).result_rows) if input.user_id else {}
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

        # Themes are named after the short query: named after a long message,
        # they were its story ("Сон как процесс усвоения страха").
        query = input.query or q
        passages = {p for p, _ in notes}
        themes = themes_from(ask(THEMES_PROMPT.format(question=query, notes=render_notes(notes)), THEMES_SCHEMA),
                             passages)
        if not themes:
            themes = [(query, sorted(passages))]
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
