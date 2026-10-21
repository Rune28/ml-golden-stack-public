-- Who owns a chat: written by be_service when a user starts one. A message
-- or the history of a chat is refused to anyone else.
CREATE TABLE ml_data.chat_owners
(
    user_id    String,
    chat_id    String,
    created_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree
ORDER BY (user_id, chat_id)
