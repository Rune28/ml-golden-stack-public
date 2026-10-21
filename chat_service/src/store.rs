//! Users, identities, sessions and failed logins in the Postgres database
//! `chat` of this service (db/postgres/chat/migrations, applied by a Job
//! before it starts). What the login needs from it is the trait `AuthStore`;
//! a fake implements it in tests.

use std::future::Future;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use deadpool_postgres::{Config, Pool, Runtime};
use tokio_postgres::NoTls;
use uuid::Uuid;

/// A user as the login sees them: the identity with provider 'password'.
#[derive(Clone, Debug)]
pub struct Account {
    pub user_id: Uuid,
    pub email: String,
    /// argon2id hash.
    pub secret: String,
    pub disabled: bool,
}

/// The user a valid session belongs to.
#[derive(Clone, Debug, PartialEq)]
pub struct SessionUser {
    pub user_id: Uuid,
    pub email: String,
}

pub trait AuthStore: Send + Sync + 'static {
    fn account(&self, email: &str) -> impl Future<Output = Result<Option<Account>>> + Send;
    /// Counts a login attempt of the email before its password is checked,
    /// unless the email already has `email_limit` attempts over the window,
    /// or the whole service `service_limit`: false then, and nothing is
    /// counted. An attempt stays counted as a failure until a login of the
    /// email succeeds (`clear_attempts`). Attempts of one email are counted
    /// one at a time, so a burst of parallel guesses cannot all pass a count
    /// none of them has joined yet.
    fn claim_attempt(
        &self,
        email: &str,
        window: Duration,
        email_limit: i64,
        service_limit: i64,
    ) -> impl Future<Output = Result<bool>> + Send;
    fn clear_attempts(&self, email: &str) -> impl Future<Output = Result<()>> + Send;
    fn create_session(
        &self,
        token_hash: &[u8],
        user_id: Uuid,
        ttl: Duration,
    ) -> impl Future<Output = Result<()>> + Send;
    /// The user of a session that is not revoked, not expired, not older than
    /// `max` and of a user not disabled; its expiry moves `ttl` ahead, since
    /// it is being used. `max` bounds that renewal: a stolen cookie in use
    /// does not live forever.
    fn session_user(
        &self,
        token_hash: &[u8],
        ttl: Duration,
        max: Duration,
    ) -> impl Future<Output = Result<Option<SessionUser>>> + Send;
    fn revoke(&self, token_hash: &[u8]) -> impl Future<Output = Result<()>> + Send;
}

pub struct PgStore {
    pool: Pool,
}

impl PgStore {
    /// From DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD. Connections are
    /// made on first use: the service starts while the database is down.
    pub fn from_env() -> Result<Self> {
        let var = |name: &str| std::env::var(name).with_context(|| format!("{name} is not set"));
        let mut config = Config::new();
        config.host = Some(var("DB_HOST")?);
        config.port = Some(var("DB_PORT")?.parse().context("DB_PORT")?);
        config.dbname = Some(var("DB_NAME")?);
        config.user = Some(var("DB_USER")?);
        config.password = Some(var("DB_PASSWORD")?);
        Ok(Self { pool: config.create_pool(Some(Runtime::Tokio1), NoTls).context("database pool")? })
    }

    async fn client(&self) -> Result<deadpool_postgres::Object> {
        self.pool.get().await.context("database connection")
    }

    /// A user with a password, for `user-add`; refused if the email is taken.
    pub async fn create_user(&self, email: &str, secret: &str) -> Result<Uuid> {
        let mut client = self.client().await?;
        let tx = client.transaction().await?;
        if tx.query_opt("SELECT 1 FROM users WHERE email = $1", &[&email]).await?.is_some() {
            bail!("a user with the email {email} exists");
        }
        let user_id = Uuid::now_v7();
        tx.execute("INSERT INTO users (user_id, email) VALUES ($1, $2)", &[&user_id, &email]).await?;
        tx.execute(
            "INSERT INTO identities (provider, subject, user_id, secret) VALUES ('password', $1, $2, $3)",
            &[&email, &user_id, &secret],
        )
        .await?;
        tx.commit().await?;
        Ok(user_id)
    }

