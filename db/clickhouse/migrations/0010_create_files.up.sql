-- Files uploaded for retrieval. The bytes live in the object store under
-- object_key; this row is written after them, so a file listed here is
-- always there. file_id is the sha256 of the bytes, made by be_service: the
-- same bytes uploaded again are the same file, from any replica. Two uploads
-- of them at once both write this row; the duplicate merges away, and until
-- it does readers take one row per file_id (FINAL or LIMIT 1).
CREATE TABLE ml_data.files
(
    file_id     String,
    name        String,
    size        UInt64,
    object_key  String,
    uploaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree
ORDER BY file_id
