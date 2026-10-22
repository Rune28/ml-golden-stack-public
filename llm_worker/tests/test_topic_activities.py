import pytest
from temporalio.testing import ActivityEnvironment

from rag import topics
from rag.topics import assemble, cites, lemmas, note_batches, notes_from, render_notes, similar_from, themes_from
from dataclasses import replace

from workflow import profiles
from workflow.activities import topic as topic_module
from workflow.activities.topic import TopicSteps
from workflow.contracts import GatherInput, RouteInput, Section, Source, SummarizeInput


class FakeClickHouse:
    """mentions: (term, file_id, index_run_id, chunk_id); themes: (term, file_id, name, chunks)
    of themes in more than MAX_SHARE of their document's chunks; vectors: (term, distance)
    of the stored terms, the same distance to whatever term is looked up; chunks: (file_id,
    run_id, chunk_id) of every file, the closest to any question first; authors: by file_id."""

    def __init__(self, mentions=(), themes=(), texts=None, vectors=(), chunks=(), authors=None):
        self.mentions, self.themes, self.texts, self.queries = list(mentions), list(themes), texts or {}, []
        self.vectors, self.chunks, self.authors = list(vectors), list(chunks), authors or {}

    def query(self, sql, parameters):
        self.queries.append(sql)
        assert parameters.get("u") == "ann", f"a read without the user: {sql}"
        if "FROM chunk_mentions_visible" in sql:
            rows = [m for m in self.mentions if m[0] in parameters["t"]]
        elif "FROM term_vectors_visible" in sql:
            close = sorted((d, t) for t, d in self.vectors if t != parameters["t"] and d <= parameters["d"])
            rows = [[t, d] for d, t in close][: parameters["n"]]
        elif "FROM document_themes_visible" in sql:
            rows = [t for t in self.themes if t[0] in parameters["t"]]
        elif "FROM files_visible(user_id = {u:String}) WHERE" in sql:
            rows = [[f, f"{f}.txt"] for f in parameters["f"]]
        elif "FROM documents_visible" in sql:
            rows = [[f, self.authors.get(f, "")] for f in parameters["f"]]
        elif "FROM doc_chunks_visible" in sql:  # the nearest of the files: here, in the order given
            taken = set(parameters["x"])
            rows = [list(c) for c in self.chunks
                    if (not parameters["f"] or c[0] in parameters["f"]) and (c[1], c[2]) not in taken][: parameters["n"]]
        elif "cosineDistance" in sql:  # the closest: here, the first n keys
            rows = [[f"f-{r}", r, c] for r, c in parameters["k"][: parameters["n"]]]
        else:  # texts of chunks
            rows = [[r, c, self.texts.get(c, f"text {c}")] for r, c in parameters["k"]]
        return type("Result", (), {"result_rows": rows})


class FakeLLM:
    embed_model, chat_model = "bge-m3", "qwen3:4b"

    def __init__(self, answers=()):
        self.answers, self.prompts = list(answers), []

    def embed(self, texts):
        return [[1.0] for _ in texts]

    def chat_json(self, messages, schema):
        self.prompts.append(messages)
        return self.answers.pop(0) if self.answers else {"answer": "final answer [1]"}


def gather(clickhouse, terms, question="q", min_chunks=1, llm=None):
    """By default any number of chunks is a topic: the tests of which chunks are taken use a few."""
    steps = TopicSteps(lambda: clickhouse, llm or FakeLLM())
    return ActivityEnvironment().run(steps.gather, GatherInput(question=question, terms=terms, min_chunks=min_chunks,
                                                                user_id="ann"))


