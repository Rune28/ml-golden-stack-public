-- Which of the user's files are in which of their groups; a file may be in
-- several. Adding writes a row, removing writes it again with removed =
-- true, the latest by changed_at wins. Readers use collection_files_visible.
CREATE TABLE ml_data.collection_files
(
    user_id       String,
    collection_id String,
    file_id       String,
    removed       Bool DEFAULT false,
    changed_at    DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(changed_at)
ORDER BY (user_id, collection_id, file_id)
