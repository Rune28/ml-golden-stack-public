-- The keywords of the chunks of the latest committed mentions run of every
-- uploaded file (README, rule 11).
CREATE VIEW IF NOT EXISTS ml_data.chunk_mentions_current AS
SELECT run_id, file_id, index_run_id, chunk_id, term, rank, score
FROM ml_data.chunk_mentions
WHERE run_id IN (
    SELECT argMax(run_id, started_at)
    FROM ml_data.runs FINAL
    WHERE kind = 'mentions'
    GROUP BY parents['file_id']
)
AND file_id IN (SELECT file_id FROM ml_data.files FINAL)
