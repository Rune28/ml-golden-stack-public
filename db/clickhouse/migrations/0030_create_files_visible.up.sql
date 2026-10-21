-- The files a user may see: the isolation boundary of the platform. Every
-- *_visible view takes the user_id of the request, ml_data.files_visible(
-- user_id = ...), and readers of user data read only these views; without
-- the parameter a query fails instead of reading everyone's rows. The name
-- and the time of the upload come from the user's own link.
CREATE VIEW IF NOT EXISTS ml_data.files_visible AS
SELECT f.file_id AS file_id, o.name AS name, f.size AS size, f.object_key AS object_key, o.added_at AS uploaded_at
FROM ml_data.files AS f FINAL
INNER JOIN (
    SELECT file_id, name, added_at FROM ml_data.file_owners FINAL WHERE user_id = {user_id:String}
) AS o ON o.file_id = f.file_id
