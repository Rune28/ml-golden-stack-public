-- The user's groups that are not deleted, with the user's files in them
-- (a file the user deleted drops out of its groups).
CREATE VIEW IF NOT EXISTS ml_data.collections_visible AS
SELECT c.collection_id AS collection_id, c.name AS name, c.changed_at AS changed_at, groupArrayIf(cf.file_id, cf.file_id != '') AS file_ids
FROM (SELECT collection_id, name, changed_at FROM ml_data.collections FINAL WHERE user_id = {user_id:String} AND NOT deleted) AS c
LEFT JOIN (
    SELECT collection_id, file_id FROM ml_data.collection_files FINAL
    WHERE user_id = {user_id:String} AND NOT removed
    AND file_id IN (SELECT file_id FROM ml_data.file_owners FINAL WHERE user_id = {user_id:String})
) AS cf ON cf.collection_id = c.collection_id
GROUP BY c.collection_id, c.name, c.changed_at