def test_route_reads_a_short_query_then_the_kind_and_its_terms():
    llm = FakeLLM(answers=[{"query": " меняет ли сон человека? ", "kind": "topic", "terms": ["сон", " ", 3]},
                           {"kind": "fact", "terms": []},
                           {"kind": "interpret", "query": "как описать этот сон?", "terms": ["сон"]},
                           {"kind": "topic", "query": "о чём книга?", "terms": []},
                           {"kind": "story", "query": "q", "terms": ["сон"]}])
    steps = TopicSteps(lambda: None, llm)

    def route(text):
        return ActivityEnvironment().run(steps.route, RouteInput(question=text))

    topic, old, story, no_terms, unknown = (route(t) for t in ["Вопрос про сон...", "что такое S3?", "мне снилось...",
                                                               "о чём книга?", "?"])

    assert (topic.kind, topic.terms, topic.query) == ("topic", ["сон"], "меняет ли сон человека?")
    # The kind and the aspects are read off the whole message, the terms off the query.
    assert list(topics.ROUTE_SCHEMA["properties"]) == ["kind", "aspects", "query", "terms"]
    # No query: empty, and the workflow uses the message.
    assert (old.kind, old.query) == ("fact", "")
    assert story.kind == "interpret"
    # A topic needs terms to gather by; a kind not offered is a fact.
    assert no_terms.kind == "fact" and unknown.kind == "fact"


def test_a_term_also_gathers_the_chunks_of_stored_terms_that_name_the_same():
    # "сон" was a keyword of 3 chunks of a book, "сновидение" of 5 more.
    clickhouse = FakeClickHouse(
        mentions=[("сон", "b", "i", 1), ("сновидение", "b", "i", 2), ("мечтание", "b", "i", 3), ("соната", "b", "i", 4)],
        vectors=[("сновидение", 0.22), ("мечтание", 0.28), ("соната", 0.36)],
    )
    llm = FakeLLM(answers=[{"same": ["сновидение", "пробуждение"]}])  # пробуждение: not a candidate

    gathered = gather(clickhouse, ["сон"], question="меняет ли сон человека?", llm=llm)

    assert [s.chunk_id for s in gathered.sources] == [1, 2] and gathered.similar == {"сон": ["сновидение"]}
    [[prompt]] = llm.prompts
    assert "«сон»" in prompt["content"] and "меняет ли сон человека?" in prompt["content"]
    # Candidates are the close terms only, the closest first.
    assert prompt["content"].endswith("Terms:\nсновидение\nмечтание")


def test_similar_terms_count_as_the_term_when_terms_are_intersected():
    clickhouse = FakeClickHouse(
        mentions=[("сновидение", "b", "i", 2), ("страх", "b", "i", 2), ("страх", "b", "i", 5)],
        vectors=[("сновидение", 0.2)],
    )
    llm = FakeLLM(answers=[{"same": ["сновидение"]}, {"same": []}])

    gathered = gather(clickhouse, ["сон", "страх"], llm=llm)

    assert [s.chunk_id for s in gathered.sources] == [2] and gathered.similar == {"сон": ["сновидение"]}


def test_terms_together_in_too_few_chunks_for_a_topic_give_the_chunks_of_any():
    mentions = [("сон", "b", "i", n) for n in (1, 2, 3)] + [("страх", "b", "i", n) for n in (2, 3, 7, 8, 9)]

    few = gather(FakeClickHouse(mentions=mentions), ["сон", "страх"], min_chunks=3)
    enough = gather(FakeClickHouse(mentions=mentions), ["сон", "страх"], min_chunks=2)

    assert [s.chunk_id for s in few.sources] == [1, 2, 3, 7, 8, 9]
    assert [s.chunk_id for s in enough.sources] == [2, 3]


def test_route_says_which_profile_and_version_it_read_the_message_with(monkeypatch):
    monkeypatch.setenv("LLM_DEFAULT_PROFILE", "philosophy")
    llm = FakeLLM(answers=[{"kind": "fact", "query": "q", "terms": []}] * 2)
    steps = TopicSteps(lambda: None, llm)

    default = ActivityEnvironment().run(steps.route, RouteInput(question="кто автор?"))
    named = ActivityEnvironment().run(steps.route, RouteInput(question="кто автор?", profile="default"))

    assert (default.profile, default.profile_version) == ("philosophy", profiles.load("philosophy").version)
    assert named.profile == "default"
    assert "Пруст" in llm.prompts[0][0]["content"] and "Пруст" not in llm.prompts[1][0]["content"]


