-- Chunks of the user's files from every index run, for texts read by key:
-- an index committed while an answer is written must not hide the chunks
-- the answer was given.
CREATE VIEW IF NOT EXISTS ml_data.doc_chunks_owned AS
SELECT run_id, file_id, chunk_id, embed_model, text, embedding
FROM ml_data.doc_chunks
WHERE file_id IN (SELECT file_id FROM ml_data.file_owners FINAL WHERE user_id = {user_id:String})
