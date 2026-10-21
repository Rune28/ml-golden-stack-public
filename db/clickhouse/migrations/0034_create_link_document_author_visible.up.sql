-- The authors of the documents of the user's files.
CREATE VIEW IF NOT EXISTS ml_data.link_document_author_visible AS
SELECT run_id, file_id, document_id, author_id, name, position
FROM ml_data.link_document_author_current
WHERE file_id IN (SELECT file_id FROM ml_data.file_owners FINAL WHERE user_id = {user_id:String})
