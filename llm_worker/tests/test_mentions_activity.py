from dataclasses import replace
from datetime import datetime, timezone

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from workflow.activities import mentions as mentions_module
from workflow.activities.mentions import Extractor
from workflow.contracts import MentionsInput

SCHEDULED = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
TEXTS = ["Пруст и смерть Альбертины.", "Пруст и стиль Бюффона.", "Пруст и время.", "Пруст и смерть."]


class FakeClickHouse:
    """The index run has TEXTS as its chunks. `committed`: the marker is
    there; `partial`: rows of the run without it. Records writes in order."""

    def __init__(self, committed=False, partial=0, texts=TEXTS, model="bge-m3"):
        self.committed, self.partial, self.texts, self.model, self.writes = committed, partial, texts, model, []

    def query(self, sql, parameters):
        if sql.startswith("SELECT count() FROM runs"):
            rows = [[int(self.committed)]]
        elif "SELECT chunk_id, text, embed_model" in sql:
            rows = [[i, t, self.model] for i, t in enumerate(self.texts)]
        elif "SELECT chunk_id, embedding" in sql:
            rows = [[i, [1.0, float(i)]] for i in parameters["ids"]]
        elif sql.startswith("SELECT (SELECT count() FROM chunk_mentions") and "+" in sql:
            rows = [[self.partial]]
        elif sql.startswith("SELECT (SELECT count() FROM chunk_mentions"):
            rows = [[7, ["пруст", "смерть"]]]
        else:  # the number of themes of a committed run
            rows = [[2]]
        return type("Result", (), {"result_rows": rows})

    def command(self, sql, parameters):
        self.writes.append(("delete", sql.split()[2], parameters))

    def insert(self, table, rows, column_names):
        self.writes.append((table, [dict(zip(column_names, row)) for row in rows]))


class FakeLLM:
    embed_model = "bge-m3"

    def __init__(self):
        self.embedded = []

    def embed(self, texts):
        self.embedded.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]


def run(clickhouse, batch=2):
    llm, beats = FakeLLM(), []
    env = ActivityEnvironment()
    env.info = replace(env.info, scheduled_time=SCHEDULED)
    env.on_heartbeat = lambda *details: beats.append(details[0])
    original, mentions_module.BATCH = mentions_module.BATCH, batch
    try:
        result = env.run(Extractor(lambda: clickhouse, llm).extract_mentions,
                         MentionsInput(file_id="abc", index_run_id="idx-1", run_id="men-1"))
    finally:
        mentions_module.BATCH = original
    return result, llm, beats


def test_mentions_are_written_a_batch_at_a_time_then_themes_then_the_marker():
    clickhouse = FakeClickHouse()

    result, llm, beats = run(clickhouse)

    tables = [w[0] for w in clickhouse.writes]
    batch = ["chunk_mentions", "term_vectors"]
    assert tables in ([*batch, *batch, "runs"], [*batch, *batch, "document_themes", "runs"])
    assert beats == [2, 4] and len(llm.embedded) == 2
    mentions = [row for table, rows in clickhouse.writes if table == "chunk_mentions" for row in rows]
    assert {m["chunk_id"] for m in mentions} == {0, 1, 2, 3}
    assert all(m["index_run_id"] == "idx-1" and m["run_id"] == "men-1" for m in mentions)
    # The vector of every term written, of this run and model.
    vectors = [row for table, rows in clickhouse.writes if table == "term_vectors" for row in rows]
    assert sorted(v["term"] for v in vectors) == sorted({m["term"] for m in mentions})
    assert all(v["run_id"] == "men-1" and v["embed_model"] == "bge-m3" and v["file_id"] == "abc" for v in vectors)
    # Ranks start at 1 in every chunk.
    assert sorted(m["rank"] for m in mentions if m["chunk_id"] == 0)[0] == 1
    [marker] = clickhouse.writes[-1][1]
    assert (marker["kind"], marker["started_at"]) == ("mentions", SCHEDULED)
    assert marker["parents"] == {"file_id": "abc", "index_run_id": "idx-1", "embed_model": "bge-m3"}
    assert marker["rows"] == result.mentions + result.themes and result.mentions == len(mentions)


def test_the_vector_of_a_term_is_written_once_per_run_and_unit_length():
    clickhouse, kept = FakeClickHouse(), set()
    extractor = Extractor(lambda: clickhouse, FakeLLM())
    input = MentionsInput(file_id="abc", index_run_id="idx-1", run_id="men-1")

    extractor.write_batch(clickhouse, input, [0], [["смерть"]], kept)
    extractor.write_batch(clickhouse, input, [1], [["смерть", "время"]], kept)

    vectors = [row for table, rows in clickhouse.writes if table == "term_vectors" for row in rows]
    assert [v["term"] for v in vectors] == ["смерть", "время"] and kept == {"смерть", "время"}
    # FakeLLM embeds "смерть" as [6, 1].
    assert vectors[0]["embedding"] == pytest.approx([6 / 37 ** 0.5, 1 / 37 ** 0.5])


def test_a_retry_after_the_marker_does_nothing_again():
    clickhouse = FakeClickHouse(committed=True)

    result, llm, _ = run(clickhouse)

    assert clickhouse.writes == [] and llm.embedded == []
    assert (result.mentions, result.themes, result.top_themes) == (7, 2, ["пруст", "смерть"])


def test_rows_of_an_attempt_that_died_are_deleted_first():
    clickhouse = FakeClickHouse(partial=3)

    run(clickhouse)

    assert clickhouse.writes[:3] == [
        ("delete", "chunk_mentions", {"r": "men-1"}),
        ("delete", "term_vectors", {"r": "men-1"}),
        ("delete", "document_themes", {"r": "men-1"}),
    ]
    assert clickhouse.writes[-1][0] == "runs"


@pytest.mark.parametrize(
    "clickhouse, message",
    [(FakeClickHouse(texts=[]), "no chunks"), (FakeClickHouse(model="other-model"), "embedded by other-model")],
    ids=["no chunks", "chunks of another embedding model"],
)
def test_what_a_retry_cannot_fix_fails_at_once(clickhouse, message):
    with pytest.raises(ApplicationError, match=message) as error:
        run(clickhouse)

    assert error.value.non_retryable
    assert clickhouse.writes == []
