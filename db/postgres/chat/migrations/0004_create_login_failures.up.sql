-- Failed logins, counted per email and for the whole service over the last
-- minutes, so that every replica refuses the same guessing. Rows older than
-- a day are deleted as new ones come.
CREATE TABLE login_failures (
    email text NOT NULL,
    at    timestamptz NOT NULL DEFAULT now()
)
