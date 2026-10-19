-- What a document is about, by the same mentions run as chunk_mentions:
-- terms frequent in the document and rare in the language, every one of
-- them with its rank; readers take the top N (rank <= N), so N changes
-- without running anything again.
CREATE TABLE ml_data.document_themes
(
    run_id     String,
    file_id    String,
    term       String,
    rank       UInt32,
    chunks     UInt32,
    share      Float32,
    zipf       Float32,
    score      Float32,
    created_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (term, file_id, run_id)