    /// A new password, for `user-password`: every session of the user ends.
    pub async fn set_password(&self, email: &str, secret: &str) -> Result<()> {
        let mut client = self.client().await?;
        let tx = client.transaction().await?;
        let Some(row) = tx
            .query_opt(
                "UPDATE identities SET secret = $2 WHERE provider = 'password' AND subject = $1 RETURNING user_id",
                &[&email, &secret],
            )
            .await?
        else {
            bail!("no user with the email {email}");
        };
        let user_id: Uuid = row.get(0);
        tx.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", &[&user_id])
            .await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn user_id(&self, email: &str) -> Result<Option<Uuid>> {
        let row = self.client().await?.query_opt("SELECT user_id FROM users WHERE email = $1", &[&email]).await?;
        Ok(row.map(|r| r.get(0)))
    }
}

impl AuthStore for PgStore {
    async fn account(&self, email: &str) -> Result<Option<Account>> {
        let row = self
            .client()
            .await?
            .query_opt(
                "SELECT u.user_id, u.email, i.secret, u.disabled_at IS NOT NULL FROM identities AS i \
                 JOIN users AS u USING (user_id) WHERE i.provider = 'password' AND i.subject = $1",
                &[&email],
            )
            .await?;
        Ok(row.map(|r| Account { user_id: r.get(0), email: r.get(1), secret: r.get(2), disabled: r.get(3) }))
    }

    async fn claim_attempt(&self, email: &str, window: Duration, email_limit: i64, service_limit: i64) -> Result<bool> {
        let mut client = self.client().await?;
        let tx = client.transaction().await?;
        // Attempts of the same email wait here for each other until commit, so
        // each one counts the attempts before it. Other emails do not wait:
        // the limit for the whole service may be passed by a few at once.
        tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", &[&email]).await?;
        // An aggregate without GROUP BY gives one row even over no rows:
        // HAVING keeps it, and so inserts the attempt, only under both limits.
        let counted = tx
            .query_opt(
                "INSERT INTO login_failures (email) \
                 SELECT $1 FROM login_failures WHERE at > now() - make_interval(secs => $2) \
                 HAVING count(*) FILTER (WHERE email = $1) < $3 AND count(*) < $4 RETURNING 1",
                &[&email, &window.as_secs_f64(), &email_limit, &service_limit],
            )
            .await?
            .is_some();
        tx.execute("DELETE FROM login_failures WHERE at < now() - interval '1 day'", &[]).await?;
        tx.commit().await?;
        Ok(counted)
    }

    async fn clear_attempts(&self, email: &str) -> Result<()> {
        self.client().await?.execute("DELETE FROM login_failures WHERE email = $1", &[&email]).await?;
        Ok(())
    }

    async fn create_session(&self, token_hash: &[u8], user_id: Uuid, ttl: Duration) -> Result<()> {
        self.client()
            .await?
            .execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ($1, $2, now() + make_interval(secs => $3))",
                &[&token_hash, &user_id, &ttl.as_secs_f64()],
            )
            .await?;
        Ok(())
    }

    async fn session_user(&self, token_hash: &[u8], ttl: Duration, max: Duration) -> Result<Option<SessionUser>> {
        let row = self
            .client()
            .await?
            .query_opt(
                "UPDATE sessions AS s SET expires_at = now() + make_interval(secs => $2) FROM users AS u \
                 WHERE s.token_hash = $1 AND u.user_id = s.user_id AND s.revoked_at IS NULL \
                 AND s.expires_at > now() AND s.created_at > now() - make_interval(secs => $3) \
                 AND u.disabled_at IS NULL RETURNING u.user_id, u.email",
                &[&token_hash, &ttl.as_secs_f64(), &max.as_secs_f64()],
            )
            .await?;
        Ok(row.map(|r| SessionUser { user_id: r.get(0), email: r.get(1) }))
    }

    async fn revoke(&self, token_hash: &[u8]) -> Result<()> {
        self.client()
            .await?
            .execute(
                "UPDATE sessions SET revoked_at = now() WHERE token_hash = $1 AND revoked_at IS NULL",
                &[&token_hash],
            )
            .await?;
        Ok(())
    }
}
