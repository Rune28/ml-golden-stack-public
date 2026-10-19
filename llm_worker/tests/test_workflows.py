import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflow import IndexWorkflow
from workflow.contracts import DocumentInput, DocumentResult, IndexInput, IndexResult, MentionsInput, MentionsResult


class FakeIndexer:
    """Stands in for index_file and read_document: records requests in order, answers like them."""

    def __init__(self):
        self.requests = []

    @activity.defn(name="index_file")
    async def index_file(self, input: IndexInput) -> IndexResult:
        self.requests.append(input)
        return IndexResult(run_id=input.run_id, file_id=input.file_id, name="notes.md", chunks=12, chars=9000,
                           embed_model="bge-m3")

    @activity.defn(name="read_document")
    async def read_document(self, input: DocumentInput) -> DocumentResult:
        self.requests.append(input)
        return DocumentResult(run_id=input.run_id, file_id=input.file_id, document_id="doi:10.1/x", title="T", authors=1)

    @activity.defn(name="extract_mentions")
    async def extract_mentions(self, input: MentionsInput) -> MentionsResult:
        self.requests.append(input)
        return MentionsResult(run_id=input.run_id, file_id=input.file_id, mentions=60, themes=9, top_themes=["пруст"])


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def execute(env, indexer: FakeIndexer, arg: IndexInput) -> IndexResult:
    queue = str(uuid.uuid4())
    async with Worker(env.client, task_queue=queue, workflows=[IndexWorkflow], activities=[indexer.index_file, indexer.read_document, indexer.extract_mentions]):
        return await env.client.execute_workflow(IndexWorkflow.run, arg, id=str(uuid.uuid4()), task_queue=queue)


async def test_index_runs_in_the_worker_without_a_job(env):
    indexer = FakeIndexer()

    result = await execute(env, indexer, IndexInput(file_id="file-1", run_id="idx-1"))

    assert indexer.requests[0] == IndexInput(file_id="file-1", run_id="idx-1")
    assert (result.file_id, result.chunks, result.embed_model) == ("file-1", 12, "bge-m3")


async def test_after_the_chunks_the_document_is_read_and_the_keywords_found_each_under_its_own_run(env):
    indexer = FakeIndexer()

    result = await execute(env, indexer, IndexInput(file_id="file-1", run_id="idx-1", document_run_id="doc-1",
                                                    mentions_run_id="men-1"))

    assert indexer.requests == [
        IndexInput(file_id="file-1", run_id="idx-1"),
        DocumentInput(file_id="file-1", run_id="doc-1"),
        # The keywords are found in the chunks of this index run.
        MentionsInput(file_id="file-1", index_run_id="idx-1", run_id="men-1"),
    ]
    assert (result.document_id, result.mentions, result.themes) == ("doi:10.1/x", 60, ["пруст"])


async def test_index_without_run_id_gets_one(env):
    indexer = FakeIndexer()

    result = await execute(env, indexer, IndexInput(file_id="file-1"))

    assert uuid.UUID(result.run_id)
    assert indexer.requests[0].run_id == result.run_id
    # The document run gets one of its own, not the index run's.
    assert uuid.UUID(indexer.requests[1].run_id) and indexer.requests[1].run_id != result.run_id
