-- What readers of documents read: the latest committed document run of every
-- uploaded file (README, rule 11), as doc_chunks_current does for chunks. A
-- file deleted after its run was committed drops out, whatever the order of
-- the deletion steps.
CREATE VIEW IF NOT EXISTS ml_data.sat_document_current AS
SELECT run_id, file_id, document_id, title, year, kind, abstract, keywords, abstract_embedding, embed_model
FROM ml_data.sat_document
WHERE run_id IN (
    SELECT argMax(run_id, started_at)
    FROM ml_data.runs FINAL
    WHERE kind = 'document'
    GROUP BY parents['file_id']
)
AND file_id IN (SELECT file_id FROM ml_data.files FINAL)
