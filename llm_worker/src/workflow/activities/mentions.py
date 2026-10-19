"""extract_mentions: the keywords of every chunk of a file and the themes of
the document, in the worker, with no language model (rag.mentions).

Stream work (README, rule 12), the step of Index after read_document. It
reads the chunks of the index run it is given, counts the candidates of
every chunk over the whole text (lemmas and counts of a book: seconds),
then goes a batch of chunks at a time: their vectors from doc_chunks, the
vectors of their candidates from the embedding model, the top five per
chunk, an insert and a heartbeat. So the worker holds the vectors of one
batch, not of the whole file. The themes of the document come from the
counts, last.

Idempotent per run_id, like read_document: a committed run is returned as
it is; rows of an attempt that died before its marker are deleted and the
run is done again; the marker in `runs` comes last (rule 11).
"""

import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError

from rag.mentions import pools, themes, top_mentions, unit
from workflow.activities.index import commit_run, connect
from workflow.contracts import MentionsInput, MentionsResult
from workflow.llm import LLM

# Chunks per batch: the vectors of 64 chunks and of up to 15 candidates each.
BATCH = 64
# Themes in the result, for the workflow history; all of them are written.
TOP_THEMES = 15

log = logging.getLogger(__name__)


class Extractor:
    def __init__(self, connect, llm: LLM):
        # A ClickHouse client per call: activities run on several threads at once.
        self.connect, self.llm = connect, llm

    @classmethod
    def from_env(cls) -> "Extractor":
        return cls(connect, LLM.from_env())

    @activity.defn(name="extract_mentions")
    def extract_mentions(self, input: MentionsInput) -> MentionsResult:
        client = self.connect()
        own = {"r": input.run_id}
        if client.query("SELECT count() FROM runs WHERE run_id = {r:String}", parameters=own).result_rows[0][0]:
            return committed(client, input)

        rows = client.query(
            "SELECT chunk_id, text, embed_model FROM doc_chunks WHERE run_id = {i:String} ORDER BY chunk_id",
            parameters={"i": input.index_run_id},
        ).result_rows
        if not rows:
            raise ApplicationError(f"no chunks in index run {input.index_run_id}", non_retryable=True)
        # Candidates are compared with the chunks' vectors: one model for both.
        if rows[0][2] != self.llm.embed_model:
            raise ApplicationError(
                f"chunks of {input.index_run_id} are embedded by {rows[0][2]}, not {self.llm.embed_model}",
                non_retryable=True,
            )
        ids = [r[0] for r in rows]
        best, df = pools([r[1] for r in rows])
        del rows

        # Rows of an earlier attempt of this run, which died before its marker.
        partial = client.query(
            "SELECT (SELECT count() FROM chunk_mentions WHERE run_id = {r:String}) "
            "+ (SELECT count() FROM document_themes WHERE run_id = {r:String})",
            parameters=own,
        ).result_rows[0][0]
        if partial:
            client.command("DELETE FROM chunk_mentions WHERE run_id = {r:String}", parameters=own)
            client.command("DELETE FROM document_themes WHERE run_id = {r:String}", parameters=own)

        mentions = 0
        for start in range(0, len(ids), BATCH):
            mentions += self.write_batch(client, input, ids[start : start + BATCH], best[start : start + BATCH])
            activity.heartbeat(start + BATCH)

        found = themes(df, len(ids))
        if found:
            client.insert(
                "document_themes",
                [[input.run_id, input.file_id, t.term, t.rank, t.chunks, t.share, t.zipf, t.score] for t in found],
                column_names=["run_id", "file_id", "term", "rank", "chunks", "share", "zipf", "score"],
            )
        log.info("%s: %d mentions in %d chunks, %d themes, top %s", input.file_id, mentions, len(ids), len(found),
                 [t.term for t in found[:5]])
        # When the workflow scheduled the step: the logical start, same on retries.
        commit_run(client, "mentions", input.run_id, activity.info().scheduled_time, mentions + len(found),
                   {"file_id": input.file_id, "index_run_id": input.index_run_id, "embed_model": self.llm.embed_model})
        return MentionsResult(run_id=input.run_id, file_id=input.file_id, mentions=mentions, themes=len(found),
                              top_themes=[t.term for t in found[:TOP_THEMES]])

    def write_batch(self, client, input: MentionsInput, ids: list[int], best: list[list[str]]) -> int:
        """The top terms of a batch of chunks, inserted; their number."""
        chunks = dict(client.query(
            "SELECT chunk_id, embedding FROM doc_chunks WHERE run_id = {i:String} AND chunk_id IN {ids:Array(UInt32)}",
            parameters={"i": input.index_run_id, "ids": ids},
        ).result_rows)
        # Neighbouring chunks share terms: each is embedded once per batch.
        terms = sorted({t for pool in best for t in pool})
        if not terms:
            return 0
        vectors = dict(zip(terms, map(unit, self.llm.embed(terms)), strict=True))
        rows = [
            [input.run_id, input.file_id, input.index_run_id, chunk_id, term, rank, score]
            for chunk_id, pool in zip(ids, best, strict=True)
            for rank, (term, score) in enumerate(top_mentions(unit(chunks[chunk_id]), pool, vectors), 1)
        ]
        if rows:
            client.insert(
                "chunk_mentions", rows,
                column_names=["run_id", "file_id", "index_run_id", "chunk_id", "term", "rank", "score"],
            )
        return len(rows)


def committed(client, input: MentionsInput) -> MentionsResult:
    """The result of a run committed before: a retry after the marker."""
    mentions, top = client.query(
        "SELECT (SELECT count() FROM chunk_mentions WHERE run_id = {r:String}), "
        "(SELECT arrayMap(t -> t.2, arraySort(groupArray((rank, term)))) FROM document_themes "
        "WHERE run_id = {r:String} AND rank <= {top:UInt32})",
        parameters={"r": input.run_id, "top": TOP_THEMES},
    ).result_rows[0]
    themes_count = client.query(
        "SELECT count() FROM document_themes WHERE run_id = {r:String}", parameters={"r": input.run_id}
    ).result_rows[0][0]
    return MentionsResult(run_id=input.run_id, file_id=input.file_id, mentions=mentions, themes=themes_count,
                          top_themes=list(top))
