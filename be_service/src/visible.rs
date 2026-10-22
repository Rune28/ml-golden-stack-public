//! What a user may see, and every read of user data: files, documents,
//! chats and their turns. These reads go only through the *_visible views
//! of ClickHouse (migrations 0030-0038, 0042-0043), which take the user_id of the
//! request and join the owner links: a read that forgot the user cannot get
//! past them, and without the parameter a query fails instead of reading
//! everyone's rows. A test below fails when this file names a table or a
//! current view of user data directly.
//!
//! Writers (uploads, owner links, deletion) are in store.rs: a writer checks
//! and changes its own rows by key.

use std::collections::HashMap;

use anyhow::Result;
use clickhouse::{Client, Row};
use serde::Deserialize;

use crate::store::{ChatMessageRow, ChatSummary, CollectionRow, DocumentFacet, DocumentRow, FileRow, StoredFile};

/// The reads of one user.
pub struct Visible<'a> {
    pub(crate) client: &'a Client,
    pub(crate) user: &'a str,
}

#[derive(Deserialize, Row)]
struct FileName {
    file_id: String,
    name: String,
}

impl Visible<'_> {
    /// The user's files, newest first, each with its latest committed index
    /// (README, rule 11: a run exists once its marker is in the registry).
    pub async fn files(&self, limit: u64) -> Result<Vec<FileRow>> {
        Ok(self
            .client
            .query(
                "SELECT f.file_id AS file_id, f.name AS name, f.size AS size, \
                 toUInt32(toUnixTimestamp(f.uploaded_at)) AS uploaded_at, r.chunks AS chunks, \
                 r.embed_model AS embed_model FROM files_visible(user_id = ?) AS f \
                 LEFT JOIN (SELECT parents['file_id'] AS file_id, argMax(rows, started_at) AS chunks, \
                 argMax(parents['embed_model'], started_at) AS embed_model \
                 FROM runs FINAL WHERE kind = 'index' GROUP BY file_id) AS r ON r.file_id = f.file_id \
                 ORDER BY f.uploaded_at DESC LIMIT ?",
            )
            .bind(self.user)
            .bind(limit)
            .fetch_all()
            .await?)
    }

    /// One of the user's files: None for a file of someone else, as for one
    /// that does not exist.
    pub async fn file(&self, file_id: &str) -> Result<Option<StoredFile>> {
        Ok(self
            .client
            .query("SELECT file_id, name, size FROM files_visible(user_id = ?) WHERE file_id = ? LIMIT 1")
            .bind(self.user)
            .bind(file_id)
            .fetch_optional()
            .await?)
    }

    /// The user's names of their files by id; a deleted file has none.
    pub async fn file_names(&self, file_ids: &[String]) -> Result<HashMap<String, String>> {
        let rows: Vec<FileName> = self
            .client
            .query("SELECT file_id, name FROM files_visible(user_id = ?) WHERE has(?, file_id)")
            .bind(self.user)
            .bind(file_ids)
            .fetch_all()
            .await?;
        Ok(rows.into_iter().map(|r| (r.file_id, r.name)).collect())
    }

    /// The user's files as documents, the latest upload first: their
    /// corrections over what read_document found (documents_visible), with
    /// the top themes of each file. With a file_id, only that file.
    pub async fn documents(&self, file_id: Option<&str>, limit: u64, top_themes: u32) -> Result<Vec<DocumentRow>> {
        Ok(self
            .client
            .query(
                "SELECT d.file_id AS file_id, d.name AS name, d.document_id AS document_id, \
                 toBool(d.document_run_id != '') AS read, d.title AS title, d.title_source AS title_source, \
                 d.authors AS authors, d.authors_source AS authors_source, d.kind AS kind, \
                 d.kind_source AS kind_source, d.year AS year, d.year_source AS year_source, \
                 d.confirmed AS confirmed, d.keywords AS keywords, d.collection_ids AS collection_ids, \
                 t.themes AS themes, toUInt32(toUnixTimestamp(d.uploaded_at)) AS uploaded_at \
                 FROM documents_visible(user_id = ?) AS d \
                 LEFT JOIN (SELECT file_id, arrayMap(p -> p.2, arraySort(groupArray((rank, term)))) AS themes \
                   FROM document_themes_visible(user_id = ?) WHERE rank <= ? GROUP BY file_id) AS t \
                   ON t.file_id = d.file_id \
                 WHERE ? = '' OR d.file_id = ? ORDER BY d.uploaded_at DESC LIMIT ?",
            )
            .bind(self.user)
            .bind(self.user)
            .bind(top_themes)
            .bind(file_id.unwrap_or(""))
            .bind(file_id.unwrap_or(""))
            .bind(limit)
            .fetch_all()
            .await?)
    }

    /// The authors, kind and year of each of the user's documents: what a
    /// scope can be made of.
    pub async fn facet_rows(&self) -> Result<Vec<DocumentFacet>> {
        Ok(self
            .client
            .query("SELECT authors, kind, year FROM documents_visible(user_id = ?)")
            .bind(self.user)
            .fetch_all()
            .await?)
    }

    /// The user's groups of sources with their files, by name.
    pub async fn collections(&self) -> Result<Vec<CollectionRow>> {
        Ok(self
            .client
            .query("SELECT collection_id, name, file_ids FROM collections_visible(user_id = ?) ORDER BY name")
            .bind(self.user)
            .fetch_all()
            .await?)
    }

    /// One of the user's groups: None for a group of someone else, or deleted.
    pub async fn collection(&self, collection_id: &str) -> Result<Option<CollectionRow>> {
        Ok(self
            .client
            .query("SELECT collection_id, name, file_ids FROM collections_visible(user_id = ?) WHERE collection_id = ?")
            .bind(self.user)
            .bind(collection_id)
            .fetch_optional()
            .await?)
    }

    /// Whether a chat is the user's: started by them (chat_owners).
    pub async fn owns_chat(&self, chat_id: &str) -> Result<bool> {
        let n: u64 = self
            .client
            .query("SELECT count() FROM chat_owners FINAL WHERE user_id = ? AND chat_id = ?")
            .bind(self.user)
            .bind(chat_id)
            .fetch_one()
            .await?;
        Ok(n > 0)
    }

    /// The user's conversations, the latest first; a turn written halfway never shows.
    pub async fn chats(&self, limit: u64) -> Result<Vec<ChatSummary>> {
        Ok(self
            .client
            .query(
                "SELECT chat_id, argMinIf(text, turn, role = 'user') AS title, toUInt32(max(turn)) AS turns, \
                 toUInt32(toUnixTimestamp(max(created_at))) AS last_at FROM chat_messages_visible(user_id = ?) \
                 GROUP BY chat_id ORDER BY last_at DESC LIMIT ?",
            )
            .bind(self.user)
            .bind(limit)
            .fetch_all()
            .await?)
    }

    /// The committed turns of one of the user's conversations, in order, the
    /// question before its answer.
    pub async fn chat_messages(&self, chat_id: &str) -> Result<Vec<ChatMessageRow>> {
        Ok(self
            .client
            .query(
                // The client cannot read named tuples (the column type in 0013): cast to
                // a plain one, the same values.
                "SELECT turn, role, text, CAST(sources, 'Array(Tuple(String, String, UInt32))') AS sources \
                 FROM chat_messages_visible(user_id = ?) WHERE chat_id = ? ORDER BY turn, role = 'assistant'",
            )
            .bind(self.user)
            .bind(chat_id)
            .fetch_all()
            .await?)
    }
}

