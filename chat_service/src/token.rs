//! The platform token: who the user is, for the platform API (be_service).
//!
//! A JWT signed with Ed25519 (EdDSA), made for every call: `sub` is the
//! user_id, `aud` the platform, `iss` this service, valid for 5 minutes.
//! This service holds the private key; be_service has only the public one,
//! checks it on every request and cannot make a token itself. An identity
//! provider later issues such tokens instead, and be_service takes its
//! public keys from the provider's JWKS: the check stays the same.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use jsonwebtoken::{Algorithm, EncodingKey, Header};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

pub const ISSUER: &str = "chat-service";
pub const AUDIENCE: &str = "platform";
pub const TTL: Duration = Duration::from_secs(5 * 60);

#[derive(Serialize, Deserialize, Debug)]
pub struct Claims {
    pub sub: String,
    pub aud: String,
    pub iss: String,
    pub iat: u64,
    pub exp: u64,
}

pub struct Signer {
    key: EncodingKey,
}

impl Signer {
    /// From a PEM private key (PKCS#8), as OpenTofu's tls_private_key gives it.
    pub fn from_pem(pem: &str) -> Result<Self> {
        Ok(Self { key: EncodingKey::from_ed_pem(pem.as_bytes()).context("PLATFORM_TOKEN_KEY: an Ed25519 PEM key")? })
    }

    pub fn from_env() -> Result<Self> {
        Self::from_pem(&std::env::var("PLATFORM_TOKEN_KEY").context("PLATFORM_TOKEN_KEY is not set")?)
    }

    pub fn issue(&self, user_id: Uuid, ttl: Duration) -> Result<String> {
        let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
        let claims = Claims {
            sub: user_id.to_string(),
            aud: AUDIENCE.into(),
            iss: ISSUER.into(),
            iat: now,
            exp: now + ttl.as_secs(),
        };
        Ok(jsonwebtoken::encode(&Header::new(Algorithm::EdDSA), &claims, &self.key)?)
    }
}

#[cfg(test)]
pub mod tests {
    use jsonwebtoken::{DecodingKey, Validation};

    use super::*;

    /// A key pair for tests only; the cluster's is made by OpenTofu.
    pub const PRIVATE: &str = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIPjbWyeianVbvMzsaRag/CXwkHn6yFn+yBkiDDfzMFrd\n-----END PRIVATE KEY-----\n";
    pub const PUBLIC: &str = "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAMSkHjQsWr+8ukXVqCRuPLqS43S31qOc+KJZusUC6ywE=\n-----END PUBLIC KEY-----\n";

    pub fn verify(token: &str) -> Result<Claims, jsonwebtoken::errors::Error> {
        let mut validation = Validation::new(Algorithm::EdDSA);
        validation.set_audience(&[AUDIENCE]);
        validation.set_issuer(&[ISSUER]);
        let key = DecodingKey::from_ed_pem(PUBLIC.as_bytes()).unwrap();
        jsonwebtoken::decode::<Claims>(token, &key, &validation).map(|d| d.claims)
    }

    #[test]
    fn a_token_names_the_user_for_the_platform_and_expires() {
        let user = Uuid::now_v7();

        let claims = verify(&Signer::from_pem(PRIVATE).unwrap().issue(user, TTL).unwrap()).unwrap();

        assert_eq!((claims.sub, claims.aud.as_str(), claims.iss.as_str()), (user.to_string(), AUDIENCE, ISSUER));
        assert_eq!(claims.exp - claims.iat, TTL.as_secs());
    }

    #[test]
    fn a_key_that_is_not_ed25519_pem_is_refused() {
        assert!(Signer::from_pem("not a key").is_err());
    }
}
