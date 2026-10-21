//! The page and the API it calls. The login routes are this service's own
//! (login.rs); every other route is one call to the platform API, made for
//! the logged-in user, and the product adds nothing to the data on the way.

use std::sync::Arc;
use std::time::Duration;

use axum::Extension;
use axum::Router;
use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::{Method, StatusCode};
use axum::middleware;
use axum::response::{Html, Response};
use axum::routing::{delete, get, post};
use serde::Deserialize;
use tokio::sync::Semaphore;

use crate::login::{self, Session};
use crate::platform::{self, Body, Platform};
use crate::store::AuthStore;

/// What every request reaches: the platform API, the store of users and
/// sessions, the origins the page is served from, and the password checks
/// that may run at once (login.rs).
pub struct App<S> {
    pub platform: Platform,
    pub store: S,
    pub origins: Vec<String>,
    pub checks: Arc<Semaphore>,
}

/// The platform's limit of one file, and a little for the request around it.
const MAX_UPLOAD_BYTES: usize = 10 * 1024 * 1024 + 64 * 1024;

/// Listing, starting a chat: the platform answers at once.
const QUICK: Duration = Duration::from_secs(30);
/// An upload goes to the object store before the platform answers.
const UPLOAD: Duration = Duration::from_secs(120);
/// An answer: retrieve, up to 5 minutes of generation, record.
const ANSWER: Duration = Duration::from_secs(360);

pub fn router<S: AuthStore>(app: Arc<App<S>>) -> Router {
    Router::new()
        .route("/", get(|| async { Html(include_str!("../static/index.html")) }))
        .route("/api/auth/rules", get(login::rules))
        .route("/api/auth/login", post(login::login::<S>))
        .route("/api/auth/logout", post(login::logout::<S>))
        .route("/api/auth/me", get(login::me))
        .route("/api/files", get(list_files::<S>).post(upload_file::<S>).layer(DefaultBodyLimit::max(MAX_UPLOAD_BYTES)))
        .route("/api/files/{file_id}", delete(delete_file::<S>))
        .route("/api/documents", get(list_documents::<S>))
        .route("/api/chats", get(list_chats::<S>).post(new_chat::<S>))
        .route("/api/chats/{chat_id}/messages", get(chat_history::<S>).post(send_message::<S>))
        .route("/healthz", get(|| async { "ok" }))
        .layer(middleware::from_fn_with_state(app.clone(), login::guard::<S>))
        .with_state(app)
}

async fn list_files<S: AuthStore>(State(app): State<Arc<App<S>>>, Extension(session): Extension<Session>) -> Response {
    app.platform.call(session.user.user_id, Method::GET, "/api/files", &[], None, QUICK).await
}

#[derive(Deserialize)]
struct UploadQuery {
    name: String,
}

/// The file as the body, its name in the query: the platform checks both.
async fn upload_file<S: AuthStore>(
    State(app): State<Arc<App<S>>>,
    Extension(session): Extension<Session>,
    Query(q): Query<UploadQuery>,
    body: Bytes,
) -> Response {
    let body = Body { bytes: body, content_type: "application/octet-stream" };
    app.platform.call(session.user.user_id, Method::POST, "/api/files", &[("name", &q.name)], Some(body), UPLOAD).await
}

/// An id from the browser that goes into the platform's path must not be
/// able to change that path: ids made by the platform are UUIDs and sha256 hex.
fn refuse_path_id(field: &str, id: &str) -> Option<Response> {
    let well_formed = (1..=64).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-');
    (!well_formed).then(|| platform::error(StatusCode::BAD_REQUEST, format!("{field}: 1-64 letters, digits or '-'")))
}

async fn list_documents<S: AuthStore>(
    State(app): State<Arc<App<S>>>,
    Extension(session): Extension<Session>,
) -> Response {
    app.platform.call(session.user.user_id, Method::GET, "/api/documents", &[], None, QUICK).await
}

async fn delete_file<S: AuthStore>(
    State(app): State<Arc<App<S>>>,
    Extension(session): Extension<Session>,
    Path(file_id): Path<String>,
) -> Response {
    if let Some(refused) = refuse_path_id("file_id", &file_id) {
        return refused;
    }
    app.platform.call(session.user.user_id, Method::DELETE, &format!("/api/files/{file_id}"), &[], None, QUICK).await
}

async fn list_chats<S: AuthStore>(State(app): State<Arc<App<S>>>, Extension(session): Extension<Session>) -> Response {
    app.platform.call(session.user.user_id, Method::GET, "/api/chats", &[], None, QUICK).await
}

async fn chat_history<S: AuthStore>(
    State(app): State<Arc<App<S>>>,
    Extension(session): Extension<Session>,
    Path(chat_id): Path<String>,
) -> Response {
    if let Some(refused) = refuse_path_id("chat_id", &chat_id) {
        return refused;
    }
    app.platform
        .call(session.user.user_id, Method::GET, &format!("/api/chats/{chat_id}/messages"), &[], None, QUICK)
        .await
}

async fn new_chat<S: AuthStore>(State(app): State<Arc<App<S>>>, Extension(session): Extension<Session>) -> Response {
    app.platform.call(session.user.user_id, Method::POST, "/api/chats", &[], None, QUICK).await
}

/// `{"text": "..."}`, passed on as it came; the platform checks it.
async fn send_message<S: AuthStore>(
    State(app): State<Arc<App<S>>>,
    Extension(session): Extension<Session>,
    Path(chat_id): Path<String>,
    body: Bytes,
) -> Response {
    if let Some(refused) = refuse_path_id("chat_id", &chat_id) {
        return refused;
    }
    let body = Body { bytes: body, content_type: "application/json" };
    app.platform
        .call(session.user.user_id, Method::POST, &format!("/api/chats/{chat_id}/messages"), &[], Some(body), ANSWER)
        .await
}

