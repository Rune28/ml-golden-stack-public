import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflow import IndexWorkflow
from workflow.contracts import IndexInput, IndexResult


class FakeIndexer:
    """Stands in for index_file: records requests, answers like it."""

    def __init__(self):
        self.requests = []

    @activity.defn(name="index_file")
    async def index_file(self, input: IndexInput) -> IndexResult:
        self.requests.append(input)
        return IndexResult(run_id=input.run_id, file_id=input.file_id, name="notes.md", chunks=12, chars=9000,
                           embed_model="bge-m3")


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def execute(env, indexer: FakeIndexer, arg: IndexInput) -> IndexResult:
    queue = str(uuid.uuid4())
    async with Worker(env.client, task_queue=queue, workflows=[IndexWorkflow], activities=[indexer.index_file]):
        return await env.client.execute_workflow(IndexWorkflow.run, arg, id=str(uuid.uuid4()), task_queue=queue)


async def test_index_runs_in_the_worker_without_a_job(env):
    indexer = FakeIndexer()

    result = await execute(env, indexer, IndexInput(file_id="file-1", run_id="idx-1"))

    assert indexer.requests == [IndexInput(file_id="file-1", run_id="idx-1")]
    assert (result.file_id, result.chunks, result.embed_model) == ("file-1", 12, "bge-m3")


async def test_index_without_run_id_gets_one(env):
    indexer = FakeIndexer()

    result = await execute(env, indexer, IndexInput(file_id="file-1"))

    assert uuid.UUID(result.run_id)
    assert indexer.requests[0].run_id == result.run_id
