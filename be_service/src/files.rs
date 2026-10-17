//! Uploaded files: the checks they pass before anything is stored, and the
//! object store their bytes go to. The bytes live in the bucket under
//! `files/<file_id>`; the row in `ml_data.files` is written after them.

use std::time::Duration;

use anyhow::{Context, Result};
use bytes::Bytes;
use rusty_s3::{Bucket, Credentials, S3Action, UrlStyle};
use sha2::{Digest, Sha256};

/// Upper bound of one file; the request body limit of the upload route.
pub const MAX_FILE_BYTES: usize = 10 * 1024 * 1024;
const EXTENSIONS: [&str; 3] = ["txt", "md", "markdown"];

/// Why a file is refused, before it is stored.
pub fn validate(name: &str, body: &[u8]) -> Result<(), String> {
    let well_formed =
        (1..=200).contains(&name.chars().count()) && !name.contains(['/', '\\']) && !name.chars().any(char::is_control);
    if !well_formed {
        return Err("name: 1-200 characters, no slashes".into());
    }
    let extension = name.rsplit_once('.').map(|(_, ext)| ext.to_ascii_lowercase());
    if !extension.is_some_and(|ext| EXTENSIONS.contains(&ext.as_str())) {
        return Err(format!("only text files for now: .{}", EXTENSIONS.join(", .")));
    }
    if body.is_empty() {
        return Err("the file is empty".into());
    }
    if body.len() > MAX_FILE_BYTES {
        return Err(format!("the file is larger than {} MiB", MAX_FILE_BYTES / 1024 / 1024));
    }
    if std::str::from_utf8(body).is_err() {
        return Err("the file is not UTF-8 text".into());
    }
    Ok(())
}

/// Content address of a file, its file_id: the same bytes uploaded twice are one file.
pub fn sha256_hex(body: &[u8]) -> String {
    Sha256::digest(body).iter().map(|b| format!("{b:02x}")).collect()
}

/// S3 API of the object store (SeaweedFS locally, S3 in the cloud): requests
/// are signed with SigV4 and sent with the same HTTP client as the rest.
pub struct ObjectStore {
    bucket: Bucket,
    credentials: Credentials,
    http: reqwest::Client,
}

impl ObjectStore {
    pub fn new(endpoint: &str, bucket: &str, access_key: &str, secret_key: &str) -> Result<Self> {
        let endpoint: reqwest::Url = endpoint.parse().with_context(|| format!("S3 endpoint {endpoint}"))?;
        let bucket = Bucket::new(endpoint, UrlStyle::Path, bucket.to_owned(), "us-east-1").context("S3 bucket")?;
        Ok(Self { bucket, credentials: Credentials::new(access_key, secret_key), http: reqwest::Client::new() })
    }

    pub async fn put(&self, key: &str, body: Bytes) -> Result<()> {
        let url = self.bucket.put_object(Some(&self.credentials), key).sign(Duration::from_secs(60));
        self.http.put(url).body(body).send().await?.error_for_status().with_context(|| format!("PUT {key}"))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_text_files() {
        assert_eq!(validate("notes.md", b"# Notes"), Ok(()));
        assert_eq!(validate("README.TXT", "привет".as_bytes()), Ok(()));
    }

    #[test]
    fn refuses_what_cannot_be_indexed() {
        assert!(validate("photo.png", b"x").unwrap_err().contains("only text files"));
        assert!(validate("noextension", b"x").is_err());
        assert!(validate("../etc/passwd.txt", b"x").unwrap_err().contains("no slashes"));
        assert_eq!(validate("empty.txt", b""), Err("the file is empty".into()));
        assert_eq!(validate("binary.txt", &[0xff, 0xfe, 0x00]), Err("the file is not UTF-8 text".into()));
        assert!(validate("big.txt", &vec![b'a'; MAX_FILE_BYTES + 1]).unwrap_err().contains("larger"));
    }

    #[test]
    fn sha256_is_hex_of_the_content() {
        assert_eq!(sha256_hex(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }
}
