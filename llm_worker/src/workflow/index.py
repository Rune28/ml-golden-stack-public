"""Index: an uploaded file becomes chunks with embeddings, ready for retrieval,
a document with its metadata and authors, and the keywords of its chunks.

Stream work (README, rule 12): one file per upload, indexed as it comes by
activities of the worker against the model server, with no Job to start.
Indexing a file again (another embedding model, a retry after a failure) is a
new run; the previous one stays until this one is committed, and readers
switch over at once.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import DocumentInput, DocumentResult, IndexInput, IndexResult, MentionsInput, MentionsResult

# Activities by name: the workflow sandbox never imports ClickHouse or boto3.
INDEX_FILE = "index_file"
READ_DOCUMENT = "read_document"
EXTRACT_MENTIONS = "extract_mentions"
RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))


@workflow.defn(name="Index")
class IndexWorkflow:
    @workflow.run
    async def run(self, input: IndexInput) -> IndexResult:
        run_id = input.run_id or str(workflow.uuid4())
        result = await workflow.execute_activity(
            INDEX_FILE,
            IndexInput(file_id=input.file_id, run_id=run_id),
            result_type=IndexResult,
            # Embedding waits on the LLM server: minutes for a large file.
            start_to_close_timeout=timedelta(minutes=15),
            # A heartbeat after every batch of chunks: a dead worker is noticed in a minute.
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=RETRY,
        )
        # A step added after the first release: workflows started before it
        # replay without it (principle 6).
        if workflow.patched("read-document"):
            document = await workflow.execute_activity(
                READ_DOCUMENT,
                DocumentInput(file_id=input.file_id, run_id=input.document_run_id or str(workflow.uuid4())),
                result_type=DocumentResult,
                # One answer of the chat model and one embedding: seconds.
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RETRY,
            )
            result.document_id = document.document_id
        if workflow.patched("extract-mentions"):
            mentions = await workflow.execute_activity(
                EXTRACT_MENTIONS,
                MentionsInput(file_id=input.file_id, index_run_id=run_id,
                              run_id=input.mentions_run_id or str(workflow.uuid4())),
                result_type=MentionsResult,
                # Embeddings of the candidates: about two minutes for a book.
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(seconds=120),
                retry_policy=RETRY,
            )
            result.mentions, result.themes = mentions.mentions, mentions.top_themes
        return result
