-- The vectors of the keywords of the latest committed mentions run of every
-- uploaded file, as chunk_mentions_current (README, rule 11).
CREATE VIEW IF NOT EXISTS ml_data.term_vectors_current AS
SELECT run_id, file_id, term, embed_model, embedding
FROM ml_data.term_vectors
WHERE run_id IN (
    SELECT argMax(run_id, started_at)
    FROM ml_data.runs FINAL
    WHERE kind = 'mentions'
    GROUP BY parents['file_id']
)
AND file_id IN (SELECT file_id FROM ml_data.files FINAL)