#[cfg(test)]
mod tests {
    use axum::Json;
    use axum::body::Body as HttpBody;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use serde_json::{Value, json};
    use tower::ServiceExt;

    use super::*;
    use crate::login::tests::{FakeStore, ORIGIN_OK, app};

    /// A stand-in for be_service on a free port: records what reached it.
    async fn fake_platform(app: Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        url
    }

    /// A call of a logged-in user from the page: a session cookie and the page's origin.
    async fn call(platform_url: &str, mut req: Request<HttpBody>) -> (StatusCode, Value) {
        let store = FakeStore::with_user("ann@example.org");
        let token = store.session("ann@example.org");
        req.headers_mut().insert("cookie", format!("session={token}").parse().unwrap());
        req.headers_mut().insert("origin", ORIGIN_OK.parse().unwrap());
        let res = router(app(store, platform_url)).oneshot(req).await.unwrap();
        let status = res.status();
        let body = res.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&body).unwrap())
    }

    fn post_json(path: &str, body: &'static str) -> Request<HttpBody> {
        Request::post(path).header("content-type", "application/json").body(HttpBody::from(body)).unwrap()
    }

    #[tokio::test]
    async fn passes_the_platform_answer_and_its_errors_through() {
        let url = fake_platform(
            Router::new()
                .route(
                    "/api/chats/{id}/messages",
                    post(
                        |Path(id): Path<String>, body: String| async move { Json(json!({ "chat": id, "got": body })) },
                    ),
                )
                .route(
                    "/api/files",
                    post(|| async { (StatusCode::CONFLICT, Json(json!({ "error": "already indexing" }))) }),
                ),
        )
        .await;

        let (status, answer) = call(&url, post_json("/api/chats/c-1/messages", r#"{"text":"hi"}"#)).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(answer, json!({ "chat": "c-1", "got": r#"{"text":"hi"}"# }));

        let (status, error) = call(&url, post_json("/api/files?name=a.md", "text")).await;
        assert_eq!((status, error), (StatusCode::CONFLICT, json!({ "error": "already indexing" })));
    }

    #[tokio::test]
    async fn an_unreachable_platform_is_a_502_with_a_message() {
        // Nothing listens on port 9.
        let (status, error) =
            call("http://127.0.0.1:9", Request::get("/api/files").body(HttpBody::empty()).unwrap()).await;
        assert_eq!(status, StatusCode::BAD_GATEWAY);
        assert_eq!(error["error"], "the platform is unreachable");
    }

    #[tokio::test]
    async fn a_refused_platform_token_is_not_the_end_of_the_users_session() {
        let url = fake_platform(Router::new().route(
            "/api/files",
            get(|| async { (StatusCode::UNAUTHORIZED, Json(json!({ "error": "the platform token is not valid" }))) }),
        ))
        .await;

        let (status, error) = call(&url, Request::get("/api/files").body(HttpBody::empty()).unwrap()).await;

        // Not 401: the page would show the login form, and logging in again would not help.
        assert_eq!(status, StatusCode::BAD_GATEWAY);
        assert!(error["error"].as_str().unwrap().contains("refused this service's token"));
    }

    #[tokio::test]
    async fn an_answer_that_is_not_json_is_not_passed_on() {
        let url = fake_platform(Router::new().route("/api/files", get(|| async { "<html>proxy error</html>" }))).await;

        let (status, error) = call(&url, Request::get("/api/files").body(HttpBody::empty()).unwrap()).await;

        assert_eq!(status, StatusCode::BAD_GATEWAY);
        assert!(error["error"].as_str().unwrap().contains("without a JSON body"));
    }

    #[tokio::test]
    async fn a_chat_id_cannot_change_the_platform_path() {
        // Decoded by the router to "../files": refused before any call.
        let (status, _) = call("http://127.0.0.1:9", post_json("/api/chats/..%2Ffiles/messages", "{}")).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        let history = Request::get("/api/chats/..%2Ffiles/messages").body(HttpBody::empty()).unwrap();
        let (status, _) = call("http://127.0.0.1:9", history).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        let delete = Request::delete("/api/files/..%2Fchats").body(HttpBody::empty()).unwrap();
        let (status, _) = call("http://127.0.0.1:9", delete).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn every_call_to_the_platform_carries_a_token_naming_the_user() {
        use axum::http::HeaderMap;

        let url = fake_platform(Router::new().route(
            "/api/files",
            get(|headers: HeaderMap| async move {
                let auth = headers.get("authorization").and_then(|v| v.to_str().ok()).unwrap_or("").to_owned();
                Json(json!({ "authorization": auth }))
            }),
        ))
        .await;
        let store = FakeStore::with_user("ann@example.org");
        let token = store.session("ann@example.org");
        let user_id = store.accounts.lock().unwrap()["ann@example.org"].user_id;
        let signer = crate::token::Signer::from_pem(crate::token::tests::PRIVATE).unwrap();
        let app = Arc::new(App {
            platform: Platform::new(&url, Some(signer)).unwrap(),
            store,
            origins: vec![ORIGIN_OK.into()],
            checks: Arc::new(Semaphore::new(login::PASSWORD_CHECKS)),
        });
        let req = Request::get("/api/files").header("cookie", format!("session={token}")).body(HttpBody::empty());

        let res = router(app).oneshot(req.unwrap()).await.unwrap();

        let body: Value = serde_json::from_slice(&res.into_body().collect().await.unwrap().to_bytes()).unwrap();
        let jwt = body["authorization"].as_str().unwrap().strip_prefix("Bearer ").unwrap();
        assert_eq!(crate::token::tests::verify(jwt).unwrap().sub, user_id.to_string());
    }
}
