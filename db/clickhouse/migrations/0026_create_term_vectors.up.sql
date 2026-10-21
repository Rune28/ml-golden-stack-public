-- The vectors of the keywords of chunk_mentions, by the same mentions run:
-- extract_mentions embeds every candidate to rank it and keeps the vectors
-- of the terms it writes, one per term and run. A question about a topic
-- finds the stored terms close to its own words ("сон" → сновидение), and
-- the concept graph (S6b) will build on them. The vectors are unit length,
-- of the model in embed_model: vectors of different models are never
-- compared. Readers use term_vectors_current.
CREATE TABLE ml_data.term_vectors
(
    run_id      String,
    file_id     String,
    term        String,
    embed_model String,
    embedding   Array(Float32),
    created_at  DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (term, run_id)
