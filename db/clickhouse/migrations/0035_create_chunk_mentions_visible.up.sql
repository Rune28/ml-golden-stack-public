-- The keywords of the chunks of the user's files.
CREATE VIEW IF NOT EXISTS ml_data.chunk_mentions_visible AS
SELECT run_id, file_id, index_run_id, chunk_id, term, rank, score
FROM ml_data.chunk_mentions_current
WHERE file_id IN (SELECT file_id FROM ml_data.file_owners FINAL WHERE user_id = {user_id:String})
