-- The documents read from the user's files.
CREATE VIEW IF NOT EXISTS ml_data.sat_document_visible AS
SELECT run_id, file_id, document_id, title, year, kind, abstract, keywords, abstract_embedding, embed_model
FROM ml_data.sat_document_current
WHERE file_id IN (SELECT file_id FROM ml_data.file_owners FINAL WHERE user_id = {user_id:String})
