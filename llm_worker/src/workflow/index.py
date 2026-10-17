"""Index: an uploaded file becomes chunks with embeddings, ready for retrieval.

Stream work (README, rule 12): one file per upload, indexed as it comes by an
activity of the worker against the model server, with no Job to start.
Indexing a file again (another embedding model, a retry after a failure) is a
new run; the previous one stays until this one is committed, and readers
switch over at once.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import IndexInput, IndexResult

# Activity by name: the workflow sandbox never imports ClickHouse or boto3.
INDEX_FILE = "index_file"
RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))


@workflow.defn(name="Index")
class IndexWorkflow:
    @workflow.run
    async def run(self, input: IndexInput) -> IndexResult:
        run_id = input.run_id or str(workflow.uuid4())
        return await workflow.execute_activity(
            INDEX_FILE,
            IndexInput(file_id=input.file_id, run_id=run_id),
            result_type=IndexResult,
            # Embedding waits on the LLM server: minutes for a large file.
            start_to_close_timeout=timedelta(minutes=15),
            # A heartbeat after every batch of chunks: a dead worker is noticed in a minute.
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=RETRY,
        )
