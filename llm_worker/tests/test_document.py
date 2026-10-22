import io
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from pdf_fixture import make_pdf
from rag.documents import HEAD
from workflow.activities.document import Reader
from workflow.contracts import DocumentInput

SCHEDULED = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
FILE = {"name": "squats.pdf", "object_key": "files/abc"}
ANSWER = {
    "title": "Squat depth and knee load",
    "people": [{"name": "John A. Smith", "role": "author"}, {"name": "Maria Ivanova", "role": "author"}],
    "file_name_author": "",
    "year": 2021,
    "kind": "article",
    "abstract": "We measured knee load at three squat depths.",
    "keywords": ["squat", "knee"],
}
PAPER = make_pdf([["Squat depth and knee load", "doi:10.1519/JSC.123.", "Abstract: We measured knee load."]])


class FakeClickHouse:
    """`committed`: the marker of the run is there; `partial`: rows of the run
    without it. Records every write in order."""

    def __init__(self, committed=False, partial=0, file=FILE):
        self.committed, self.partial, self.file, self.writes = committed, partial, file, []

    def query(self, sql, parameters):
        if "FROM runs" in sql:
            rows = [[int(self.committed)]]
        elif "FROM files" in sql:
            rows = [[self.file["name"], self.file["object_key"]]] if self.file else []
        elif sql.startswith("SELECT (SELECT count()"):
            rows = [[self.partial]]
        else:  # the result of a committed run
            rows = [["doi:10.1/x", "Committed title", 2]]
        return type("Result", (), {"result_rows": rows})

    def command(self, sql, parameters):
        self.writes.append(("delete", sql.split()[2], parameters))

    def insert(self, table, rows, column_names):
        self.writes.append((table, [dict(zip(column_names, row)) for row in rows]))


class FakeS3:
    def __init__(self, body: bytes):
        self.body = body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.body)}


class FakeLLM:
    chat_model, embed_model = "qwen3:4b", "bge-m3"

    def __init__(self, answer=ANSWER):
        self.answer, self.prompts, self.embedded = answer, [], []

    def chat_json(self, messages, schema):
        self.prompts.append(messages[-1]["content"])
        return self.answer

    def embed(self, texts):
        self.embedded.append(texts)
        return [[0.5] for _ in texts]


def run(clickhouse, body=PAPER, llm=None):
    llm = llm or FakeLLM()
    env = ActivityEnvironment()
    env.info = replace(env.info, scheduled_time=SCHEDULED)
    reader = Reader(lambda: clickhouse, FakeS3(body), llm, "pipeline")
    return env.run(reader.read_document, DocumentInput(file_id="abc", run_id="doc-1")), llm


def test_a_new_run_writes_the_satellite_then_the_authors_then_its_marker():
    clickhouse = FakeClickHouse()

    result, llm = run(clickhouse)

    [(sat, [document]), (link, authors), (runs, [marker])] = clickhouse.writes
    assert (sat, link, runs) == ("sat_document", "link_document_author", "runs")
    # The key comes from the text, not from the model.
    assert document["document_id"] == "doi:10.1519/jsc.123"
    assert (document["title"], document["year"], document["keywords"]) == ("Squat depth and knee load", 2021, ["squat", "knee"])
    assert (document["abstract_embedding"], document["embed_model"]) == ([0.5], "bge-m3")
    assert [(a["author_id"], a["position"]) for a in authors] == [("name:smith j a", 0), ("name:ivanova m", 1)]
    assert marker["run_id"] == "doc-1" and marker["kind"] == "document" and marker["rows"] == 3
    assert marker["parents"] == {"file_id": "abc", "chat_model": "qwen3:4b"}
    assert marker["started_at"] == SCHEDULED
    assert (result.document_id, result.authors) == ("doi:10.1519/jsc.123", 2)


def test_the_model_gets_the_first_pages_with_the_file_name_and_the_length():
    long = ("word " * HEAD).encode()

    _, llm = run(FakeClickHouse(), body=long)

    [prompt] = llm.prompts
    assert prompt.startswith("File name: squats.pdf\nLength of the whole text: 30,000 characters")
    assert prompt.endswith("\n" + long.decode()[:HEAD])


def test_a_document_without_a_doi_is_keyed_by_its_file():
    clickhouse = FakeClickHouse()

    run(clickhouse, body=b"My notes on squats.\n\nNo references.")

    assert clickhouse.writes[0][1][0]["document_id"] == "file:abc"


def test_a_document_without_an_abstract_has_no_vector():
    clickhouse = FakeClickHouse()

    _, llm = run(clickhouse, llm=FakeLLM({**ANSWER, "abstract": "", "people": []}))

    assert llm.embedded == []
    assert clickhouse.writes[0][1][0]["abstract_embedding"] == []
    # No authors, no link rows; the marker counts the satellite only.
    assert [w[0] for w in clickhouse.writes] == ["sat_document", "runs"]
    assert clickhouse.writes[-1][1][0]["rows"] == 1


def test_a_retry_after_the_marker_asks_the_model_nothing():
    clickhouse = FakeClickHouse(committed=True)

    result, llm = run(clickhouse)

    assert llm.prompts == [] and clickhouse.writes == []
    assert (result.document_id, result.title, result.authors) == ("doi:10.1/x", "Committed title", 2)


def test_rows_of_an_attempt_that_died_are_deleted_and_written_again():
    clickhouse = FakeClickHouse(partial=2)

    run(clickhouse)

    assert clickhouse.writes[:2] == [
        ("delete", "sat_document", {"r": "doc-1"}),
        ("delete", "link_document_author", {"r": "doc-1"}),
    ]
    assert [w[0] for w in clickhouse.writes[2:]] == ["sat_document", "link_document_author", "runs"]


@pytest.mark.parametrize(
    "clickhouse, body, message",
    [(FakeClickHouse(), make_pdf([[]]), "no text"), (FakeClickHouse(file=None), PAPER, "no file abc")],
    ids=["a scan with no text layer", "unknown file"],
)
def test_what_a_retry_cannot_fix_fails_at_once(clickhouse, body, message):
    with pytest.raises(ApplicationError, match=message) as error:
        run(clickhouse, body=body)

    assert error.value.non_retryable
    assert clickhouse.writes == []
