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
from dataclasses import replace

from temporalio import activity

from rag.mentions import MAX_SHARE
from rag.topics import (
    COMPARE,
    COMPARE_PROMPT,
    CONDENSE_PROMPT,
    KINDS,
    MAP_SCHEMA,
    MAX_NOTES_CHARS,
    PARAGRAPH_PROMPT,
    PARAGRAPH_SCHEMA,
    ROUTE_SCHEMA,
    SIMILAR_SCHEMA,
    THEMES_PROMPT,
    THEMES_SCHEMA,
    aspects_from,
    assemble,
    cites,
    groups,
    lemmas,
    map_prompt,
    names_authors,
    names_documents,
    note_batches,
    notes_from,
    render_notes,
    route_prompt,
    similar_from,
    similar_prompt,
    themes_from,
    where_in,
)
from rag.profiles import Profile
from workflow.activities.index import connect
from workflow.contracts import (
    Broad,
    Gathered,
    GatherInput,
    Generated,
    Route,
    RouteInput,
    Scope,
    Section,
    Side,
    Source,
    SummarizeInput,
)
from workflow.llm import LLM
from workflow.profiles import load

log = logging.getLogger(__name__)

# The chat's scope over the views of what the user may see: it narrows them
# and never widens them; no files named is every file (docs/search-scope-plan.md).
IN_SCOPE = "(empty({{f:Array(String)}}) OR has({{f:Array(String)}}, {column}))"


def sides_of(filters: Scope, groups: dict[str, str]) -> list[Side]:
    """What a message compares: the authors it names, two or more, else the
    groups, else the kinds; each side keeps the rest of the filters (the
    notes of Саша, the book of Мамардашвили). groups: id → name."""
    if len(filters.authors) >= 2:
        return [Side(label=a, scope=replace(filters, authors=[a])) for a in filters.authors]
    if len(filters.collections) >= 2:
        return [Side(label=groups.get(c, c), scope=replace(filters, collections=[c])) for c in filters.collections]
    if len(filters.kinds) >= 2:
        return [Side(label=k, scope=replace(filters, kinds=[k])) for k in filters.kinds]
    return []


