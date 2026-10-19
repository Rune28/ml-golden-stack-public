-- Output of the answer step of a chat: the question and the answer of one
-- turn, two rows under run_id of the answer run (README, rule 5), so a
-- retried step rewrites only its own rows. chat_id and turn order a
-- conversation. sources are the chunks the answer was given, keyed by the
-- index run they came from: an answer traces back to its files with a join.
-- The chat keeps only its recent messages in the workflow; readers of a
-- whole conversation read the committed turns through
-- chat_messages_committed (0014, rule 11).
CREATE TABLE ml_data.chat_messages
(
    run_id     String,
    chat_id    String,
    turn       UInt32,
    role       LowCardinality(String),
    text       String,
    sources    Array(Tuple(file_id String, index_run_id String, chunk_id UInt32)),
    created_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (chat_id, turn, role)