#[cfg(test)]
mod tests {
    /// Tables and current views of user data: readers here go through
    /// *_visible instead. The owner links (file_owners, chat_owners) and the
    /// run registry (runs) are not user data and may be read.
    const USER_DATA: [&str; 19] = [
        "files",
        "doc_chunks",
        "doc_chunks_current",
        "sat_document",
        "sat_document_current",
        "link_document_author",
        "link_document_author_current",
        "chunk_mentions",
        "chunk_mentions_current",
        "document_themes",
        "document_themes_current",
        "term_vectors_current",
        "chat_messages",
        "chat_messages_committed",
        "hub_document",
        "link_document_file",
        "document_edits",
        "collections",
        "collection_files",
    ];

    /// The names this code reads from: the word after every FROM and JOIN of its SQL.
    pub fn read_names(code: &str) -> Vec<&str> {
        let mut names = Vec::new();
        for keyword in ["FROM ", "JOIN "] {
            for (i, _) in code.match_indices(keyword) {
                let rest = &code[i + keyword.len()..];
                let end = rest.find(|c: char| !(c.is_alphanumeric() || c == '_')).unwrap_or(rest.len());
                names.push(&rest[..end]);
            }
        }
        names
    }

    #[test]
    fn readers_of_user_data_name_only_visible_views() {
        let source = include_str!("visible.rs");
        let code = &source[..source.find("#[cfg(test)]").unwrap()];
        let names = read_names(code);
        assert!(names.contains(&"files_visible") && names.contains(&"runs"), "{names:?}");
        for name in names {
            assert!(!USER_DATA.contains(&name), "visible.rs reads {name} directly; read its *_visible view");
        }
    }

    #[test]
    fn the_check_sees_a_raw_table_in_sql() {
        assert_eq!(
            read_names("\"SELECT a FROM files FINAL INNER JOIN doc_chunks_current AS c\""),
            ["files", "doc_chunks_current"]
        );
    }
}
