"""read_document: what an uploaded file is as a document, in the worker.

Stream work (README, rule 12), the step of Index after index_file. The first
pages of the text go to the chat model, which reads the title, the authors,
the year, the abstract and the keywords out of them; the keys of the document
and of its authors come from the text itself (rag.documents). The rows are
the satellite sat_document and the link link_document_author, under the
run_id of this run; the hubs are views of their keys (migrations 0015-0021).

Idempotent per run_id: a run committed before is returned as it is, without
asking the model again; rows of an attempt that died before its marker are
deleted and the run is done again; the marker in `runs` comes last, so
readers never see a run halfway (rule 11).
"""

import logging
import os

import boto3
from temporalio import activity
from temporalio.exceptions import ApplicationError

from rag.chunking import read_text
from rag.documents import (
    METADATA_PROMPT,
    METADATA_SCHEMA,
    document_id,
    find_doi,
    find_isbn,
    head,
    metadata_request,
    parse_metadata,
)
from workflow.activities.index import commit_run, connect, read_file
from workflow.contracts import DocumentInput, DocumentResult
from workflow.llm import LLM

log = logging.getLogger(__name__)


class Reader:
    def __init__(self, connect, s3, llm: LLM, bucket: str):
        # A ClickHouse client per call: activities run on several threads at once.
        self.connect, self.s3, self.llm, self.bucket = connect, s3, llm, bucket

    @classmethod
    def from_env(cls) -> "Reader":
        return cls(connect, boto3.client("s3"), LLM.from_env(), os.environ.get("S3_BUCKET", "pipeline"))

    @activity.defn(name="read_document")
    def read_document(self, input: DocumentInput) -> DocumentResult:
        client = self.connect()
        own = {"r": input.run_id}
        if client.query("SELECT count() FROM runs WHERE run_id = {r:String}", parameters=own).result_rows[0][0]:
            return committed(client, input)

        name, object_key = read_file(client, input.file_id)
        text = read_text(self.s3.get_object(Bucket=self.bucket, Key=object_key)["Body"].read())
        if not head(text).strip():
            raise ApplicationError(f"{name}: no text to read a document from", non_retryable=True)

        answer = self.llm.chat_json(
            [{"role": "system", "content": METADATA_PROMPT}, {"role": "user", "content": metadata_request(name, text)}],
            METADATA_SCHEMA,
        )
        meta = parse_metadata(answer, name)
        doc = document_id(find_doi(text), find_isbn(text), input.file_id)
        # The abstract's vector: documents are found by their topic even
        # before the concept graph is built over them.
        embedding = self.llm.embed([meta.abstract])[0] if meta.abstract else []
        log.info("%s is %s: %r, %d authors", name, doc, meta.title, len(meta.authors))

        # Rows of an earlier attempt of this run, which died before its marker.
        partial = client.query(
            "SELECT (SELECT count() FROM sat_document WHERE run_id = {r:String}) "
            "+ (SELECT count() FROM link_document_author WHERE run_id = {r:String})",
            parameters=own,
        ).result_rows[0][0]
        if partial:
            client.command("DELETE FROM sat_document WHERE run_id = {r:String}", parameters=own)
            client.command("DELETE FROM link_document_author WHERE run_id = {r:String}", parameters=own)

        client.insert(
            "sat_document",
            [[input.run_id, input.file_id, doc, meta.title, meta.year, meta.kind, meta.abstract, meta.keywords,
              embedding, self.llm.embed_model]],
            column_names=["run_id", "file_id", "document_id", "title", "year", "kind", "abstract", "keywords",
                          "abstract_embedding", "embed_model"],
        )
        if meta.authors:
            client.insert(
                "link_document_author",
                [[input.run_id, input.file_id, doc, a.author_id, a.name, i] for i, a in enumerate(meta.authors)],
                column_names=["run_id", "file_id", "document_id", "author_id", "name", "position"],
            )
        # When the workflow scheduled the step: the logical start, same on retries.
        commit_run(client, "document", input.run_id, activity.info().scheduled_time, 1 + len(meta.authors),
                   {"file_id": input.file_id, "chat_model": self.llm.chat_model})
        return DocumentResult(run_id=input.run_id, file_id=input.file_id, document_id=doc, title=meta.title,
                              authors=len(meta.authors))


def committed(client, input: DocumentInput) -> DocumentResult:
    """The result of a run committed before: a retry after the marker."""
    doc, title, authors = client.query(
        "SELECT document_id, title, (SELECT count() FROM link_document_author WHERE run_id = {r:String}) "
        "FROM sat_document WHERE run_id = {r:String} LIMIT 1",
        parameters={"r": input.run_id},
    ).result_rows[0]
    return DocumentResult(run_id=input.run_id, file_id=input.file_id, document_id=doc, title=title, authors=authors)
