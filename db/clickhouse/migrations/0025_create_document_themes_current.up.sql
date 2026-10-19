-- The themes of the latest committed mentions run of every uploaded file (rule 11).
CREATE VIEW IF NOT EXISTS ml_data.document_themes_current AS
SELECT run_id, file_id, term, rank, chunks, share, zipf, score
FROM ml_data.document_themes
WHERE run_id IN (
    SELECT argMax(run_id, started_at)
    FROM ml_data.runs FINAL
    WHERE kind = 'mentions'
    GROUP BY parents['file_id']
)
AND file_id IN (SELECT file_id FROM ml_data.files FINAL)