def test_terms_that_only_name_documents_are_dropped(monkeypatch):
    # Asked "trace the author's path in the files blog 1-5", the route gave
    # путь, автор, файлы блог: автор became писатель, поэт; блог named no chunk.
    monkeypatch.setenv("LLM_DEFAULT_PROFILE", "philosophy")
    llm = FakeLLM(answers=[{"kind": "topic", "query": "q", "terms": ["путь", "автор", "файлы блог"]},
                           {"kind": "topic", "query": "q", "terms": ["время", "книге"]},
                           {"kind": "topic", "query": "q", "terms": ["книга", "авторы"]}])
    steps = TopicSteps(lambda: None, llm)

    def route():
        return ActivityEnvironment().run(steps.route, RouteInput(question="?"))

    path, time, nothing = route(), route(), route()

    assert path.terms == ["путь"] and time.terms == ["время"]
    # No subject left: a topic with no terms is a fact.
    assert (nothing.kind, nothing.terms) == ("fact", [])


def test_a_term_names_documents_only_when_every_noun_of_it_does():
    words = frozenset({"автор", "блог", "файл", "книга"})

    assert topics.names_documents("файлы блог", words) and topics.names_documents("книгах", words)
    assert not topics.names_documents("путь автора", words) and not topics.names_documents("топология пути", words)
    assert not topics.names_documents("1-5", words)  # no nouns: left to gather, which finds nothing by it


def test_how_near_and_how_many_similar_terms_are_the_profile_s(monkeypatch):
    strict = replace(profiles.load("default"), similar_max_distance=0.2, similar_candidates=1)
    monkeypatch.setattr(topic_module, "load", lambda name: strict)
    clickhouse = FakeClickHouse(mentions=[("сон", "b", "i", 1)],
                                vectors=[("сновидение", 0.15), ("эротический сон", 0.18), ("мечтание", 0.22)])
    llm = FakeLLM(answers=[{"same": ["сновидение"]}])

    gather(clickhouse, ["сон"], llm=llm)

    [[prompt]] = llm.prompts
    assert prompt["content"].endswith("Terms:\nсновидение")


def test_with_no_stored_term_close_to_a_term_the_model_is_not_asked():
    llm = FakeLLM()

    gathered = gather(FakeClickHouse(mentions=[("сон", "b", "i", 1)], vectors=[("ночь", 0.5)]), ["сон"], llm=llm)

    assert llm.prompts == [] and gathered.similar == {} and gathered.found == 1


def test_the_similar_terms_picked_are_candidates_in_their_order():
    answer = {"same": [" солнце", "пробуждение", "сновидение", "сновидение", 7]}

    assert similar_from(answer, ["сновидение", "солнце", "ночь"]) == ["сновидение", "солнце"]
    assert similar_from({}, ["сновидение"]) == []


def test_the_words_of_a_question_become_stored_terms():
    assert lemmas("Пруста") == "пруст"
    assert lemmas("трансмировых линий") == "трансмировая линия"
    assert lemmas("о") is None


def test_chunks_of_a_selective_term_are_gathered():
    clickhouse = FakeClickHouse(mentions=[("смерть", "book", "idx", 7), ("смерть", "book", "idx", 3)])

    gathered = gather(clickhouse, ["смерти"])

    assert gathered.mode == "chunks" and gathered.found == 2
    assert [(s.chunk_id, s.name) for s in gathered.sources] == [(3, "book.txt"), (7, "book.txt")]


def test_two_selective_terms_give_the_chunks_of_both():
    clickhouse = FakeClickHouse(mentions=[("смерть", "b", "i", 1), ("смерть", "b", "i", 2), ("стиль", "b", "i", 2)])

    gathered = gather(clickhouse, ["смерть", "стиль"])

    assert [s.chunk_id for s in gathered.sources] == [2]


def test_a_broad_term_narrows_the_documents_of_a_selective_one():
    clickhouse = FakeClickHouse(
        mentions=[("смерть", "proust-book", "i1", 1), ("смерть", "other-book", "i2", 5)],
        themes=[("пруст", "proust-book", "Топология пути", 1064)],
    )

    gathered = gather(clickhouse, ["Пруст", "смерть"])

    assert [s.file_id for s in gathered.sources] == ["proust-book"]


def test_only_broad_terms_are_too_broad():
    clickhouse = FakeClickHouse(themes=[("пруст", "book", "Топология пути", 1064)])

    gathered = gather(clickhouse, ["Пруста"])

    assert gathered.mode == "too_broad" and gathered.sources == []
    assert [(b.term, b.chunks) for b in gathered.broad] == [("пруст", 1064)]


