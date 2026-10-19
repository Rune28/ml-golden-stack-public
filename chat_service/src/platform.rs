//! The platform API as the product sees it: one HTTP call per action, and
//! every way it can fail turned into a JSON `{"error": ...}` the page shows.
//!
//! - The platform answered: its status and body go to the page unchanged,
//!   so a refused file (400) or a file already being indexed (409) reads
//!   the same as in the platform.
//! - The platform did not answer in time: 504.
//! - The platform could not be reached or answered with something else: 502.

use std::time::Duration;

use anyhow::{Context, Result};
use axum::Json;
use axum::http::{Method, StatusCode};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use serde_json::json;

pub struct Platform {
    base: reqwest::Url,
    http: reqwest::Client,
}

/// A request body and its content type.
pub struct Body {
    pub bytes: Bytes,
    pub content_type: &'static str,
}

impl Platform {
    pub fn new(base: &str) -> Result<Self> {
        let base = reqwest::Url::parse(base).with_context(|| format!("PLATFORM_API_URL {base}"))?;
        Ok(Self { base, http: reqwest::Client::new() })
    }

    pub fn base(&self) -> &reqwest::Url {
        &self.base
    }

    /// `path` is fixed by the caller, never taken from a request: whatever
    /// comes from the browser goes in `query` or `body`, or is checked first.
    pub async fn call(
        &self,
        method: Method,
        path: &str,
        query: &[(&str, &str)],
        body: Option<Body>,
        timeout: Duration,
    ) -> Response {
        let url = match self.base.join(path) {
            Ok(url) => url,
            Err(e) => return error(StatusCode::INTERNAL_SERVER_ERROR, format!("bad platform path {path}: {e}")),
        };
        let mut request = self.http.request(method, url).query(query).timeout(timeout);
        if let Some(body) = body {
            request = request.header("content-type", body.content_type).body(body.bytes);
        }

        let response = match request.send().await {
            Ok(response) => response,
            Err(e) if e.is_timeout() => {
                tracing::warn!(path, "platform API timed out");
                return error(StatusCode::GATEWAY_TIMEOUT, "the platform did not answer in time".into());
            }
            Err(e) => {
                tracing::warn!(path, error = %e, "platform API unreachable");
                return error(StatusCode::BAD_GATEWAY, "the platform is unreachable".into());
            }
        };

        let status = response.status();
        match response.bytes().await {
            // The platform always answers JSON; anything else is not passed on.
            Ok(bytes) if serde_json::from_slice::<serde_json::Value>(&bytes).is_ok() => {
                (status, [("content-type", "application/json")], bytes).into_response()
            }
            Ok(_) => {
                tracing::warn!(path, %status, "platform API answered without JSON");
                error(StatusCode::BAD_GATEWAY, format!("the platform answered {status} without a JSON body"))
            }
            Err(e) => {
                tracing::warn!(path, error = %e, "platform API answer cut off");
                error(StatusCode::BAD_GATEWAY, "the platform answer was cut off".into())
            }
        }
    }
}

pub fn error(status: StatusCode, message: String) -> Response {
    (status, Json(json!({ "error": message }))).into_response()
}
