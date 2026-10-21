//! The platform API as the product sees it: one HTTP call per action, made
//! for a user with a token that names them (token.rs), and every way it can
//! fail turned into a JSON `{"error": ...}` the page shows.
//!
//! - The platform answered: its status and body go to the page unchanged,
//!   so a refused file (400) or a file already being indexed (409) reads
//!   the same as in the platform.
//! - The platform refused the token of this service (401): 502. To the page
//!   a 401 means its session ended and it shows the login form; logging in
//!   again would not help when the keys or the clocks of the two services differ.
//! - The platform did not answer in time: 504.
//! - The platform could not be reached or answered with something else: 502.

use std::time::Duration;

use anyhow::{Context, Result};
use axum::Json;
use axum::http::{Method, StatusCode};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use serde_json::json;
use uuid::Uuid;

use crate::token::{self, Signer};

pub struct Platform {
    base: reqwest::Url,
    http: reqwest::Client,
    /// None only in tests of this service, where the fake platform checks no token.
    signer: Option<Signer>,
}

/// A request body and its content type.
pub struct Body {
    pub bytes: Bytes,
    pub content_type: &'static str,
}

impl Platform {
    pub fn new(base: &str, signer: Option<Signer>) -> Result<Self> {
        let base = reqwest::Url::parse(base).with_context(|| format!("PLATFORM_API_URL {base}"))?;
        Ok(Self { base, http: reqwest::Client::new(), signer })
    }

    pub fn base(&self) -> &reqwest::Url {
        &self.base
    }

    /// `path` is fixed by the caller, never taken from a request: whatever
    /// comes from the browser goes in `query` or `body`, or is checked first.
    /// `user` is the logged-in user the call is made for.
    pub async fn call(
        &self,
        user: Uuid,
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
        if let Some(signer) = &self.signer {
            match signer.issue(user, token::TTL) {
                Ok(jwt) => request = request.bearer_auth(jwt),
                Err(e) => return error(StatusCode::INTERNAL_SERVER_ERROR, format!("platform token: {e:#}")),
            }
        }
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
        if status == StatusCode::UNAUTHORIZED {
            tracing::error!(path, "the platform refused the platform token: keys or clocks differ");
            return error(StatusCode::BAD_GATEWAY, "the platform refused this service's token".into());
        }
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
