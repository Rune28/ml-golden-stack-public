-- What retrieval reads: the chunks of the latest committed index of every
-- file, per embedding model (README, rule 11). Retrieval filters by the
-- model it embeds the question with, so after switching models a file is
-- found only once it is indexed with the new one.
CREATE VIEW IF NOT EXISTS ml_data.doc_chunks_current AS
SELECT run_id, file_id, chunk_id, embed_model, text, embedding
FROM ml_data.doc_chunks
WHERE run_id IN (
    SELECT argMax(run_id, started_at)
    FROM ml_data.runs FINAL
    WHERE kind = 'index'
    GROUP BY parents['file_id'], parents['embed_model']
)
