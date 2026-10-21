//! Uploaded files: the checks they pass before anything is stored, the
//! object store their bytes go to, and their deletion. The bytes live in the
//! bucket under `files/<file_id>`; the row in `ml_data.files` is written
//! after them and deleted after everything made from them.

use std::time::Duration;

use anyhow::{Context, Result};
use bytes::Bytes;
use rusty_s3::{Bucket, Credentials, S3Action, UrlStyle};
use sha2::{Digest, Sha256};

use crate::store::ClickHouse;

/// Upper bound of one file; the request body limit of the upload route.
pub const MAX_FILE_BYTES: usize = 10 * 1024 * 1024;
const EXTENSIONS: [&str; 4] = ["txt", "md", "markdown", "pdf"];

/// Why a file is refused, before it is stored.
pub fn validate(name: &str, body: &[u8]) -> Result<(), String> {
    let well_formed =
        (1..=200).contains(&name.chars().count()) && !name.contains(['/', '\\']) && !name.chars().any(char::is_control);
    if !well_formed {
        return Err("name: 1-200 characters, no slashes".into());
    }
    let extension = name.rsplit_once('.').map(|(_, ext)| ext.to_ascii_lowercase()).unwrap_or_default();
    if !EXTENSIONS.contains(&extension.as_str()) {
        return Err(format!("only text and PDF files for now: .{}", EXTENSIONS.join(", .")));
    }
    if body.is_empty() {
        return Err("the file is empty".into());
    }
    if body.len() > MAX_FILE_BYTES {
        return Err(format!("the file is larger than {} MiB", MAX_FILE_BYTES / 1024 / 1024));
    }
    // The worker reads a PDF by its signature, any other file as UTF-8 text.
    if extension == "pdf" {
        if !body.starts_with(b"%PDF-") {
            return Err("the file is not a PDF".into());
        }
    } else if std::str::from_utf8(body).is_err() {
        return Err("the file is not UTF-8 text".into());
    }
    Ok(())
}

/// Content address of a file, its file_id: the same bytes uploaded twice are one file.
pub fn sha256_hex(body: &[u8]) -> String {
    Sha256::digest(body).iter().map(|b| format!("{b:02x}")).collect()
}

/// Where the bytes of a file live in the bucket.
pub fn object_key(file_id: &str) -> String {
    format!("files/{file_id}")
}

/// What deleting a file touches; a fake implements it in tests.
pub trait FileParts {
    async fn delete_markers(&self, file_id: &str) -> Result<()>;
    async fn delete_derived(&self, file_id: &str) -> Result<()>;
    async fn delete_chunks(&self, file_id: &str) -> Result<()>;
    async fn delete_object(&self, key: &str) -> Result<()>;
    async fn delete_file_row(&self, file_id: &str) -> Result<()>;
}

/// Deletes a file and everything made from it, in an order that keeps both
/// readers and retries right. Each step deletes by file_id and is a no-op
/// when there is nothing left, so a retry after a failure at any step
/// finishes the job.
pub async fn delete(parts: &impl FileParts, file_id: &str) -> Result<()> {
    // The markers of the runs made from the file first (index, document, mentions):
    // retrieval and the document views see rows only through the run
    // registry, so the file drops out of every answer and every list at once.
    parts.delete_markers(file_id).await?;
    // The rows, no longer seen by anyone: the document read from the file
    // and the keywords of its chunks, then the chunks.
    parts.delete_derived(file_id).await?;
    parts.delete_chunks(file_id).await?;
    parts.delete_object(&object_key(file_id)).await?;
    // The row last: while it is there, the file is listed and a retried
    // DELETE finds it.
    parts.delete_file_row(file_id).await
}

/// The owner links of files; a fake implements it in tests.
pub trait FileOwners {
    async fn remove_owner(&self, user: &str, file_id: &str) -> Result<()>;
    async fn owner_count(&self, file_id: &str) -> Result<u64>;
}

/// A user deletes a file: their link goes, and the file with everything made
/// from it goes when no other user has it (`delete`). True when it went.
/// The link goes first: if the deletion of the rest fails, the file is no
/// longer the user's, and the next deletion by its last owner finishes it.
/// Known race: a second user uploading the same bytes between the count and
/// the deletion gets a link to a file being deleted, and uploads it again.
pub async fn remove(owners: &impl FileOwners, parts: &impl FileParts, user: &str, file_id: &str) -> Result<bool> {
    owners.remove_owner(user, file_id).await?;
    if owners.owner_count(file_id).await? > 0 {
        return Ok(false);
    }
    delete(parts, file_id).await?;
    Ok(true)
}

impl FileOwners for Stores<'_> {
    async fn remove_owner(&self, user: &str, file_id: &str) -> Result<()> {
        self.store.remove_owner(user, file_id).await
    }
    async fn owner_count(&self, file_id: &str) -> Result<u64> {
        self.store.owner_count(file_id).await
    }
}

/// The ClickHouse tables and the bucket of the platform, as one FileParts.
pub struct Stores<'a> {
    pub store: &'a ClickHouse,
    pub objects: &'a ObjectStore,
}