def test_a_word_that_names_nothing_stored_falls_back_to_a_fact():
    clickhouse = FakeClickHouse(themes=[("пруст", "book", "Топология пути", 1064)])

    assert gather(clickhouse, ["Пруст", "квазары"]).mode == "none"
    assert gather(FakeClickHouse(), ["квазары"]).mode == "none"


def test_a_topic_in_too_few_chunks_falls_back_to_a_fact():
    # "Настоящая жизнь" was a keyword of one chunk of a book; summarized alone,
    # that chunk came back retold, a quote of Proust in the first person.
    clickhouse = FakeClickHouse(mentions=[("настоящая жизнь", "book", "idx", 188)])

    gathered = gather(clickhouse, ["настоящая жизнь"], min_chunks=topics.MIN_CHUNKS)

    assert (gathered.mode, gathered.found, gathered.sources) == ("none", 1, [])
    many = FakeClickHouse(mentions=[("смерть", "b", "i", n) for n in range(topics.MIN_CHUNKS)])
    assert gather(many, ["смерть"], min_chunks=topics.MIN_CHUNKS).mode == "chunks"


def test_more_chunks_than_an_answer_reads_are_cut_to_the_closest():
    mentions = [("смерть", "b", "i", n) for n in range(topics.MAX_CHUNKS + 10)]

    gathered = gather(FakeClickHouse(mentions=mentions), ["смерть"])

    assert gathered.found == topics.MAX_CHUNKS + 10 and len(gathered.sources) == topics.MAX_CHUNKS
    # The workflow can ask for fewer.
    steps = TopicSteps(lambda: FakeClickHouse(mentions=mentions), FakeLLM())
    few = ActivityEnvironment().run(steps.gather, GatherInput(question="q", terms=["смерть"], max_chunks=5, user_id="ann"))
    assert len(few.sources) == 5


def test_summarize_takes_notes_then_themes_then_a_paragraph_per_theme(monkeypatch):
    monkeypatch.setattr(topic_module, "groups", lambda items: [items[:2], items[2:]])
    sources = [Source(file_id="b", index_run_id="i", chunk_id=n, name="book.txt") for n in range(3)]
    llm = FakeLLM(answers=[
        {"notes": [{"passage": 1, "note": "death is in life"}, {"passage": 2, "note": "death repeats"}]},
        {"notes": [{"passage": 3, "note": "death is a curve"}]},
        {"themes": [{"theme": "in life", "passages": [1]}, {"theme": "infinity", "passages": [2, 3]}]},
        {"paragraph": "Death is part of life [1]."},
        {"paragraph": "Only the answer, no labels."},  # cites nothing: asked again
        {"paragraph": "Death repeats like a curve [2, 3]."},
    ])
    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *d: beats.append(d[0])

    generated = env.run(TopicSteps(lambda: FakeClickHouse(), llm).summarize,
                        SummarizeInput(question="о смерти?", sources=sources, user_id="ann"))

    map_one, map_two, themes, first, _, second = llm.prompts
    assert "[1] book.txt\ntext 0" in map_one[1]["content"] and "[3] book.txt\ntext 2" in map_two[1]["content"]
    assert themes[0]["content"].endswith("[1] death is in life\n[2] death repeats\n[3] death is a curve")
    # A paragraph sees the notes of its theme only, as the user's message.
    assert "Theme: in life" in first[0]["content"] and first[1]["content"] == "[1] death is in life"
    assert second[1]["content"] == "[2] death repeats\n[3] death is a curve"
    assert generated.text == "**in life**\nDeath is part of life [1].\n\n**infinity**\nDeath repeats like a curve [2, 3]."
    assert beats == [1, 2, 3, 4, 5, 6]


