-- The keywords of every chunk, found by the extract_mentions step: the five
-- terms that set the chunk apart from the rest of its document, ranked, in
-- the language of the text (rag.mentions). The own key is run_id of the
-- mentions run (README, rule 5); the chunk is (index_run_id, chunk_id) of
-- doc_chunks, the index run the mentions were found in. Ordered by term:
-- questions about a topic look chunks up by it. Readers use
-- chunk_mentions_current.
CREATE TABLE ml_data.chunk_mentions
(
    run_id       String,
    file_id      String,
    index_run_id String,
    chunk_id     UInt32,
    term         String,
    rank         UInt8,
    score        Float32,
    created_at   DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (term, index_run_id, chunk_id)
