//! The login window behind the page: logging in and out, who is logged in,
//! the rules of the fields, and the guard every request of the API passes.
//!
//! The guard, in this order:
//! - a request that changes something (not GET) must come from the page's
//!   own origin, or it is refused with 403;
//! - the rules and the login itself are open; every other route of the API
//!   needs a valid session, or it is refused with 401, and the handler finds
//!   the user in the request's extensions.

use std::sync::Arc;

use axum::Json;
use axum::body::Bytes;
use axum::extract::{Request, State};
use axum::http::header::{COOKIE, ORIGIN, SET_COOKIE};
use axum::http::{Method, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use serde_json::json;

use crate::api::App;
use crate::auth::{self, EMAIL_FAILURES, FAILURE_WINDOW, SERVICE_FAILURES, SESSION_MAX, SESSION_TTL};
use crate::platform::error;
use crate::store::{AuthStore, SessionUser};

/// Routes of the API open without a session.
const OPEN: [&str; 2] = ["/api/auth/rules", "/api/auth/login"];

/// Password checks at once: 19 MiB of argon2 each, within the memory limit
/// of the pod next to an upload on its way (infra/charts/chat-service).
pub const PASSWORD_CHECKS: usize = 2;
/// How long a login waits for a check to free up before it is refused (503).
const CHECK_WAIT: std::time::Duration = std::time::Duration::from_secs(2);

/// The logged-in user of a request and its session.
#[derive(Clone)]
pub struct Session {
    pub user: SessionUser,
    pub token_hash: Vec<u8>,
}

pub async fn guard<S: AuthStore>(State(app): State<Arc<App<S>>>, mut req: Request, next: Next) -> Response {
    let path = req.uri().path().to_owned();
    if !path.starts_with("/api/") {
        return next.run(req).await;
    }
    let changes = !matches!(*req.method(), Method::GET | Method::HEAD);
    let origin = req.headers().get(ORIGIN).and_then(|v| v.to_str().ok());
    if changes && !auth::origin_allowed(origin, &app.origins) {
        return error(StatusCode::FORBIDDEN, "the request does not come from the page".into());
    }
    if OPEN.contains(&path.as_str()) {
        return next.run(req).await;
    }
    let token = req.headers().get(COOKIE).and_then(|v| v.to_str().ok()).and_then(auth::token_from_cookies);
    let Some(token) = token else {
        return error(StatusCode::UNAUTHORIZED, "login required".into());
    };
    let token_hash = auth::token_hash(token);
    match app.store.session_user(&token_hash, SESSION_TTL, SESSION_MAX).await {
        Ok(Some(user)) => {
            req.extensions_mut().insert(Session { user, token_hash });
            next.run(req).await
        }
        Ok(None) => error(StatusCode::UNAUTHORIZED, "login required".into()),
        Err(e) => {
            tracing::error!(error = format!("{e:#}"), "session check");
            error(StatusCode::SERVICE_UNAVAILABLE, "sessions are unavailable, try again".into())
        }
    }
}

pub async fn rules() -> Json<serde_json::Value> {
    Json(auth::rules())
}

#[derive(Deserialize)]
struct Credentials {
    email: String,
    password: String,
}

/// The same answer for an unknown email, a wrong password and a disabled
/// user: the login does not tell which emails exist.
const WRONG: &str = "wrong email or password";

pub async fn login<S: AuthStore>(State(app): State<Arc<App<S>>>, body: Bytes) -> Response {
    let Ok(creds) = serde_json::from_slice::<Credentials>(&body) else {
        return error(StatusCode::BAD_REQUEST, "expected {\"email\": ..., \"password\": ...}".into());
    };
    let email = auth::normalize_email(&creds.email);
    // The same checks as the page: a field the page would not send is refused
    // before anything is counted or hashed. Only the bounds of a password:
    // whether it is the email is a rule for setting one.
    if let Err(message) = auth::check_email(&email) {
        return error(StatusCode::BAD_REQUEST, message);
    }
    if !(auth::PASSWORD_MIN..=auth::PASSWORD_MAX).contains(&creds.password.chars().count()) {
        return error(StatusCode::BAD_REQUEST, WRONG.into());
    }

    // The attempt is counted before the password is checked, and stays
    // counted unless the login succeeds: a failure is counted even when
    // something below fails, and guesses sent in parallel are refused from
    // the sixth on, not after all of them are checked.
    match app.store.claim_attempt(&email, FAILURE_WINDOW, EMAIL_FAILURES, SERVICE_FAILURES).await {
        Ok(true) => {}
        Ok(false) => {
            return error(StatusCode::TOO_MANY_REQUESTS, "too many failed logins: try again in 15 minutes".into());
        }
        Err(e) => return unavailable(e),
    }
    let account = match app.store.account(&email).await {
        Ok(account) => account,
        Err(e) => return unavailable(e),
    };

    // argon2 takes 19 MiB and tens of milliseconds of CPU per check: a few at
    // a time, off the async threads, or a burst of logins takes more memory
    // than the pod has. The permit moves into the check, so it is held until
    // the check ends even if the client goes away first.
    let permit = match tokio::time::timeout(CHECK_WAIT, app.checks.clone().acquire_owned()).await {
        Ok(Ok(permit)) => permit,
        _ => return error(StatusCode::SERVICE_UNAVAILABLE, "too many logins at once, try again".into()),
    };
    // An unknown email is checked against a hash no password matches, so it
    // takes as long as a wrong password.
    let phc = account.as_ref().map_or_else(|| auth::dummy_hash().to_owned(), |a| a.secret.clone());
    let password = creds.password;
    let check = move || {
        let _permit = permit;
        auth::verify_password(&password, &phc)
    };
    let matches = tokio::task::spawn_blocking(check).await.unwrap_or(false);
    let Some(account) = account.filter(|a| matches && !a.disabled) else {
        return error(StatusCode::UNAUTHORIZED, WRONG.into());
    };

    let token = auth::new_token();
    if let Err(e) = app.store.create_session(&auth::token_hash(&token), account.user_id, SESSION_TTL).await {
        return unavailable(e);
    }
    if let Err(e) = app.store.clear_attempts(&email).await {
        tracing::warn!(error = format!("{e:#}"), "clearing failed logins");
    }
    tracing::info!(user_id = %account.user_id, "logged in");
    let body = Json(json!({ "user_id": account.user_id, "email": account.email }));
    ([(SET_COOKIE, auth::session_cookie(&token))], body).into_response()
}

pub async fn logout<S: AuthStore>(State(app): State<Arc<App<S>>>, session: axum::Extension<Session>) -> Response {
    if let Err(e) = app.store.revoke(&session.token_hash).await {
        return unavailable(e);
    }
    tracing::info!(user_id = %session.user.user_id, "logged out");
    ([(SET_COOKIE, auth::cleared_cookie())], Json(json!({ "logged_out": true }))).into_response()
}

pub async fn me(session: axum::Extension<Session>) -> Json<serde_json::Value> {
    Json(json!({ "user_id": session.user.user_id, "email": session.user.email }))
}

fn unavailable(e: anyhow::Error) -> Response {
    tracing::error!(error = format!("{e:#}"), "login store");
    error(StatusCode::SERVICE_UNAVAILABLE, "login is unavailable, try again".into())
}

#[cfg(test)]
pub mod tests {
    use std::collections::HashMap;
    use std::sync::Mutex;
    use std::time::Duration;

    use anyhow::Result;
    use axum::body::Body as HttpBody;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use serde_json::Value;
    use tower::ServiceExt;
    use uuid::Uuid;

    use super::*;
    use crate::api::router;
    use crate::platform::Platform;
    use crate::store::Account;

    pub const ORIGIN_OK: &str = "http://127.0.0.1:9002";
    pub const PASSWORD: &str = "correct horse battery";

    /// Users, sessions and login attempts in memory. Expiry is not modelled:
    /// a session is valid until revoked, and every attempt is in the window.
    #[derive(Default)]
    pub struct FakeStore {
        pub accounts: Mutex<HashMap<String, Account>>,
        pub sessions: Mutex<HashMap<Vec<u8>, (SessionUser, bool)>>,
        pub failures: Mutex<Vec<String>>,
    }

    impl FakeStore {
        pub fn with_user(email: &str) -> Self {
            let store = Self::default();
            let account = Account {
                user_id: Uuid::now_v7(),
                email: email.into(),
                secret: auth::hash_password(PASSWORD),
                disabled: false,
            };
            store.accounts.lock().unwrap().insert(email.into(), account);
            store
        }

        /// A session of the user, as if they had logged in: its token.
        pub fn session(&self, email: &str) -> String {
            let account = self.accounts.lock().unwrap()[email].clone();
            let token = auth::new_token();
            let user = SessionUser { user_id: account.user_id, email: account.email };
            self.sessions.lock().unwrap().insert(auth::token_hash(&token), (user, false));
            token
        }
    }

    impl AuthStore for FakeStore {
        async fn account(&self, email: &str) -> Result<Option<Account>> {
            Ok(self.accounts.lock().unwrap().get(email).cloned())
        }
        async fn claim_attempt(
            &self,
            email: &str,
            _window: Duration,
            email_limit: i64,
            service_limit: i64,
        ) -> Result<bool> {
            let mut all = self.failures.lock().unwrap();
            let mine = all.iter().filter(|e| *e == email).count() as i64;
            let counted = mine < email_limit && (all.len() as i64) < service_limit;
            if counted {
                all.push(email.into());
            }
            Ok(counted)
        }
        async fn clear_attempts(&self, email: &str) -> Result<()> {
            self.failures.lock().unwrap().retain(|e| e != email);
            Ok(())
        }
        async fn create_session(&self, token_hash: &[u8], user_id: Uuid, _ttl: Duration) -> Result<()> {
            let email = self.accounts.lock().unwrap().values().find(|a| a.user_id == user_id).unwrap().email.clone();
            self.sessions.lock().unwrap().insert(token_hash.to_vec(), (SessionUser { user_id, email }, false));
            Ok(())
        }
        async fn session_user(&self, token_hash: &[u8], _ttl: Duration, _max: Duration) -> Result<Option<SessionUser>> {
            Ok(self.sessions.lock().unwrap().get(token_hash).filter(|(_, revoked)| !revoked).map(|(u, _)| u.clone()))
        }
        async fn revoke(&self, token_hash: &[u8]) -> Result<()> {
            if let Some(s) = self.sessions.lock().unwrap().get_mut(token_hash) {
                s.1 = true;
            }
            Ok(())
        }
    }

    pub fn app(store: FakeStore, platform_url: &str) -> Arc<App<FakeStore>> {
        with_checks(store, platform_url, PASSWORD_CHECKS)
    }

    fn with_checks(store: FakeStore, platform_url: &str, checks: usize) -> Arc<App<FakeStore>> {
        Arc::new(App {
            platform: Platform::new(platform_url, None).unwrap(),
            store,
            origins: vec![ORIGIN_OK.into()],
            checks: Arc::new(tokio::sync::Semaphore::new(checks)),
        })
    }

    /// The answer of the router: status, the Set-Cookie header, the JSON body.
    pub async fn send(app: &Arc<App<FakeStore>>, req: Request<HttpBody>) -> (StatusCode, Option<String>, Value) {
        let res = router(app.clone()).oneshot(req).await.unwrap();
        let status = res.status();
        let cookie = res.headers().get(SET_COOKIE).map(|v| v.to_str().unwrap().to_owned());
        let body = res.into_body().collect().await.unwrap().to_bytes();
        (status, cookie, serde_json::from_slice(&body).unwrap_or(Value::Null))
    }

    fn login_request(email: &str, password: &str) -> Request<HttpBody> {
        let body = json!({ "email": email, "password": password }).to_string();
        Request::post("/api/auth/login")
            .header("origin", ORIGIN_OK)
            .header("content-type", "application/json")
            .body(HttpBody::from(body))
            .unwrap()
    }

    fn with_session(path: &str, token: &str) -> Request<HttpBody> {
        Request::get(path).header("cookie", format!("session={token}")).body(HttpBody::empty()).unwrap()
    }

    #[tokio::test]
    async fn a_login_sets_a_strict_session_cookie_that_opens_the_api() {
        let app = app(FakeStore::with_user("ann@example.org"), "http://127.0.0.1:9");

        let (status, cookie, body) = send(&app, login_request(" Ann@Example.org ", PASSWORD)).await;

        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["email"], "ann@example.org");
        let cookie = cookie.unwrap();
        assert!(cookie.contains("HttpOnly") && cookie.contains("SameSite=Strict") && cookie.contains("Secure"));
        let token = auth::token_from_cookies(cookie.split(';').next().unwrap()).unwrap().to_owned();
        let (status, _, me) = send(&app, with_session("/api/auth/me", &token)).await;
        assert_eq!((status, me["email"].as_str()), (StatusCode::OK, Some("ann@example.org")));
    }

    #[tokio::test]
    async fn an_unknown_email_and_a_wrong_password_look_the_same() {
        let app = app(FakeStore::with_user("ann@example.org"), "http://127.0.0.1:9");

        let (s1, c1, wrong_password) = send(&app, login_request("ann@example.org", "not the password!")).await;
        let (s2, c2, unknown_email) = send(&app, login_request("bob@example.org", PASSWORD)).await;

        assert_eq!((s1, s2), (StatusCode::UNAUTHORIZED, StatusCode::UNAUTHORIZED));
        assert_eq!(wrong_password, unknown_email);
        assert!(c1.is_none() && c2.is_none());
    }

    #[tokio::test]
    async fn the_sixth_failed_login_of_an_email_is_refused_even_with_the_right_password() {
        let app = app(FakeStore::with_user("ann@example.org"), "http://127.0.0.1:9");
        for _ in 0..EMAIL_FAILURES {
            let (status, _, _) = send(&app, login_request("ann@example.org", "not the password!")).await;
            assert_eq!(status, StatusCode::UNAUTHORIZED);
        }

        let (status, cookie, body) = send(&app, login_request("ann@example.org", PASSWORD)).await;

        assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
        assert!(cookie.is_none() && body["error"].as_str().unwrap().contains("15 minutes"));
    }

    // Time is paused: the wait for a free check passes at once.
    #[tokio::test(start_paused = true)]
    async fn a_login_waits_for_a_free_password_check_and_counts_as_an_attempt_meanwhile() {
        // Every check taken, as by a burst of logins in flight.
        let app = with_checks(FakeStore::with_user("ann@example.org"), "http://127.0.0.1:9", 0);

        let (status, cookie, body) = send(&app, login_request("ann@example.org", PASSWORD)).await;

        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert!(cookie.is_none() && body["error"].as_str().unwrap().contains("at once"));
        // Counted before the check, so a burst cannot outrun the limit.
        assert_eq!(*app.store.failures.lock().unwrap(), ["ann@example.org"]);
    }

    #[tokio::test]
    async fn a_successful_login_clears_the_failures_of_its_email() {
        let app = app(FakeStore::with_user("ann@example.org"), "http://127.0.0.1:9");
        send(&app, login_request("ann@example.org", "not the password!")).await;

        send(&app, login_request("ann@example.org", PASSWORD)).await;

        assert!(app.store.failures.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_disabled_user_cannot_log_in() {
        let store = FakeStore::with_user("ann@example.org");
        store.accounts.lock().unwrap().get_mut("ann@example.org").unwrap().disabled = true;
        let app = app(store, "http://127.0.0.1:9");

        let (status, _, body) = send(&app, login_request("ann@example.org", PASSWORD)).await;

        assert_eq!((status, body["error"].as_str()), (StatusCode::UNAUTHORIZED, Some(WRONG)));
    }

    #[tokio::test]
    async fn fields_the_page_would_not_send_are_refused_before_counting() {
        let app = app(FakeStore::with_user("ann@example.org"), "http://127.0.0.1:9");

        let (status, _, body) = send(&app, login_request("not an email", PASSWORD)).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(body["error"].as_str().unwrap().starts_with("email:"));
        let (status, _, _) = send(&app, login_request("ann@example.org", &"x".repeat(129))).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(app.store.failures.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn without_a_valid_session_the_api_is_closed_but_the_page_and_rules_are_open() {
        let store = FakeStore::with_user("ann@example.org");
        let token = store.session("ann@example.org");
        let app = app(store, "http://127.0.0.1:9");

        let (status, _, _) = send(&app, Request::get("/api/files").body(HttpBody::empty()).unwrap()).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        let (status, _, _) = send(&app, with_session("/api/files", "not-a-session")).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        let (status, _, rules) = send(&app, Request::get("/api/auth/rules").body(HttpBody::empty()).unwrap()).await;
        assert_eq!((status, rules["password"]["min"].as_u64()), (StatusCode::OK, Some(12)));
        let res = router(app.clone()).oneshot(Request::get("/").body(HttpBody::empty()).unwrap()).await.unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        // With the session: past the guard, on to the (unreachable) platform.
        let (status, _, _) = send(&app, with_session("/api/files", &token)).await;
        assert_eq!(status, StatusCode::BAD_GATEWAY);
    }

    #[tokio::test]
    async fn logout_ends_the_session_for_every_tab() {
        let store = FakeStore::with_user("ann@example.org");
        let token = store.session("ann@example.org");
        let app = app(store, "http://127.0.0.1:9");
        let logout = Request::post("/api/auth/logout")
            .header("origin", ORIGIN_OK)
            .header("cookie", format!("session={token}"))
            .body(HttpBody::empty())
            .unwrap();

        let (status, cookie, _) = send(&app, logout).await;

        assert_eq!(status, StatusCode::OK);
        assert!(cookie.unwrap().contains("Max-Age=0"));
        // Another tab holds the same cookie: its next call is refused.
        let (status, _, _) = send(&app, with_session("/api/auth/me", &token)).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn a_change_from_another_origin_is_refused_even_with_a_session() {
        let store = FakeStore::with_user("ann@example.org");
        let token = store.session("ann@example.org");
        let app = app(store, "http://127.0.0.1:9");
        for origin in [Some("http://evil.example"), None] {
            let mut req = Request::post("/api/chats").header("cookie", format!("session={token}"));
            if let Some(origin) = origin {
                req = req.header("origin", origin);
            }
            let (status, _, _) = send(&app, req.body(HttpBody::empty()).unwrap()).await;
            assert_eq!(status, StatusCode::FORBIDDEN, "{origin:?}");
        }
        let login = Request::post("/api/auth/login").header("origin", "http://evil.example").body(HttpBody::from("{}"));
        let (status, _, _) = send(&app, login.unwrap()).await;
        assert_eq!(status, StatusCode::FORBIDDEN);
    }
}
