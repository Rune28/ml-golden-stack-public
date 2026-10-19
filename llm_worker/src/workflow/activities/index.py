"""index_file: an uploaded file becomes chunks with embeddings, in the worker.

Stream work (README, rule 12): one upload is one event, indexed as it comes
against the model server, with no Job and no pod to start. Blocking
(ClickHouse, S3, the LLM server), so it runs on the worker's thread pool.

Idempotent per run_id, like the batch steps: a complete run is kept, a
partial one (the previous attempt died mid-write) is deleted and written
again, and the marker in `runs` comes last, so readers never see a run
halfway (rule 11). A retry after a complete write does not embed again.
"""

import logging
import os
from datetime import datetime

import boto3
import clickhouse_connect
from temporalio import activity
from temporalio.exceptions import ApplicationError

from rag.chunking import chunk, distinct, read_text
from workflow.contracts import IndexInput, IndexResult
from workflow.llm import LLM

DATABASE = "ml_data"

# Chunks per embedding request and per insert. The worker holds the vectors
# of one batch, not of the whole file, and heartbeats after each one.
BATCH = 32

log = logging.getLogger(__name__)


def connect():
    return clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ["CLICKHOUSE_USER"],
        password=os.environ["CLICKHOUSE_PASSWORD"],
        database=DATABASE,
    )


class Indexer:
    def __init__(self, connect, s3, llm: LLM, bucket: str):
        # A ClickHouse client per call: activities run on several threads at once.
        self.connect, self.s3, self.llm, self.bucket = connect, s3, llm, bucket

    @classmethod
    def from_env(cls) -> "Indexer":
        return cls(connect, boto3.client("s3"), LLM.from_env(), os.environ.get("S3_BUCKET", "pipeline"))

    @activity.defn(name="index_file")
    def index_file(self, input: IndexInput) -> IndexResult:
        client = self.connect()
        name, object_key = read_file(client, input.file_id)
        raw = self.s3.get_object(Bucket=self.bucket, Key=object_key)["Body"].read()
        text = read_text(raw)
        texts = distinct(chunk(text))
        chars = len(text)
        # The embedding below takes minutes and needs only the chunks: the
        # bytes and the whole text, two more copies of the file, go now.
        del raw, text
        if not texts:
            raise ApplicationError(f"{name}: no text to index", non_retryable=True)

        model = self.llm.embed_model
        log.info("indexing %s: %d chars, %d chunks, model %s", name, chars, len(texts), model)
        self.write_chunks(client, input.run_id, input.file_id, model, texts)
        # When the workflow scheduled the step, not when this attempt began:
        # the logical start of the run in the run registry, same on retries.
        commit_run(client, "index", input.run_id, activity.info().scheduled_time, len(texts),
                   {"file_id": input.file_id, "embed_model": model})
        return IndexResult(run_id=input.run_id, file_id=input.file_id, name=name, chunks=len(texts),
                           chars=chars, embed_model=model)

    def write_chunks(self, client, run_id: str, file_id: str, model: str, texts: list[str]) -> None:
        """Only this run's rows. A batch at a time: embed, insert, heartbeat."""
        own = {"r": run_id}
        existing = client.query("SELECT count() FROM doc_chunks WHERE run_id = {r:String}", parameters=own).result_rows[0][0]
        if existing == len(texts):
            return
        if existing:
            client.command("DELETE FROM doc_chunks WHERE run_id = {r:String}", parameters=own)
        for start in range(0, len(texts), BATCH):
            part = texts[start : start + BATCH]
            vectors = self.llm.embed(part)
            client.insert(
                "doc_chunks",
                [[run_id, file_id, start + i, model, text, vector] for i, (text, vector) in enumerate(zip(part, vectors, strict=True))],
                column_names=["run_id", "file_id", "chunk_id", "embed_model", "text", "embedding"],
            )
            activity.heartbeat(start + len(part))


def read_file(client, file_id: str) -> tuple[str, str]:
    """Name and object key of an uploaded file."""
    rows = client.query(
        "SELECT name, object_key FROM files WHERE file_id = {f:String} LIMIT 1", parameters={"f": file_id}
    ).result_rows
    if not rows:
        raise ApplicationError(f"no file {file_id}", non_retryable=True)
    return rows[0][0], rows[0][1]


def commit_run(client, kind: str, run_id: str, started_at: datetime, rows: int, parents: dict[str, str]) -> None:
    """The last action of a step that wrote rows: they are complete. A copy of
    commit_run in mle_models/src/jobs/clickhouse.py commits batch runs: keep
    the two writing the same marker. A repeated commit is a duplicate that
    merges away."""
    client.insert(
        "runs",
        [[run_id, kind, parents, started_at, rows]],
        column_names=["run_id", "kind", "parents", "started_at", "rows"],
    )
