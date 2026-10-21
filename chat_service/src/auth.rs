//! What a login is made of, with no storage: the rules a login and a
//! password follow, password hashes, session tokens, the session cookie and
//! the check of where a request comes from.
//!
//! The rules are written here once. The server checks every field against
//! them and is the authority; the page reads the same rules from
//! `GET /api/auth/rules` and checks before sending, for the user's sake.

use std::sync::OnceLock;
use std::time::Duration;

use argon2::Argon2;
use argon2::password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use rand_core::{OsRng, RngCore};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const EMAIL_MIN: usize = 3;
pub const EMAIL_MAX: usize = 254;
pub const PASSWORD_MIN: usize = 12;
/// An upper bound too: hashing an unbounded password is work an attacker chooses.
pub const PASSWORD_MAX: usize = 128;

/// A session lasts this long after its last use...
pub const SESSION_TTL: Duration = Duration::from_secs(12 * 3600);
/// ...and never longer than this after the login, used or not.
pub const SESSION_MAX: Duration = Duration::from_secs(7 * 24 * 3600);
/// Failed logins (and logins being checked) counted over this window...
pub const FAILURE_WINDOW: Duration = Duration::from_secs(15 * 60);
/// ...per email, and for the whole service. Not per address: behind the
/// port-forward every request comes from 127.0.0.1, and a limit per address
/// would lock everyone out at once.
pub const EMAIL_FAILURES: i64 = 5;
pub const SERVICE_FAILURES: i64 = 100;

pub const COOKIE: &str = "session";

/// The rules, as the page reads them.
pub fn rules() -> Value {
    json!({
        "email": { "min": EMAIL_MIN, "max": EMAIL_MAX },
        "password": { "min": PASSWORD_MIN, "max": PASSWORD_MAX, "not_email": true },
    })
}

/// Emails are kept and compared lowercase, without surrounding spaces.
pub fn normalize_email(raw: &str) -> String {
    raw.trim().to_lowercase()
}

/// Why an email is refused: one `@` with something on both sides, no spaces.
pub fn check_email(email: &str) -> Result<(), String> {
    let n = email.chars().count();
    if !(EMAIL_MIN..=EMAIL_MAX).contains(&n) {
        return Err(format!("email: {EMAIL_MIN}-{EMAIL_MAX} characters"));
    }
    let well_formed = matches!(email.split_once('@'), Some((local, domain))
        if !local.is_empty() && !domain.is_empty() && !domain.contains('@'))
        && !email.chars().any(|c| c.is_whitespace() || c.is_control());
    if !well_formed {
        return Err("email: one @ with something on both sides, no spaces".into());
    }
    Ok(())
}

/// Why a password is refused.
pub fn check_password(password: &str, email: &str) -> Result<(), String> {
    let n = password.chars().count();
    if !(PASSWORD_MIN..=PASSWORD_MAX).contains(&n) {
        return Err(format!("password: {PASSWORD_MIN}-{PASSWORD_MAX} characters"));
    }
    if normalize_email(password) == normalize_email(email) {
        return Err("password: not the email".into());
    }
    Ok(())
}

/// argon2id with its default parameters (19 MiB, 2 passes), as a PHC string.
pub fn hash_password(password: &str) -> String {
    let salt = SaltString::generate(&mut OsRng);
    Argon2::default().hash_password(password.as_bytes(), &salt).expect("argon2 with default parameters").to_string()
}

pub fn verify_password(password: &str, phc: &str) -> bool {
    PasswordHash::new(phc).is_ok_and(|hash| Argon2::default().verify_password(password.as_bytes(), &hash).is_ok())
}

/// A hash no password matches, checked when the email is unknown: a wrong
/// email then takes as long as a wrong password, and the two cannot be told
/// apart by the time the answer takes.
pub fn dummy_hash() -> &'static str {
    static DUMMY: OnceLock<String> = OnceLock::new();
    DUMMY.get_or_init(|| hash_password(&new_token()))
}

/// A session token: 32 random bytes, URL-safe base64.
pub fn new_token() -> String {
    let mut bytes = [0u8; 32];
    OsRng.fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}

/// What the database keeps of a token: a copy of the table logs no one in.
pub fn token_hash(token: &str) -> Vec<u8> {
    Sha256::digest(token.as_bytes()).to_vec()
}