class TopicSteps:
    def __init__(self, connect, llm: LLM):
        # A ClickHouse client per call: activities run on several threads at once.
        self.connect, self.llm = connect, llm

    @classmethod
    def from_env(cls) -> "TopicSteps":
        return cls(connect, LLM.from_env())

    @activity.defn(name="route")
    def route(self, input: RouteInput) -> Route:
        profile = load(input.profile)
        answer = self.llm.chat_json(
            [{"role": "system", "content": route_prompt(profile)}, {"role": "user", "content": input.question}],
            ROUTE_SCHEMA,
        )
        filters, groups = self.filters(input.question, input.user_id, profile)
        terms = [t.strip() for t in answer.get("terms") or [] if isinstance(t, str) and t.strip()][:3]
        # Words that say where to look (блог, книга, an author the message is
        # narrowed to) are no subject; a topic left with no terms and no
        # aspects is answered as a fact, below.
        terms = [t for t in terms
                 if not names_documents(t, profile.document_words) and not names_authors(t, filters.authors)]
        aspects = aspects_from(answer, input.question, profile.document_words)
        kind = answer.get("kind")
        if kind not in KINDS:
            kind = "fact"
        # Subjects listed to cover one by one make a topic: "что Саша думает о
        # жизни, смерти и любви?" went to interpret 3 times of 5, and no
        # dream gave aspects (15 of 15, 2026-10-22).
        if aspects:
            kind = "topic"
        if kind == "topic" and not terms and not aspects:
            kind = "fact"
        # Two sides named compare whatever the model read: the message of chat
        # 01a16252 went to interpret 4 times of 5 once the prompt had aspects.
        sides = sides_of(filters, groups)
        if len(sides) >= 2:
            kind = COMPARE
        query = answer.get("query")
        return Route(kind=kind, terms=terms, query=query.strip() if isinstance(query, str) else "",
                     profile=profile.name, profile_version=profile.version, filters=filters,
                     aspects=aspects, sides=sides if kind == COMPARE else [])

    def filters(self, question: str, user: str, profile: Profile) -> tuple[Scope, dict[str, str]]:
        """Where the message itself asks to look, among the user's own groups,
        authors and kinds (rag.topics.where_in), and the names of the groups
        by id; with no user, nowhere."""
        if not user:
            return Scope(), {}
        client = self.connect()
        rows = client.query(
            "SELECT authors, kind FROM documents_visible(user_id = {u:String})", parameters={"u": user}
        ).result_rows
        groups = dict(client.query(
            "SELECT name, collection_id FROM collections_visible(user_id = {u:String})", parameters={"u": user}
        ).result_rows)
        authors = sorted({a for names, _ in rows for a in names if a.strip()})
        kinds = sorted({k for _, k in rows if k})
        collections, authors, kinds = where_in(question, groups, authors, profile.kind_words, kinds)
        return Scope(collections=collections, authors=authors, kinds=kinds), {i: n for n, i in groups.items()}

    @activity.defn(name="gather")
    def gather(self, input: GatherInput) -> Gathered:
        stored = list(dict.fromkeys(s for s in map(lemmas, input.terms) if s))
        # Only the user's files, through the views of what they may see; with
        # no user, nothing (fail closed).
        if not input.user_id:
            return Gathered(mode="none")
        if not stored:
            return self.filled(self.connect(), input, set(), {}) if input.fill else Gathered(mode="none")
        user, profile = input.user_id, load(input.profile)
        client = self.connect()
        similar = {t: self.similar(client, t, input.question, user, profile, input.file_ids) for t in stored}

        # The chunks of a term: those its own or a similar term is a keyword of.
        variants = {t: [t, *similar[t]] for t in stored}
        by_variant: dict[str, set[tuple[str, str, int]]] = {}
        for term, file_id, index_run_id, chunk_id in client.query(
            "SELECT term, file_id, index_run_id, chunk_id FROM chunk_mentions_visible(user_id = {u:String}) "
            f"WHERE term IN {{t:Array(String)}} AND {IN_SCOPE.format(column='file_id')}",
            parameters={"t": sorted({v for vs in variants.values() for v in vs}), "u": user, "f": input.file_ids},
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
                f"WHERE t.term IN {{t:Array(String)}} AND t.share >= {{s:Float32}} AND {IN_SCOPE.format(column='t.file_id')} "
                "ORDER BY t.chunks DESC",
                parameters={"t": stored, "s": MAX_SHARE, "u": user, "f": input.file_ids},
            ).result_rows
        ]
        broad_terms = {b.term for b in broad} - set(chunks)
        unknown = set(stored) - set(chunks) - broad_terms

        if not chunks and input.fill:
            return self.filled(client, input, set(), similar, terms=stored)
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
        if input.fill:
            return self.filled(client, input, found, similar, terms=stored)
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

    def filled(self, client, input: GatherInput, found: set[tuple[str, str, int]], similar: dict[str, list[str]],
               terms: list[str] = ()) -> Gathered:
        """max_chunks of them: the keyword chunks found (the closest to the
        question when more), then the closest of the rest of the files. In
        640 chunks of Саша's notes любовь was a keyword of one."""
        keys = sorted(found)
        if len(keys) > input.max_chunks:
            keys = self.closest(client, input.question, keys, input.max_chunks, input.user_id)
        if len(keys) < input.max_chunks:
            keys = sorted({*keys, *self.nearest(client, input, keys, input.max_chunks - len(keys))})
        if not keys:
            return Gathered(mode="none", terms=list(terms), similar=similar)
        names = dict(client.query(
            "SELECT file_id, name FROM files_visible(user_id = {u:String}) WHERE file_id IN {f:Array(String)}",
            parameters={"f": sorted({k[0] for k in keys}), "u": input.user_id},
        ).result_rows)
        log.info("part %s (similar %s): %d keyword chunks, %d taken", terms, similar, len(found), len(keys))
        return Gathered(
            mode="chunks", terms=list(terms), found=len(found), similar=similar,
            sources=[Source(file_id=f, index_run_id=r, chunk_id=c, name=names.get(f, "")) for f, r, c in keys],
        )

    def nearest(self, client, input: GatherInput, taken: list[tuple[str, str, int]],
                n: int) -> list[tuple[str, str, int]]:
        """The n chunks of the files closest to the question, other than those
        taken, one text once (as retrieve)."""
        [vector] = self.llm.embed([input.question])
        return [tuple(row) for row in client.query(
            "SELECT file_id, run_id, chunk_id FROM doc_chunks_visible(user_id = {u:String}) "
            f"WHERE embed_model = {{m:String}} AND {IN_SCOPE.format(column='file_id')} "
            "AND (run_id, chunk_id) NOT IN {x:Array(Tuple(String, UInt32))} "
            "ORDER BY cosineDistance(embedding, {q:Array(Float32)}) LIMIT 1 BY cityHash64(text) LIMIT {n:UInt32}",
            parameters={"q": vector, "m": self.llm.embed_model, "n": n, "u": input.user_id, "f": input.file_ids,
                        "x": [(r, c) for _, r, c in taken]},
        ).result_rows]

    def similar(self, client, term: str, query: str, user: str, profile: Profile, file_ids: list[str]) -> list[str]:
        """The stored terms that name what `term` does, other than itself: the
        closest by vector (as near and as many as the profile allows) are the
        candidates, the chat model picks. Nothing stored close to it (or no
        vectors yet): no call to the model."""
        [vector] = self.llm.embed([term])
        candidates = [t for t, _ in client.query(
            "SELECT term, min(cosineDistance(embedding, {q:Array(Float32)})) AS distance "
            "FROM term_vectors_visible(user_id = {u:String}) "
            f"WHERE embed_model = {{m:String}} AND term != {{t:String}} AND {IN_SCOPE.format(column='file_id')} "
            "GROUP BY term HAVING distance <= {d:Float32} ORDER BY distance LIMIT {n:UInt32}",
            parameters={"q": vector, "m": self.llm.embed_model, "t": term, "d": profile.similar_max_distance,
                        "n": profile.similar_candidates, "u": user, "f": file_ids},
        ).result_rows]
        if not candidates:
            return []
        prompt = similar_prompt(profile, term, query, candidates)
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
        client = self.connect() if input.user_id else None
        texts = dict(((r, c), t) for r, c, t in client.query(
            "SELECT run_id, chunk_id, text FROM doc_chunks_owned(user_id = {u:String}) "
            "WHERE (run_id, chunk_id) IN {k:Array(Tuple(String, UInt32))}",
            parameters={"k": [(s.index_run_id, s.chunk_id) for s in input.sources], "u": input.user_id},
        ).result_rows) if client else {}
        authors = dict(client.query(
            "SELECT file_id, arrayStringConcat(authors, ', ') FROM documents_visible(user_id = {u:String}) "
            "WHERE file_id IN {f:Array(String)}",
            parameters={"f": sorted({s.file_id for s in input.sources}), "u": input.user_id},
        ).result_rows) if client else {}
        q, calls, profile = input.question, 0, load(input.profile)
        numbered = dict(enumerate(input.sources, 1))
        # Whose documents a passage is from: its side in a comparison, else
        # the authors of its document.
        who = {i: authors.get(s.file_id, "") for i, s in numbered.items()}
        who.update({i: section.side for section in input.sections if section.side for i in section.passages})

        def ask(prompt: str, schema: dict, given: str | None = None) -> dict:
            """One call; with given text (passages, notes), the prompt is the system message and the text the user's."""
            nonlocal calls
            messages = ([{"role": "system", "content": prompt}, {"role": "user", "content": given}]
                        if given is not None else [{"role": "user", "content": prompt}])
            answer = self.llm.chat_json(messages, schema)
            calls += 1
            activity.heartbeat(calls)
            return answer

        def take_notes(passages: list[int], question: str) -> list[tuple[int, str]]:
            """The map step over these passages, a group at a time."""
            notes = []
            for group in groups([p for p in passages if p in numbered]):
                text = "\n\n".join(
                    f"[{i}] {who[i] + ' · ' if who[i] else ''}{numbered[i].name}\n"
                    f"{texts.get((numbered[i].index_run_id, numbered[i].chunk_id), '')}" for i in group)
                notes += notes_from(ask(map_prompt(profile, question), MAP_SCHEMA, text), set(group))
            return notes

        def condensed(notes: list[tuple[int, str]]) -> list[tuple[int, str]]:
            """The second level: too many notes for one call are condensed until they fit."""
            while len(render_notes(notes, limit=None, who=who)) > MAX_NOTES_CHARS:
                shorter = []
                for batch in note_batches(notes, MAX_NOTES_CHARS // 2):
                    answer = ask(CONDENSE_PROMPT, MAP_SCHEMA, render_notes(batch, limit=None, who=who))
                    shorter += notes_from(answer, {p for p, _ in batch})
                if not shorter or len(render_notes(shorter, limit=None)) >= len(render_notes(notes, limit=None)):
                    break  # no shorter: render_notes cuts at the limit instead
                notes = shorter
            return notes

        # Themes are named after the short query: named after a long message,
        # they were its story ("Сон как процесс усвоения страха").
        query = input.query or q
        sides = list(dict.fromkeys(section.side for section in input.sections if section.side))
        by_aspect: dict[str, list[tuple[int, str]]] = {}
        if not input.sections:
            by_aspect[""] = take_notes(list(numbered), q)
        # A section's notes are taken on its aspect (on the query when it has
        # none): asked about the whole message, the map step read the name of
        # one side into the passages of the other.
        for section in input.sections:
            by_aspect.setdefault(section.aspect, []).extend(take_notes(section.passages, section.aspect or query))
        # The aspects asked for are the themes, in the order asked, each with
        # its own notes; with none, the themes are grouped from the notes, as
        # for one topic.
        parts = [(aspect, condensed(found)) for aspect, found in by_aspect.items() if aspect and found]
        missing = [aspect for aspect, found in by_aspect.items() if aspect and not found]
        notes = condensed(by_aspect.get("", []))
        if not parts and not notes:
            return Generated(text="", model=self.llm.chat_model)
        if not parts:
            passages = {p for p, _ in notes}
            themes = themes_from(ask(THEMES_PROMPT.format(question=query, notes=render_notes(notes, who=who)),
                                     THEMES_SCHEMA), passages) or [(query, sorted(passages))]
            parts = [(theme, [n for n in notes if n[0] in mine]) for theme, mine in themes]

        paragraphs = []
        for theme, mine in parts:
            text = render_notes(mine, who=who)
            prompt = (COMPARE_PROMPT.format(theme=theme, sides=", ".join(sides)) if len(sides) >= 2
                      else PARAGRAPH_PROMPT.format(theme=theme))
            for _ in range(2):  # one more try for a paragraph that cites nothing
                paragraph = str(ask(prompt, PARAGRAPH_SCHEMA, text).get("paragraph") or "").strip()
                if cites(paragraph, [p for p, _ in mine]):
                    paragraphs.append((theme, paragraph))
                    break
        log.info("summarized %d chunks in %d sections: %d themes, %d paragraphs, %d calls",
                 len(input.sources), len(input.sections), len(parts), len(paragraphs), calls)
        russian = any("а" <= c <= "я" for c in q.lower())
        return Generated(text=assemble(paragraphs, missing, russian), model=self.llm.chat_model)
