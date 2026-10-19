"""The steps of one chat answer: retrieve, generate, record.

Stream work (README, rule 12), blocking (ClickHouse, the LLM server), on the
worker's thread pool. Payloads between the steps are keys of chunks, not
their texts (principle 1): generate reads the texts it needs itself.
"""

import logging

from temporalio import activity

from workflow.activities.index import commit_run, connect
from workflow.contracts import Generated, GenerateInput, RecordInput, RetrieveInput, Source
from workflow.llm import LLM

log = logging.getLogger(__name__)

SYSTEM = """You answer questions about the user's documents.
Use only the numbered context below. If the answer is not in it, say that the documents do not say.
Cite the context you used as [1], [2]. Answer in the language of the question.

Context:
{context}"""


class Chatter:
    def __init__(self, connect, llm: LLM):
        # A ClickHouse client per call: activities run on several threads at once.
        self.connect, self.llm = connect, llm

    @classmethod
    def from_env(cls) -> "Chatter":
        return cls(connect, LLM.from_env())

    @activity.defn(name="retrieve")
    def retrieve(self, input: RetrieveInput) -> list[Source]:
        """The k chunks closest to the question, of the latest index of every
        file. Only chunks of the model the question is embedded with: vectors
        of different models are never compared. Only chunks of files that
        exist: an index committed after its file was deleted stays unseen.
        One text once: the same paragraph in two files takes one place."""
        [vector] = self.llm.embed([input.question])
        rows = self.connect().query(
            "SELECT c.file_id, c.run_id, c.chunk_id, f.name, cosineDistance(c.embedding, {q:Array(Float32)}) AS distance "
            "FROM doc_chunks_current AS c INNER JOIN (SELECT file_id, name FROM files FINAL) AS f ON f.file_id = c.file_id "
            "WHERE c.embed_model = {m:String} ORDER BY distance LIMIT 1 BY cityHash64(c.text) LIMIT {k:UInt32}",
            parameters={"q": vector, "m": self.llm.embed_model, "k": input.k},
        ).result_rows
        return [Source(file_id=f, index_run_id=r, chunk_id=c, name=n, distance=float(d)) for f, r, c, n, d in rows]

    @activity.defn(name="generate")
    def generate(self, input: GenerateInput) -> Generated:
        texts = self.read_chunks(input.sources)
        context = "\n\n".join(
            f"[{i}] {s.name}\n{texts.get((s.index_run_id, s.chunk_id), '')}" for i, s in enumerate(input.sources, 1)
        ) or "(no documents)"
        messages = [
            {"role": "system", "content": SYSTEM.format(context=context)},
            *({"role": m.role, "content": m.content} for m in input.history),
            {"role": "user", "content": input.question},
        ]
        reply = self.llm.chat(messages)
        log.info("answered with %d sources, %d tokens, model %s", len(input.sources), reply.completion_tokens, reply.model)
        return Generated(text=reply.text.strip(), model=reply.model)

    def read_chunks(self, sources: list[Source]) -> dict[tuple[str, int], str]:
        """Texts by key, from the table and not the view: a run's chunks never
        change, and a newer index committed since retrieve must not hide them."""
        if not sources:
            return {}
        rows = self.connect().query(
            "SELECT run_id, chunk_id, text FROM doc_chunks WHERE (run_id, chunk_id) IN {keys:Array(Tuple(String, UInt32))}",
            parameters={"keys": [(s.index_run_id, s.chunk_id) for s in sources]},
        ).result_rows
        return {(r, c): t for r, c, t in rows}

    @activity.defn(name="record")
    def record(self, input: RecordInput) -> None:
        """The question and the answer of one turn under the answer's run_id,
        then its marker. Idempotent like index_file: both rows there → kept,
        one → deleted and written again; the marker comes last."""
        client = self.connect()
        own = {"r": input.run_id}
        existing = client.query("SELECT count() FROM chat_messages WHERE run_id = {r:String}", parameters=own).result_rows[0][0]
        if existing != 2:
            if existing:
                client.command("DELETE FROM chat_messages WHERE run_id = {r:String}", parameters=own)
            sources = [(s.file_id, s.index_run_id, s.chunk_id) for s in input.sources]
            client.insert(
                "chat_messages",
                [
                    [input.run_id, input.chat_id, input.turn, "user", input.question, []],
                    [input.run_id, input.chat_id, input.turn, "assistant", input.answer, sources],
                ],
                column_names=["run_id", "chat_id", "turn", "role", "text", "sources"],
            )
        # When the workflow scheduled the step: the logical start, same on retries.
        commit_run(client, "answer", input.run_id, activity.info().scheduled_time, 2, {"chat_id": input.chat_id})
