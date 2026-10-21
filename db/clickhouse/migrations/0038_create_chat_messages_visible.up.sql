-- The committed turns of the user's chats.
CREATE VIEW IF NOT EXISTS ml_data.chat_messages_visible AS
SELECT run_id, chat_id, turn, role, text, sources, created_at
FROM ml_data.chat_messages_committed
WHERE chat_id IN (SELECT chat_id FROM ml_data.chat_owners FINAL WHERE user_id = {user_id:String})
