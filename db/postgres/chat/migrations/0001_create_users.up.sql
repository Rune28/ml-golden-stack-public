-- The users of the chat product. user_id is ours and stays when the way a
-- user logs in changes (identities, 0002): the owner links of files and
-- chats in ClickHouse refer to it. Users are created by an admin
-- (chat_service user-add); disabled_at ends every login without deleting
-- what the user owns.
CREATE TABLE users (
    user_id     uuid PRIMARY KEY,
    email       text NOT NULL UNIQUE CHECK (email = lower(email)),
    created_at  timestamptz NOT NULL DEFAULT now(),
    disabled_at timestamptz
)