def test_notes_too_many_for_one_call_are_condensed_first(monkeypatch):
    monkeypatch.setattr(topic_module, "MAX_NOTES_CHARS", 40)
    sources = [Source(file_id="b", index_run_id="i", chunk_id=n, name="book.txt") for n in range(2)]
    llm = FakeLLM(answers=[
        {"notes": [{"passage": 1, "note": "a" * 30}, {"passage": 2, "note": "b" * 30}]},
        {"notes": [{"passage": 1, "note": "short a"}]},
        {"notes": [{"passage": 2, "note": "short b"}]},
        {"themes": [{"theme": "t", "passages": [1, 2]}]},
        {"paragraph": "a and b [1]"},
    ])

    generated = ActivityEnvironment().run(TopicSteps(lambda: FakeClickHouse(), llm).summarize,
                                          SummarizeInput(question="q", sources=sources, user_id="ann"))

    assert "Merge the notes" in llm.prompts[1][0]["content"] and llm.prompts[1][1]["content"] == f"[1] {'a' * 30}"
    assert llm.prompts[3][0]["content"].endswith("[1] short a\n[2] short b")
    assert generated.text == "**t**\na and b [1]"


def test_notes_are_taken_on_the_message_and_themes_named_after_the_query():
    sources = [Source(file_id="b", index_run_id="i", chunk_id=0, name="book.txt")]
    llm = FakeLLM(answers=[{"notes": [{"passage": 1, "note": "n"}]}, {"themes": []}, {"paragraph": "p [1]"}])

    generated = ActivityEnvironment().run(
        TopicSteps(lambda: FakeClickHouse(), llm).summarize,
        SummarizeInput(question="Вопрос про сон. Сначала ты летишь", sources=sources, query="меняет ли сон?",
                       user_id="ann"))

    map_, themes, paragraph = llm.prompts
    assert "Сначала ты летишь" in map_[0]["content"]
    assert "меняет ли сон?" in themes[0]["content"] and "летишь" not in themes[0]["content"]
    # No themes from the model: one, named after the query, never the message.
    assert "Theme: меняет ли сон?" in paragraph[0]["content"]
    assert generated.text == "**меняет ли сон?**\np [1]"


def test_the_question_never_reaches_the_steps_that_write_from_notes(monkeypatch):
    # A dream retold as the question came back as the answer, its sentences
    # cited as passages of a book: the paragraph step answered the question.
    monkeypatch.setattr(topic_module, "MAX_NOTES_CHARS", 40)
    question = "Вопрос про сон: сначала ты летишь и смотришь на магистраль, потом акробатика и людоеды"
    sources = [Source(file_id="b", index_run_id="i", chunk_id=n, name="book.txt") for n in range(2)]
    llm = FakeLLM(answers=[
        {"notes": [{"passage": 1, "note": "a" * 30}, {"passage": 2, "note": "b" * 30}]},
        {"notes": [{"passage": 1, "note": "short a"}]},
        {"notes": [{"passage": 2, "note": "short b"}]},
        {"themes": [{"theme": "сон", "passages": [1, 2]}]},
        {"paragraph": "a and b [1]"},
    ])

    ActivityEnvironment().run(TopicSteps(lambda: FakeClickHouse(), llm).summarize,
                              SummarizeInput(question=question, sources=sources, user_id="ann"))

    _, condense_one, condense_two, themes, paragraph = llm.prompts
    assert question in themes[0]["content"]  # themes are chosen to answer it
    for messages in (condense_one, condense_two, paragraph):
        assert not any("магистраль" in m["content"] for m in messages)


def test_themes_are_checked_and_a_passage_belongs_to_one_theme():
    answer = {"themes": [{"theme": "a", "passages": [1, 2, 9]}, {"theme": "b", "passages": [2, 3]},
                         {"theme": "", "passages": [4]}, {"theme": "c", "passages": [1]}]}

    assert themes_from(answer, {1, 2, 3, 4}) == [("a", [1, 2]), ("b", [3])]


def test_a_paragraph_must_cite_a_passage_of_its_theme():
    assert cites("Смерть — часть жизни [1, 2].", [2])
    assert not cites("Смерть — часть жизни [7].", [1, 2])
    assert not cites("Сократить до 3-5 предложений, без меток.", [1])


def test_notes_are_batched_under_a_limit_in_passage_order():
    notes = [(2, "b" * 10), (1, "a" * 10), (3, "c" * 10)]

    assert note_batches(notes, 20) == [[(1, "a" * 10)], [(2, "b" * 10)], [(3, "c" * 10)]]
    assert assemble([("t", "p [1]")]) == "**t**\np [1]"


