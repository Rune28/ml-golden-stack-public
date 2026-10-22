-- What a user corrected or confirmed in the metadata of a document read from
-- one of their files (docs/search-scope-plan.md). Reference data, not the
-- output of a run (principle 4): written by be_service when the user saves
-- the card of a document, keyed by the user and the file, never touched by a
-- new document run, so a correction outlives every new reading of the file.
-- Each user's own: one user's correction never changes what another sees.
-- `corrected` names the fields the user set (title, authors, kind, year);
-- the others are left to what read_document found. A save writes the whole
-- row again; the latest by edited_at wins. Readers use documents_visible.
CREATE TABLE ml_data.document_edits
(
    user_id   String,
    file_id   String,
    corrected Array(LowCardinality(String)),
    title     String,
    authors   Array(String),
    kind      LowCardinality(String),
    year      Nullable(UInt16),
    confirmed Bool,
    edited_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(edited_at)
ORDER BY (user_id, file_id)
