-- How a user logs in. Today one kind, provider 'password': the subject is
-- the email, the secret an argon2id hash. An identity provider later is
-- another row for the same user_id, provider 'oidc:<issuer>' and the
-- provider's subject, with no secret: what refers to users stays.
CREATE TABLE identities (
    provider   text NOT NULL,
    subject    text NOT NULL,
    user_id    uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    secret     text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, subject)
)