def test_when_no_passage_says_anything_there_is_no_answer_to_combine():
    sources = [Source(file_id="b", index_run_id="i", chunk_id=0, name="book.txt")]
    llm = FakeLLM(answers=[{"notes": []}])

    generated = ActivityEnvironment().run(TopicSteps(lambda: FakeClickHouse(), llm).summarize,
                                          SummarizeInput(question="о квазарах?", sources=sources, user_id="ann"))

    assert generated.text == "" and len(llm.prompts) == 1


def test_a_note_must_name_a_passage_it_was_given_and_say_something():
    answer = {"notes": [{"passage": 2, "note": " ok "}, {"passage": 9, "note": "not given"},
                        {"passage": 3, "note": ""}, {"passage": "2", "note": "a string"}, "broken"]}

    assert notes_from(answer, {1, 2, 3}) == [(2, "ok")]


def test_notes_are_read_in_the_order_of_the_passages_and_cut_at_the_limit():
    notes = [(3, "c" * 10), (1, "a" * 10), (2, "b" * 10)]

    assert render_notes(notes) == f"[1] {'a' * 10}\n[2] {'b' * 10}\n[3] {'c' * 10}"
    assert render_notes(notes, limit=30) == f"[1] {'a' * 10}\n[2] {'b' * 10}"


def test_gather_without_a_user_reads_nothing():
    clickhouse = FakeClickHouse(mentions=[("смерть", "book", "idx", 7)])
    steps = TopicSteps(lambda: clickhouse, FakeLLM())

    gathered = ActivityEnvironment().run(steps.gather, GatherInput(question="q", terms=["смерть"]))

    assert gathered.mode == "none" and clickhouse.queries == []


def test_summarize_without_a_user_reads_no_texts():
    clickhouse = FakeClickHouse(texts={0: "a passage of someone else"})
    llm = FakeLLM(answers=[{"notes": []}])
    sources = [Source(file_id="b", index_run_id="i", chunk_id=0, name="book.txt")]

    ActivityEnvironment().run(TopicSteps(lambda: clickhouse, llm).summarize, SummarizeInput(question="q", sources=sources))

    assert clickhouse.queries == []
    assert all("someone else" not in m["content"] for prompt in llm.prompts for m in prompt)


def test_gather_in_a_scope_reads_only_its_files_of_the_visible_ones():
    clickhouse = FakeClickHouse(mentions=[("сон", "b", "i", 1)], vectors=[("сновидение", 0.2)])
    steps = TopicSteps(lambda: clickhouse, FakeLLM(answers=[{"same": []}]))

    ActivityEnvironment().run(steps.gather, GatherInput(question="q", terms=["сон"], min_chunks=1, user_id="ann",
                                                        file_ids=["f-blog"]))

    reads = [sql for sql in clickhouse.queries if any(v in sql for v in ("chunk_mentions", "document_themes", "term_vectors"))]
    assert len(reads) == 3
    assert all("_visible(user_id = {u:String})" in sql and "has({f:Array(String)}" in sql for sql in reads)


class FacetClickHouse:
    """The user's documents (authors, kind) and groups (name, id), for where a message looks."""

    def __init__(self, documents, groups):
        self.documents, self.groups, self.queries = documents, groups, []

    def query(self, sql, parameters):
        self.queries.append((sql, parameters))
        rows = self.documents if "documents_visible" in sql else self.groups
        return type("Result", (), {"result_rows": rows})


GROUPS = {"Блоги": "c-blogs", "Лекции о Прусте": "c-lectures"}
AUTHORS = ["Merab Mamardashvili", "Smith, J. A."]
KIND_WORDS = profiles.load("philosophy").kind_words


@pytest.mark.parametrize("message, want", [
    ("а проследи путь автора в блогах?", (["c-blogs"], [], [])),
    ("а проследи путь автора именно файлов блог 1-5?", (["c-blogs"], [], [])),
    ("что Мамардашвили говорит о времени?", ([], ["Merab Mamardashvili"], [])),
    ("what does Smith say about squats?", ([], ["Smith, J. A."], [])),
    ("что я писал в своих заметках о тюрьме культуры?", ([], [], ["notes"])),
    ("что в книге сказано о времени?", ([], [], ["book"])),
    ("что говорится в лекциях о Прусте про время?", (["c-lectures"], [], ["book"])),
    # Where to look is not named: every file.
    ("что Пруст говорит о смерти?", ([], [], [])),
    ("что такое трансмировая линия?", ([], [], [])),
    ("как бы ты описал этот сон? мне снилось, что я лечу над магистралью", ([], [], [])),
], ids=["group", "group by a file name", "author in Cyrillic", "author with a comma", "kind", "a book",
        "group and kind", "a subject, not an author", "a fact", "a dream"])
