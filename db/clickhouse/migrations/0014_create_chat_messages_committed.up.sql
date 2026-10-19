-- What readers of conversations read: the turns whose answer is committed
-- to the run registry (README, rule 11). A turn written halfway (the record
-- step died after its insert, before its marker) is never shown, and the
-- table can change its layout without a reader noticing.
CREATE VIEW IF NOT EXISTS ml_data.chat_messages_committed AS
SELECT run_id, chat_id, turn, role, text, sources, created_at
FROM ml_data.chat_messages
WHERE run_id IN (SELECT run_id FROM ml_data.runs FINAL WHERE kind = 'answer')
