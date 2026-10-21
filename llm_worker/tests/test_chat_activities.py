from dataclasses import replace
from datetime import datetime, timezone

import pytest
from temporalio.testing import ActivityEnvironment

from workflow.activities.chat import Chatter
from workflow.contracts import ChatMessage, GenerateInput, RecordInput, RetrieveInput, Source
from workflow.llm import Reply

SCHEDULED = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
SOURCES = [
    Source(file_id="file-1", index_run_id="idx-1", chunk_id=0, name="notes.md", distance=0.1),
    Source(file_id="file-2", index_run_id="idx-2", chunk_id=4, name="plan.md", distance=0.3),
]


class FakeClickHouse:
    """Answers queries with canned rows; records every query and write in order."""

    def __init__(self, rows=(), existing=0):
        self.rows, self.existing, self.queries, self.writes = list(rows), existing, [], []

    def query(self, sql, parameters):
        self.queries.append((sql, parameters))
        rows = [[self.existing]] if "count()" in sql else self.rows
        return type("Result", (), {"result_rows": rows})

    def command(self, sql, parameters):
        self.writes.append(("delete", parameters))

    def insert(self, table, rows, column_names):
        self.writes.append((table, rows))


class FakeLLM:
    embed_model = "bge-m3"

    def __init__(self):
        self.embedded, self.messages = [], None

    def embed(self, texts):
        self.embedded.append(texts)
        return [[0.5, 0.5]]

    def chat(self, messages):
        self.messages = messages
        return Reply(text=" It says so [1]. ", model="qwen3:4b", completion_tokens=7)


def run(activity_name, clickhouse, input):
    llm = FakeLLM()
    env = ActivityEnvironment()
    env.info = replace(env.info, scheduled_time=SCHEDULED)
    result = env.run(getattr(Chatter(lambda: clickhouse, llm), activity_name), input)
    return result, llm


def test_retrieve_embeds_the_question_and_searches_only_its_model():
    clickhouse = FakeClickHouse(rows=[["file-1", "idx-1", 0, "notes.md", 0.1]])

    sources, llm = run("retrieve", clickhouse, RetrieveInput(question="what?", k=3, user_id="ann"))

    assert llm.embedded == [["what?"]]
    [(sql, parameters)] = clickhouse.queries
    # Only the user's files: their current chunks and their names for the files.
    assert "FROM doc_chunks_visible(user_id = {u:String})" in sql and "c.embed_model = {m:String}" in sql
    assert "FROM files_visible(user_id = {u:String})" in sql and "LIMIT 1 BY cityHash64(c.text)" in sql
    assert parameters == {"q": [0.5, 0.5], "m": "bge-m3", "k": 3, "u": "ann"}
    assert sources == [SOURCES[0]]


def test_retrieve_without_a_user_reads_nothing():
    clickhouse = FakeClickHouse(rows=[["file-1", "idx-1", 0, "notes.md", 0.1]])

    sources, llm = run("retrieve", clickhouse, RetrieveInput(question="what?"))

    assert sources == [] and clickhouse.queries == [] and llm.embedded == []


def test_generate_numbers_the_sources_and_keeps_the_conversation():
    clickhouse = FakeClickHouse(rows=[["idx-2", 4, "Plan text."], ["idx-1", 0, "Notes text."]])
    history = [ChatMessage("user", "hi"), ChatMessage("assistant", "hello")]

    generated, llm = run("generate", clickhouse,
                         GenerateInput(question="what?", sources=SOURCES, history=history, user_id="ann"))

    [(sql, parameters)] = clickhouse.queries
    # Texts by key of every index run, of the user's files only.
    assert "FROM doc_chunks_owned(user_id = {u:String}) WHERE (run_id, chunk_id) IN" in sql
    assert parameters == {"keys": [("idx-1", 0), ("idx-2", 4)], "u": "ann"}
    system, *rest = llm.messages
    assert "[1] notes.md\nNotes text." in system["content"] and "[2] plan.md\nPlan text." in system["content"]
    assert rest == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "what?"}]
    assert (generated.text, generated.model) == ("It says so [1].", "qwen3:4b")


def test_generate_without_a_user_reads_no_chunk_texts():
    clickhouse = FakeClickHouse(rows=[["idx-1", 0, "Notes text."]])

    _, llm = run("generate", clickhouse, GenerateInput(question="what?", sources=SOURCES, history=[]))

    assert clickhouse.queries == []
    assert "Notes text." not in llm.messages[0]["content"]


def test_generate_without_sources_asks_the_model_without_reading_chunks():
    clickhouse = FakeClickHouse()

    run("generate", clickhouse, GenerateInput(question="what?", sources=[], history=[]))

    assert clickhouse.queries == []


RECORD = RecordInput(run_id="ans-1", chat_id="chat-1", turn=3, question="what?", answer="this", sources=SOURCES)
MARKER = ("runs", [["ans-1", "answer", {"chat_id": "chat-1"}, SCHEDULED, 2]])


def test_record_writes_the_turn_then_its_marker():
    clickhouse = FakeClickHouse()

    run("record", clickhouse, RECORD)

    [(table, rows), marker] = clickhouse.writes
    assert table == "chat_messages"
    assert rows == [
        ["ans-1", "chat-1", 3, "user", "what?", []],
        ["ans-1", "chat-1", 3, "assistant", "this", [("file-1", "idx-1", 0), ("file-2", "idx-2", 4)]],
    ]
    assert marker == MARKER


@pytest.mark.parametrize(
    "existing, writes",
    [(2, ["runs"]), (1, ["delete", "chat_messages", "runs"])],
    ids=["retry after a complete write only commits", "partial write is deleted and written again"],
)
def test_record_is_idempotent_per_run(existing, writes):
    clickhouse = FakeClickHouse(existing=existing)

    run("record", clickhouse, RECORD)

    assert [w[0] for w in clickhouse.writes] == writes
    assert clickhouse.writes[-1] == MARKER
