-- Login sessions. The browser holds a random token in a cookie; only its
-- sha256 is here, so a copy of this table logs no one in. A session ends
-- at expires_at (renewed while it is used) or when revoked (logout).
CREATE TABLE sessions (
    token_hash bytea PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
)
