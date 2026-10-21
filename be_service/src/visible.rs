//! What a user may see, and every read of user data: files, documents,
//! chats and their turns. These reads go only through the *_visible views
//! of ClickHouse (migrations 0030-0038), which take the user_id of the
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

use crate::store::{ChatMessageRow, ChatSummary, DocumentRow, FileRow, StoredFile};

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

    /// Documents of the user's files, the latest read first. A preprint and
    /// the published version of a paper, both the user's, are one document.
    pub async fn documents(&self, limit: u64, top_themes: u32) -> Result<Vec<DocumentRow>> {
        Ok(self
            .client
            .query(
                "SELECT d.document_id AS document_id, d.title AS title, d.year AS year, d.kind AS kind, \
                 d.keywords AS keywords, a.authors AS authors, d.files AS files, t.themes AS themes FROM ( \
                   SELECT s.document_id AS document_id, argMax(s.run_id, r.started_at) AS run_id, \
                   argMax(s.file_id, r.started_at) AS file_id, \
                   argMax(s.title, r.started_at) AS title, argMax(s.year, r.started_at) AS year, \
                   argMax(s.kind, r.started_at) AS kind, argMax(s.keywords, r.started_at) AS keywords, \
                   groupUniqArray(s.file_id) AS files, max(r.started_at) AS read_at \
                   FROM sat_document_visible(user_id = ?) AS s \
                   INNER JOIN (SELECT run_id, started_at FROM runs FINAL WHERE kind = 'document') AS r \
                   ON r.run_id = s.run_id GROUP BY s.document_id) AS d \
                 LEFT JOIN (SELECT run_id, arrayMap(p -> p.2, arraySort(groupArray((position, name)))) AS authors \
                   FROM link_document_author_visible(user_id = ?) GROUP BY run_id) AS a ON a.run_id = d.run_id \
                 LEFT JOIN (SELECT file_id, arrayMap(p -> p.2, arraySort(groupArray((rank, term)))) AS themes \
                   FROM document_themes_visible(user_id = ?) WHERE rank <= ? GROUP BY file_id) AS t \
                   ON t.file_id = d.file_id \
                 ORDER BY d.read_at DESC LIMIT ?",
            )
            .bind(self.user)
            .bind(self.user)
            .bind(self.user)
            .bind(top_themes)
            .bind(limit)
            .fetch_all()
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
    const USER_DATA: [&str; 16] = [
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
