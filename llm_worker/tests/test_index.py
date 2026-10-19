import io
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from workflow.activities import index
from workflow.activities.index import Indexer
from workflow.contracts import IndexInput

SCHEDULED = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
FILE = {"name": "notes.md", "object_key": "files/abc"}


class FakeClickHouse:
    """files has one row; doc_chunks has `existing` rows of the run. Records every write in order."""

    def __init__(self, existing=0, file=FILE):
        self.existing, self.file, self.writes = existing, file, []

    def query(self, sql, parameters):
        if "FROM files" in sql:
            rows = [[self.file["name"], self.file["object_key"]]] if self.file else []
        else:
            rows = [[self.existing]]
        return type("Result", (), {"result_rows": rows})

    def command(self, sql, parameters):
        self.writes.append(("delete", parameters))

    def insert(self, table, rows, column_names):
        self.writes.append((table, rows))


class FakeS3:
    def __init__(self, body: bytes):
        self.body, self.keys = body, []

    def get_object(self, Bucket, Key):
        self.keys.append((Bucket, Key))
        return {"Body": io.BytesIO(self.body)}


class FakeLLM:
    embed_model = "bge-m3"

    def __init__(self):
        self.batches = []

    def embed(self, texts):
        self.batches.append(len(texts))
        return [[float(len(t))] for t in texts]


def run(clickhouse, body: bytes, batch=2):
    llm, s3 = FakeLLM(), FakeS3(body)
    beats = []
    env = ActivityEnvironment()
    env.info = replace(env.info, scheduled_time=SCHEDULED)
    env.on_heartbeat = lambda *details: beats.append(details[0])
    original, index.BATCH = index.BATCH, batch
    try:
        result = env.run(Indexer(lambda: clickhouse, s3, llm, "pipeline").index_file, IndexInput("abc", "idx-1"))
    finally:
        index.BATCH = original
    return result, llm, s3, beats


# Three paragraphs too long to share a chunk: three chunks.
TEXT = "\n\n".join(c * 600 for c in "abc").encode()


def test_a_new_run_is_embedded_batch_by_batch_and_committed_last():
    clickhouse = FakeClickHouse()

    result, llm, s3, beats = run(clickhouse, TEXT)

    assert s3.keys == [("pipeline", "files/abc")]
    assert llm.batches == [2, 1]
    assert beats == [2, 3]
    [first, second, marker] = clickhouse.writes
    assert [row[2] for row in first[1] + second[1]] == [0, 1, 2]
    assert first[1][0][:4] == ["idx-1", "abc", 0, "bge-m3"]
    assert marker == ("runs", [["idx-1", "index", {"file_id": "abc", "embed_model": "bge-m3"}, SCHEDULED, 3]])
    assert (result.name, result.chunks, result.chars, result.embed_model) == ("notes.md", 3, len(TEXT), "bge-m3")


def test_a_file_that_repeats_itself_is_indexed_once():
    clickhouse = FakeClickHouse()
    twice = "\n\n".join(c * 600 for c in "abab").encode()

    result, llm, _, _ = run(clickhouse, twice)

    written = [row for table, rows in clickhouse.writes if table == "doc_chunks" for row in rows]
    assert [(row[2], row[4][0]) for row in written] == [(0, "a"), (1, "b")]
    assert result.chunks == 2 and result.chars == len(twice)


def test_a_retry_after_a_complete_write_only_commits():
    clickhouse = FakeClickHouse(existing=3)

    _, llm, _, _ = run(clickhouse, TEXT)

    assert llm.batches == []
    assert [w[0] for w in clickhouse.writes] == ["runs"]


def test_a_partial_run_is_deleted_and_written_again():
    clickhouse = FakeClickHouse(existing=2)

    run(clickhouse, TEXT)

    assert clickhouse.writes[0] == ("delete", {"r": "idx-1"})
    assert [w[0] for w in clickhouse.writes[1:]] == ["doc_chunks", "doc_chunks", "runs"]


@pytest.mark.parametrize(
    "clickhouse, body, message",
    [(FakeClickHouse(), b"  \n\n ", "no text to index"), (FakeClickHouse(file=None), TEXT, "no file abc")],
    ids=["empty file", "unknown file"],
)
def test_what_a_retry_cannot_fix_fails_at_once(clickhouse, body, message):
    with pytest.raises(ApplicationError, match=message) as error:
        run(clickhouse, body)

    assert error.value.non_retryable
    assert clickhouse.writes == []
