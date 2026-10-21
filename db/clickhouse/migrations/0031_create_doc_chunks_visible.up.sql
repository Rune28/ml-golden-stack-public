-- The current chunks of the user's files: what retrieval reads.
CREATE VIEW IF NOT EXISTS ml_data.doc_chunks_visible AS
SELECT run_id, file_id, chunk_id, embed_model, text, embedding
FROM ml_data.doc_chunks_current
WHERE file_id IN (SELECT file_id FROM ml_data.file_owners FINAL WHERE user_id = {user_id:String})