def test_where_a_message_looks_is_what_it_names_of_the_user_s_own(message, want):
    # Measured cases: asked of the model with the lists, 3 right of 45.
    assert topics.where_in(message, GROUPS, AUTHORS, KIND_WORDS, ["book", "notes"]) == want


def test_a_kind_the_user_has_no_document_of_narrows_nothing():
    assert topics.where_in("что в статьях о сне?", GROUPS, AUTHORS, KIND_WORDS, ["book", "notes"]) == ([], [], [])


def test_the_route_reads_the_user_s_own_values_for_where_the_message_looks():
    clickhouse = FacetClickHouse(documents=[[["Merab Mamardashvili"], "book"], [[], "notes"]],
                                 groups=[["Блоги", "c-blogs"]])
    llm = FakeLLM(answers=[{"kind": "fact", "query": "q", "terms": []}])

    route = ActivityEnvironment().run(TopicSteps(lambda: clickhouse, llm).route,
                                      RouteInput(question="проследи путь автора в блогах", user_id="ann",
                                                 profile="philosophy"))

    assert (route.filters.collections, route.filters.authors, route.filters.kinds) == (["c-blogs"], [], [])
    # One call to the model, the route itself; and only the user's views are read.
    assert len(llm.prompts) == 1
    assert all("_visible(user_id = {u:String})" in sql and p == {"u": "ann"} for sql, p in clickhouse.queries)


# Chat 01a16252: "what Саша thinks of life, death, love... and how his notes
# differ from the book of Мамардашвили".
COMPARED = "Изучи заметки Саши, что он думает о жизни, смерти, любви и других философских вопросах, " \
           "и чем заметки Саши отличаются по философии от книги Мамардашвили?"


def test_a_message_naming_two_authors_compares_them_and_their_names_are_no_terms():
    clickhouse = FacetClickHouse(documents=[[["Саша"], "notes"], [["Мамардашвили"], "book"]], groups=[])
    llm = FakeLLM(answers=[{"kind": "interpret", "query": "чем заметки Саши отличаются",
                            "terms": ["Саши", "Мамардашвили", "философия"],
                            "aspects": ["жизнь", "смерть", "любовь", "другие философские вопросы"]}])

    route = ActivityEnvironment().run(TopicSteps(lambda: clickhouse, llm).route,
                                      RouteInput(question=COMPARED, user_id="ann", profile="philosophy"))

    # Whatever the model read: two sides named compare.
    assert route.kind == "compare"
    assert route.terms == ["философия"] and route.aspects == ["жизнь", "смерть", "любовь"]
    assert [(s.label, s.scope.authors, s.scope.kinds) for s in route.sides] == [
        ("Мамардашвили", ["Мамардашвили"], ["book", "notes"]), ("Саша", ["Саша"], ["book", "notes"])]


def test_aspects_make_a_topic_and_one_side_compares_nothing():
    clickhouse = FacetClickHouse(documents=[[["Саша"], "notes"]], groups=[])
    llm = FakeLLM(answers=[{"kind": "interpret", "query": "что Саша думает", "terms": ["Саша"],
                            "aspects": ["жизнь", "смерть", "любовь"]}])

    route = ActivityEnvironment().run(TopicSteps(lambda: clickhouse, llm).route,
                                      RouteInput(question="что Саша думает о жизни, смерти и любви?", user_id="ann",
                                                 profile="philosophy"))

    assert (route.kind, route.terms, route.sides) == ("topic", [], [])
    assert route.aspects == ["жизнь", "смерть", "любовь"]


def test_aspects_are_words_of_the_message_two_or_more_once_each():
    words = profiles.load("philosophy").document_words

    def aspects(message, given):
        return topics.aspects_from({"aspects": given}, message, words)

    # Of a dream the model once gave words it never had.
    assert aspects("мне снилось, что я лечу над магистралью", ["мечта", "мажорность"]) == []
    assert aspects("о жизни, смерти и других вопросах", ["жизнь", "смерти", "смерть", "другие вопросы"]) == \
        ["жизнь", "смерти"]
    # One subject is no list.
    assert aspects("о смерти и книгах", ["смерть", "книги"]) == []