impl FileParts for Stores<'_> {
    async fn delete_markers(&self, file_id: &str) -> Result<()> {
        self.store.delete_markers(file_id).await
    }
    async fn delete_derived(&self, file_id: &str) -> Result<()> {
        self.store.delete_derived(file_id).await
    }
    async fn delete_chunks(&self, file_id: &str) -> Result<()> {
        self.store.delete_chunks(file_id).await
    }
    async fn delete_object(&self, key: &str) -> Result<()> {
        self.objects.delete(key).await
    }
    async fn delete_file_row(&self, file_id: &str) -> Result<()> {
        self.store.delete_file(file_id).await
    }
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

    /// S3 answers 204 whether the object was there or not: deleting twice is fine.
    pub async fn delete(&self, key: &str) -> Result<()> {
        let url = self.bucket.delete_object(Some(&self.credentials), key).sign(Duration::from_secs(60));
        self.http.delete(url).send().await?.error_for_status().with_context(|| format!("DELETE {key}"))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;

    #[test]
    fn accepts_text_and_pdf_files() {
        assert_eq!(validate("notes.md", b"# Notes"), Ok(()));
        assert_eq!(validate("README.TXT", "привет".as_bytes()), Ok(()));
        assert_eq!(validate("paper.PDF", b"%PDF-1.7\n\xff\xfe binary"), Ok(()));
    }

    #[test]
    fn refuses_what_cannot_be_indexed() {
        assert!(validate("photo.png", b"x").unwrap_err().contains("only text and PDF files"));
        assert_eq!(validate("renamed.pdf", b"just text"), Err("the file is not a PDF".into()));
        assert!(validate("noextension", b"x").is_err());
        assert!(validate("../etc/passwd.txt", b"x").unwrap_err().contains("no slashes"));
        assert_eq!(validate("empty.txt", b""), Err("the file is empty".into()));
        assert_eq!(validate("binary.txt", &[0xff, 0xfe, 0x00]), Err("the file is not UTF-8 text".into()));
        assert!(validate("big.txt", &vec![b'a'; MAX_FILE_BYTES + 1]).unwrap_err().contains("larger"));
    }

    /// Records the steps; fails the step named in `fail` once.
    struct FakeParts {
        calls: RefCell<Vec<String>>,
        fail: RefCell<Option<&'static str>>,
    }

    impl FakeParts {
        fn new(fail: Option<&'static str>) -> Self {
            Self { calls: RefCell::default(), fail: RefCell::new(fail) }
        }
        fn step(&self, name: &'static str, arg: &str) -> Result<()> {
            if *self.fail.borrow() == Some(name) {
                self.fail.replace(None);
                anyhow::bail!("{name} failed");
            }
            self.calls.borrow_mut().push(format!("{name} {arg}"));
            Ok(())
        }
    }

    impl FileParts for FakeParts {
        async fn delete_markers(&self, file_id: &str) -> Result<()> {
            self.step("markers", file_id)
        }
        async fn delete_derived(&self, file_id: &str) -> Result<()> {
            self.step("derived", file_id)
        }
        async fn delete_chunks(&self, file_id: &str) -> Result<()> {
            self.step("chunks", file_id)
        }
        async fn delete_object(&self, key: &str) -> Result<()> {
            self.step("object", key)
        }
        async fn delete_file_row(&self, file_id: &str) -> Result<()> {
            self.step("row", file_id)
        }
    }

    #[tokio::test]
    async fn a_file_is_hidden_from_answers_first_and_unlisted_last() {
        let parts = FakeParts::new(None);

        delete(&parts, "abc").await.unwrap();

        assert_eq!(*parts.calls.borrow(), ["markers abc", "derived abc", "chunks abc", "object files/abc", "row abc"]);
    }

    #[tokio::test]
    async fn a_retry_after_a_failed_step_finishes_the_deletion() {
        let parts = FakeParts::new(Some("object"));

        assert!(delete(&parts, "abc").await.is_err());
        // The row is still there: the file is listed, and the retry finds it.
        assert_eq!(*parts.calls.borrow(), ["markers abc", "derived abc", "chunks abc"]);

        delete(&parts, "abc").await.unwrap();
        assert_eq!(parts.calls.borrow().last().unwrap(), "row abc");
    }

    /// Owner links in memory, recording into the same calls as FakeParts.
    struct FakeOwners<'a> {
        parts: &'a FakeParts,
        owners: RefCell<Vec<(String, String)>>,
    }

    impl FileOwners for FakeOwners<'_> {
        async fn remove_owner(&self, user: &str, file_id: &str) -> Result<()> {
            self.owners.borrow_mut().retain(|(u, f)| !(u == user && f == file_id));
            self.parts.calls.borrow_mut().push(format!("unlink {user} {file_id}"));
            Ok(())
        }
        async fn owner_count(&self, file_id: &str) -> Result<u64> {
            Ok(self.owners.borrow().iter().filter(|(_, f)| f == file_id).count() as u64)
        }
    }

    fn owned_by<'a>(parts: &'a FakeParts, users: &[&str]) -> FakeOwners<'a> {
        FakeOwners { parts, owners: RefCell::new(users.iter().map(|u| (u.to_string(), "abc".into())).collect()) }
    }

    #[tokio::test]
    async fn the_last_owner_deletes_the_file_and_everything_made_from_it() {
        let parts = FakeParts::new(None);
        let owners = owned_by(&parts, &["ann"]);

        assert!(remove(&owners, &parts, "ann", "abc").await.unwrap());

        assert_eq!(
            *parts.calls.borrow(),
            ["unlink ann abc", "markers abc", "derived abc", "chunks abc", "object files/abc", "row abc"]
        );
    }

    #[tokio::test]
    async fn an_owner_who_is_not_the_last_removes_only_their_link() {
        let parts = FakeParts::new(None);
        let owners = owned_by(&parts, &["ann", "bob"]);

        assert!(!remove(&owners, &parts, "bob", "abc").await.unwrap());

        assert_eq!(*parts.calls.borrow(), ["unlink bob abc"]);
        assert_eq!(owners.owners.borrow().as_slice(), [("ann".to_owned(), "abc".to_owned())]);
    }

    #[test]
    fn sha256_is_hex_of_the_content() {
        assert_eq!(sha256_hex(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }
}
