//! The page and the API it calls. Each route is one call to the platform
//! API; the product adds nothing to the data on the way.

use std::sync::Arc;
use std::time::Duration;

use axum::Router;
use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::{Method, StatusCode};
use axum::response::{Html, Response};
use axum::routing::{delete, get};
use serde::Deserialize;

use crate::platform::{self, Body, Platform};

/// The platform's limit of one file, and a little for the request around it.
const MAX_UPLOAD_BYTES: usize = 10 * 1024 * 1024 + 64 * 1024;

/// Listing, starting a chat: the platform answers at once.
const QUICK: Duration = Duration::from_secs(30);
/// An upload goes to the object store before the platform answers.
const UPLOAD: Duration = Duration::from_secs(120);
/// An answer: retrieve, up to 5 minutes of generation, record.
const ANSWER: Duration = Duration::from_secs(360);

pub fn router(platform: Arc<Platform>) -> Router {
    Router::new()
        .route("/", get(|| async { Html(include_str!("../static/index.html")) }))
        .route("/api/files", get(list_files).post(upload_file).layer(DefaultBodyLimit::max(MAX_UPLOAD_BYTES)))
        .route("/api/files/{file_id}", delete(delete_file))
        .route("/api/chats", get(list_chats).post(new_chat))
        .route("/api/chats/{chat_id}/messages", get(chat_history).post(send_message))
        .route("/healthz", get(|| async { "ok" }))
        .with_state(platform)
}

async fn list_files(State(platform): State<Arc<Platform>>) -> Response {
    platform.call(Method::GET, "/api/files", &[], None, QUICK).await
}

#[derive(Deserialize)]
struct UploadQuery {
    name: String,
}

/// The file as the body, its name in the query: the platform checks both.
async fn upload_file(State(platform): State<Arc<Platform>>, Query(q): Query<UploadQuery>, body: Bytes) -> Response {
    let body = Body { bytes: body, content_type: "application/octet-stream" };
    platform.call(Method::POST, "/api/files", &[("name", &q.name)], Some(body), UPLOAD).await
}

/// An id from the browser that goes into the platform's path must not be
/// able to change that path: ids made by the platform are UUIDs and sha256 hex.
fn refuse_path_id(field: &str, id: &str) -> Option<Response> {
    let well_formed = (1..=64).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-');
    (!well_formed).then(|| platform::error(StatusCode::BAD_REQUEST, format!("{field}: 1-64 letters, digits or '-'")))
}

async fn delete_file(State(platform): State<Arc<Platform>>, Path(file_id): Path<String>) -> Response {
    if let Some(refused) = refuse_path_id("file_id", &file_id) {
        return refused;
    }
    platform.call(Method::DELETE, &format!("/api/files/{file_id}"), &[], None, QUICK).await
}

async fn list_chats(State(platform): State<Arc<Platform>>) -> Response {
    platform.call(Method::GET, "/api/chats", &[], None, QUICK).await
}

async fn chat_history(State(platform): State<Arc<Platform>>, Path(chat_id): Path<String>) -> Response {
    if let Some(refused) = refuse_path_id("chat_id", &chat_id) {
        return refused;
    }
    platform.call(Method::GET, &format!("/api/chats/{chat_id}/messages"), &[], None, QUICK).await
}

async fn new_chat(State(platform): State<Arc<Platform>>) -> Response {
    platform.call(Method::POST, "/api/chats", &[], None, QUICK).await
}

/// `{"text": "..."}`, passed on as it came; the platform checks it.
async fn send_message(State(platform): State<Arc<Platform>>, Path(chat_id): Path<String>, body: Bytes) -> Response {
    if let Some(refused) = refuse_path_id("chat_id", &chat_id) {
        return refused;
    }
    let body = Body { bytes: body, content_type: "application/json" };
    platform.call(Method::POST, &format!("/api/chats/{chat_id}/messages"), &[], Some(body), ANSWER).await
}

#[cfg(test)]
mod tests {
    use axum::Json;
    use axum::body::Body as HttpBody;
    use axum::http::Request;
    use axum::routing::post;
    use http_body_util::BodyExt;
    use serde_json::{Value, json};
    use tower::ServiceExt;

    use super::*;

    /// A stand-in for be_service on a free port: records what reached it.
    async fn fake_platform(app: Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        url
    }

    async fn call(platform_url: &str, req: Request<HttpBody>) -> (StatusCode, Value) {
        let res = router(Arc::new(Platform::new(platform_url).unwrap())).oneshot(req).await.unwrap();
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
}
