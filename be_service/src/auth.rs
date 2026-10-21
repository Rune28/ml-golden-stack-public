//! Who a request of a product route is for: the platform token.
//!
//! A JWT signed with Ed25519 by its issuer (chat_service now, an identity
//! provider later): `sub` is the user_id, `aud` the platform. be_service
//! holds only the public key, from PLATFORM_TOKEN_PUBLIC_KEY, and checks the
//! signature, the audience, the issuer and the expiry of every request; it
//! cannot make a token itself. A product route takes a `User` and is refused
//! with 401 without a valid token. A copy of the claims is in
//! chat_service/src/token.rs: a change to one is a change to both.

use std::sync::Arc;

use anyhow::{Context, Result};
use axum::extract::FromRequestParts;
use axum::http::header::AUTHORIZATION;
use axum::http::request::Parts;
use jsonwebtoken::{Algorithm, DecodingKey, Validation};
use serde::Deserialize;

use crate::api::{ApiError, AppState};

pub const ISSUER: &str = "chat-service";
pub const AUDIENCE: &str = "platform";
/// Clocks of the issuer and of this service may differ a little.
const LEEWAY_SECONDS: u64 = 30;

#[derive(Deserialize)]
struct Claims {
    sub: String,
}

pub struct Verifier {
    key: DecodingKey,
    validation: Validation,
}

impl Verifier {
    pub fn from_pem(pem: &str) -> Result<Self> {
        let key = DecodingKey::from_ed_pem(pem.as_bytes()).context("PLATFORM_TOKEN_PUBLIC_KEY: an Ed25519 PEM key")?;
        let mut validation = Validation::new(Algorithm::EdDSA);
        validation.set_audience(&[AUDIENCE]);
        validation.set_issuer(&[ISSUER]);
        validation.set_required_spec_claims(&["exp", "aud", "iss", "sub"]);
        validation.leeway = LEEWAY_SECONDS;
        Ok(Self { key, validation })
    }

    pub fn from_env() -> Result<Self> {
        Self::from_pem(&std::env::var("PLATFORM_TOKEN_PUBLIC_KEY").context("PLATFORM_TOKEN_PUBLIC_KEY is not set")?)
    }

    /// The user_id of a valid token: a UUID, made by the issuer.
    pub fn user(&self, token: &str) -> Option<String> {
        let claims = jsonwebtoken::decode::<Claims>(token, &self.key, &self.validation).ok()?.claims;
        uuid::Uuid::parse_str(&claims.sub).ok().map(|id| id.to_string())
    }
}

/// The user a request is for; a handler that takes it needs a valid token.
pub struct User(pub String);

impl FromRequestParts<Arc<AppState>> for User {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &Arc<AppState>) -> Result<Self, Self::Rejection> {
        let token = parts
            .headers
            .get(AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .ok_or_else(|| ApiError::Unauthorized("a platform token is required".into()))?;
        state
            .verifier
            .user(token)
            .map(User)
            .ok_or_else(|| ApiError::Unauthorized("the platform token is not valid".into()))
    }
}

#[cfg(test)]
pub mod tests {
    use std::time::{SystemTime, UNIX_EPOCH};

    use jsonwebtoken::{EncodingKey, Header};
    use serde_json::json;

    use super::*;

    /// A key pair for tests only (the same as chat_service's tests); the
    /// cluster's is made by OpenTofu.
    pub const PRIVATE: &str = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIPjbWyeianVbvMzsaRag/CXwkHn6yFn+yBkiDDfzMFrd\n-----END PRIVATE KEY-----\n";
    pub const PUBLIC: &str = "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAMSkHjQsWr+8ukXVqCRuPLqS43S31qOc+KJZusUC6ywE=\n-----END PUBLIC KEY-----\n";
    /// Another key: a token it signs must be refused.
    const OTHER_PRIVATE: &str = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIEnaweMkShc4R/E2bk0lbX+deAibPUHBB1OvCKeG7Hwe\n-----END PRIVATE KEY-----\n";

    pub const USER: &str = "01a15e0c-c672-769b-8916-345554c7d79d";

    pub fn token_with(private: &str, sub: &str, aud: &str, iss: &str, exp_in: i64) -> String {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
        let claims = json!({ "sub": sub, "aud": aud, "iss": iss, "iat": now, "exp": now + exp_in });
        jsonwebtoken::encode(
            &Header::new(Algorithm::EdDSA),
            &claims,
            &EncodingKey::from_ed_pem(private.as_bytes()).unwrap(),
        )
        .unwrap()
    }

    pub fn token(user: &str) -> String {
        token_with(PRIVATE, user, AUDIENCE, ISSUER, 300)
    }

    #[test]
    fn a_token_of_the_issuer_names_its_user() {
        let verifier = Verifier::from_pem(PUBLIC).unwrap();
        assert_eq!(verifier.user(&token(USER)).as_deref(), Some(USER));
    }

    #[test]
    fn a_token_that_is_not_for_the_platform_or_not_valid_names_no_one() {
        let verifier = Verifier::from_pem(PUBLIC).unwrap();
        for (name, token) in [
            ("expired", token_with(PRIVATE, USER, AUDIENCE, ISSUER, -120)),
            ("another audience", token_with(PRIVATE, USER, "mlflow", ISSUER, 300)),
            ("another issuer", token_with(PRIVATE, USER, AUDIENCE, "someone", 300)),
            ("another key", token_with(OTHER_PRIVATE, USER, AUDIENCE, ISSUER, 300)),
            ("not a user_id", token_with(PRIVATE, "admin", AUDIENCE, ISSUER, 300)),
            ("not a token", "abc.def.ghi".to_owned()),
        ] {
            assert_eq!(verifier.user(&token), None, "{name}");
        }
    }
}
