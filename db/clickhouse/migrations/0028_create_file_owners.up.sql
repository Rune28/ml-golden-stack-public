-- Who owns an uploaded file: a link of a user (Postgres chat.users, the
-- user_id of the platform token) to a file. file_id is the sha256 of the
-- bytes, so the same file of two users is one file with two links, indexed
-- once. The name and the time of the upload are the user's own: another
-- user's name for the same bytes says what they called it, and is theirs.
-- Reference data with a natural key (principle 4): written by be_service
-- when a user uploads, deleted when the user deletes; the file and what was
-- made from it go when no link is left. Readers see files only through the
-- *_visible views of a user.
CREATE TABLE ml_data.file_owners
(
    user_id  String,
    file_id  String,
    name     String,
    added_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(added_at)
ORDER BY (user_id, file_id)
