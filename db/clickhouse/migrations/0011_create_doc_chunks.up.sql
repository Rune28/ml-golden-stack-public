-- Output of the `index` step: a file cut into chunks, each with its
-- embedding. The own key is run_id of the index run (README, rule 5): a
-- retried run rewrites only its own rows, and a new run of the same file
-- leaves the previous one alone; readers pick the current one through
-- doc_chunks_current. embed_model is part of a chunk's identity: vectors of
-- different models are never compared.
CREATE TABLE ml_data.doc_chunks
(
    run_id      LowCardinality(String),
    file_id     LowCardinality(String),
    chunk_id    UInt32,
    embed_model LowCardinality(String),
    text        String,
    embedding   Array(Float32),
    created_at  DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (run_id, chunk_id)