/// The session cookie: not readable by scripts, sent over HTTPS only (browsers
/// treat http://localhost and 127.0.0.1 as secure), never on a request from
/// another site. No Max-Age: the server alone decides when a session ends
/// (SESSION_TTL, SESSION_MAX). A Max-Age set once at the login would drop the
/// cookie of a user still at work while the session lives on.
pub fn session_cookie(token: &str) -> String {
    format!("{COOKIE}={token}; HttpOnly; Secure; SameSite=Strict; Path=/")
}

pub fn cleared_cookie() -> String {
    format!("{COOKIE}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0")
}

/// The session token of a `Cookie` header, if there is one.
pub fn token_from_cookies(header: &str) -> Option<&str> {
    header
        .split(';')
        .filter_map(|pair| pair.trim().split_once('='))
        .find(|(name, value)| *name == COOKIE && !value.is_empty())
        .map(|(_, value)| value)
}

/// A request that changes something must say it comes from the page itself.
/// With SameSite=Strict, this is the guard against cross-site requests.
pub fn origin_allowed(origin: Option<&str>, allowed: &[String]) -> bool {
    origin.is_some_and(|o| allowed.iter().any(|a| a == o))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn emails_are_one_at_with_something_on_both_sides() {
        assert_eq!(check_email("ann@example.org"), Ok(()));
        assert!(check_email("a@").is_err());
        assert!(check_email("@b.org").is_err());
        assert!(check_email("a@b@c.org").is_err());
        assert!(check_email("ann @example.org").is_err());
        assert!(check_email("ab").unwrap_err().contains("3-254"));
        assert!(check_email(&format!("{}@b.org", "a".repeat(250))).is_err());
        assert_eq!(normalize_email("  Ann@Example.ORG "), "ann@example.org");
    }

    #[test]
    fn passwords_have_bounds_and_are_not_the_email() {
        assert_eq!(check_password("correct horse battery", "ann@example.org"), Ok(()));
        assert!(check_password("short", "ann@example.org").unwrap_err().contains("12-128"));
        assert!(check_password(&"x".repeat(129), "ann@example.org").is_err());
        assert_eq!(check_password("Ann@Example.org", "ann@example.org"), Err("password: not the email".into()));
        // Characters, not bytes: twelve Cyrillic letters are enough.
        assert_eq!(check_password("пароль-длинный", "ann@example.org"), Ok(()));
    }

    #[test]
    fn a_hash_verifies_its_password_only() {
        let phc = hash_password("correct horse battery");
        assert!(phc.starts_with("$argon2id$"));
        assert!(verify_password("correct horse battery", &phc));
        assert!(!verify_password("correct horse batterY", &phc));
        assert!(!verify_password("anything", "not a hash"));
        assert!(!verify_password("", dummy_hash()));
    }

    #[test]
    fn tokens_are_random_and_only_their_hash_is_kept() {
        let (a, b) = (new_token(), new_token());
        assert_ne!(a, b);
        assert_eq!(a.len(), 43);
        assert_eq!(token_hash(&a).len(), 32);
        assert_ne!(token_hash(&a), token_hash(&b));
    }

    #[test]
    fn the_session_cookie_is_strict_and_found_among_others() {
        let cookie = session_cookie("tok");
        for attr in ["HttpOnly", "Secure", "SameSite=Strict", "Path=/"] {
            assert!(cookie.contains(attr), "{attr}");
        }
        assert!(!cookie.contains("Max-Age"), "the browser keeps the cookie; the server ends the session");
        assert_eq!(token_from_cookies("theme=dark; session=abc; x=1"), Some("abc"));
        assert_eq!(token_from_cookies("session="), None);
        assert_eq!(token_from_cookies("sessions=abc"), None);
        assert!(cleared_cookie().contains("Max-Age=0"));
    }

    #[test]
    fn only_the_page_itself_may_change_something() {
        let allowed = vec!["http://127.0.0.1:9002".to_owned()];
        assert!(origin_allowed(Some("http://127.0.0.1:9002"), &allowed));
        assert!(!origin_allowed(Some("http://evil.example"), &allowed));
        assert!(!origin_allowed(None, &allowed));
    }
}
