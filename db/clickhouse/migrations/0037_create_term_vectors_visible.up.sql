-- The vectors of the keywords of the user's files: a term found close to a
-- question is a term of the user's own documents.
CREATE VIEW IF NOT EXISTS ml_data.term_vectors_visible AS
SELECT run_id, file_id, term, embed_model, embedding
FROM ml_data.term_vectors_current
WHERE file_id IN (SELECT file_id FROM ml_data.file_owners FINAL WHERE user_id = {user_id:String})