def test_names_authors_only_when_every_word_is_a_surname_of_one():
    assert topics.names_authors("Саши", ["Саша"])
    assert topics.names_authors("Mamardashvili", ["Мамардашвили"])
    assert not topics.names_authors("смерть Саши", ["Саша"])
    assert not topics.names_authors("Пруст", ["Мамардашвили"])


def test_a_part_is_filled_with_the_closest_chunks_of_its_files_when_keywords_are_few():
    # In 640 chunks of Саша's notes любовь was a keyword of one.
    clickhouse = FakeClickHouse(mentions=[("любовь", "blog", "i", 5)],
                                chunks=[("book", "j", 1), ("blog", "i", 5), ("blog", "i", 8), ("blog", "i", 9)])
    steps = TopicSteps(lambda: clickhouse, FakeLLM())

    gathered = ActivityEnvironment().run(steps.gather, GatherInput(
        question="любовь", terms=["любовь"], max_chunks=3, user_id="ann", file_ids=["blog"], fill=True))

    assert gathered.mode == "chunks" and gathered.found == 1
    assert [(s.file_id, s.chunk_id) for s in gathered.sources] == [("blog", 5), ("blog", 8), ("blog", 9)]


def test_a_part_with_no_terms_is_the_closest_chunks():
    clickhouse = FakeClickHouse(chunks=[("blog", "i", 2), ("blog", "i", 3)])
    steps = TopicSteps(lambda: clickhouse, FakeLLM())

    gathered = ActivityEnvironment().run(steps.gather, GatherInput(
        question="чем отличаются?", terms=[], max_chunks=5, user_id="ann", fill=True))

    assert [s.chunk_id for s in gathered.sources] == [2, 3]


def test_notes_are_marked_with_whose_documents_they_come_from():
    assert render_notes([(2, "b"), (1, "a")], who={1: "Саша", 2: ""}) == "[1] (Саша) a\n[2] b"


def test_a_comparison_is_a_paragraph_per_aspect_with_every_side_and_whose_words_each_note_is():
    sources = [Source(file_id="blog", index_run_id="i", chunk_id=1, name="блог.txt"),
               Source(file_id="book", index_run_id="j", chunk_id=2, name="lectures.txt")]
    sections = [Section(aspect="смерть", side="Саша", passages=[1]), Section(aspect="любовь", side="Саша", passages=[1]),
                Section(aspect="смерть", side="Мамардашвили", passages=[2]),
                Section(aspect="любовь", side="Мамардашвили", passages=[2])]
    llm = FakeLLM(answers=[
        {"notes": [{"passage": 1, "note": "смерть — конец"}]},
        {"notes": []},
        {"notes": [{"passage": 2, "note": "смерть — в жизни"}]},
        {"notes": []},
        {"paragraph": "Саша [1], Мамардашвили [2]."},
    ])
    clickhouse = FakeClickHouse(authors={"book": "Мамардашвили"})

    generated = ActivityEnvironment().run(TopicSteps(lambda: clickhouse, llm).summarize, SummarizeInput(
        question=COMPARED, sources=sources, query="чем отличаются", user_id="ann", sections=sections))

    map_death_sasha, map_love_sasha, *_, paragraph = llm.prompts
    # Notes are taken on the aspect, with whose passage it is, never on the whole message.
    assert "Question: смерть" in map_death_sasha[0]["content"] and "Саша" not in map_death_sasha[0]["content"]
    assert map_death_sasha[1]["content"].startswith("[1] Саша · блог.txt")
    assert "Question: любовь" in map_love_sasha[0]["content"]
    assert "documents of: Саша, Мамардашвили" in paragraph[0]["content"]
    assert "Theme: смерть" in paragraph[0]["content"]
    assert paragraph[1]["content"] == "[1] (Саша) смерть — конец\n[2] (Мамардашвили) смерть — в жизни"
    # An aspect nobody wrote about is said, not left out.
    assert generated.text == "**смерть**\nСаша [1], Мамардашвили [2].\n\nВ документах не нашлось ничего о: любовь."
