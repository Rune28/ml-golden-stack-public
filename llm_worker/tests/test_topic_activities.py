from temporalio.testing import ActivityEnvironment

from rag import topics
from rag.topics import assemble, cites, lemmas, note_batches, notes_from, render_notes, themes_from
from workflow.activities import topic as topic_module
from workflow.activities.topic import TopicSteps
from workflow.contracts import GatherInput, Source, SummarizeInput


class FakeClickHouse:
    """mentions: (term, file_id, index_run_id, chunk_id); themes: (term, file_id, name, chunks)
    of themes in more than MAX_SHARE of their document's chunks."""

    def __init__(self, mentions=(), themes=(), texts=None):
        self.mentions, self.themes, self.texts, self.queries = list(mentions), list(themes), texts or {}, []

    def query(self, sql, parameters):
        self.queries.append(sql)
        if "FROM chunk_mentions_current" in sql:
            rows = [m for m in self.mentions if m[0] in parameters["t"]]
        elif "FROM document_themes_current" in sql:
            rows = [t for t in self.themes if t[0] in parameters["t"]]
        elif "FROM files FINAL WHERE" in sql:
            rows = [[f, f"{f}.txt"] for f in parameters["f"]]
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


def gather(clickhouse, terms, question="q", min_chunks=1):
    """By default any number of chunks is a topic: the tests of which chunks are taken use a few."""
    steps = TopicSteps(lambda: clickhouse, FakeLLM())
    return ActivityEnvironment().run(steps.gather, GatherInput(question=question, terms=terms, min_chunks=min_chunks))


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
    few = ActivityEnvironment().run(steps.gather, GatherInput(question="q", terms=["смерть"], max_chunks=5))
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
                        SummarizeInput(question="о смерти?", sources=sources))

    map_one, map_two, themes, first, _, second = llm.prompts
    assert "[1] book.txt\ntext 0" in map_one[1]["content"] and "[3] book.txt\ntext 2" in map_two[1]["content"]
    assert themes[0]["content"].endswith("[1] death is in life\n[2] death repeats\n[3] death is a curve")
    # A paragraph sees the notes of its theme only.
    assert first[0]["content"].endswith("Notes:\n[1] death is in life")
    assert second[0]["content"].endswith("Notes:\n[2] death repeats\n[3] death is a curve")
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
                                          SummarizeInput(question="q", sources=sources))

    assert "Merge the notes" in llm.prompts[1][0]["content"]
    assert llm.prompts[3][0]["content"].endswith("[1] short a\n[2] short b")
    assert generated.text == "**t**\na and b [1]"


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
                                          SummarizeInput(question="о квазарах?", sources=sources))

    assert generated.text == "" and len(llm.prompts) == 1


def test_a_note_must_name_a_passage_it_was_given_and_say_something():
    answer = {"notes": [{"passage": 2, "note": " ok "}, {"passage": 9, "note": "not given"},
                        {"passage": 3, "note": ""}, {"passage": "2", "note": "a string"}, "broken"]}

    assert notes_from(answer, {1, 2, 3}) == [(2, "ok")]


def test_notes_are_read_in_the_order_of_the_passages_and_cut_at_the_limit():
    notes = [(3, "c" * 10), (1, "a" * 10), (2, "b" * 10)]

    assert render_notes(notes) == f"[1] {'a' * 10}\n[2] {'b' * 10}\n[3] {'c' * 10}"
    assert render_notes(notes, limit=30) == f"[1] {'a' * 10}\n[2] {'b' * 10}"
