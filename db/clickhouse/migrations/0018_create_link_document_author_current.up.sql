-- The authors of the current document run of every uploaded file (rule 11).
CREATE VIEW IF NOT EXISTS ml_data.link_document_author_current AS
SELECT run_id, file_id, document_id, author_id, name, position
FROM ml_data.link_document_author
WHERE run_id IN (
    SELECT argMax(run_id, started_at)
    FROM ml_data.runs FINAL
    WHERE kind = 'document'
    GROUP BY parents['file_id']
)
AND file_id IN (SELECT file_id FROM ml_data.files FINAL)
