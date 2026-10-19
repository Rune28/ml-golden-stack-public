//! ClickHouse tables of be_service: `samples` (Add data), `files` (uploads),
//! and reads of the rest. Created by db/clickhouse/migrations, never here.

use std::collections::HashMap;
use std::ops::Range;

use anyhow::Result;
use clickhouse::{Client, Row};
use serde::{Deserialize, Serialize};
use serde_repr::Serialize_repr;

use crate::mnist::{Split, SplitKind};

/// What ingest needs from the table; a fake implements it in tests.
pub trait SampleStore {
    async fn batch_rows(&self, batch_id: &str) -> Result<u64>;
    /// Rows of a split, not counting one batch.
    async fn split_rows(&self, split: SplitKind, exclude_batch: &str) -> Result<u64>;
    async fn delete_batch(&self, batch_id: &str) -> Result<()>;
    /// Writes `data[rows]` as batch `batch_id`; `idx` is the position in the split.
    async fn insert(&self, batch_id: &str, split: SplitKind, data: &Split, rows: Range<usize>) -> Result<()>;
}

#[derive(Serialize, Deserialize, Row, Debug)]
pub struct Batch {
    pub batch_id: String,
    pub rows: u64,
    pub first_idx: u32,
    pub last_idx: u32,
    pub added_at: u32,
}

#[derive(Serialize, Deserialize, Row, Debug)]
pub struct PredictionRun {
    pub run_id: String,
    pub model_version: String,
    pub rows: u64,
    pub accuracy: f64,
    pub predicted_at: u32,
}

#[derive(Serialize, Deserialize, Row, Debug)]
pub struct DigitAccuracy {
    pub digit: u8,
    pub rows: u64,
    pub accuracy: f64,
}

/// A test image with the model's answer.
#[derive(Deserialize, Row, Debug)]
pub struct Example {
    pub idx: u32,
    pub label: u8,
    pub predicted: u8,
    pub confidence: f32,
    pub pixels: Vec<u8>,
}

#[derive(Serialize, Deserialize, Row, Debug)]
pub struct ConfusionCell {
    pub label: u8,
    pub predicted: u8,
    pub rows: u64,
}

/// One digit of a report: how often the model answered it, how often it was there.
#[derive(Serialize, Deserialize, Row, Debug)]
pub struct DigitCount {
    pub digit: u8,
    pub recognized: u32,
    pub actual: u32,
}

#[derive(Serialize, Deserialize, Row, Debug, PartialEq, Eq)]
pub struct Stats {
    pub train: u64,
    pub test: u64,
    pub train_batches: u64,
}

/// Enum8('train' = 1, 'test' = 2): RowBinary sends the number.
#[derive(Serialize_repr, Clone, Copy)]
#[repr(i8)]
enum SplitValue {
    Train = 1,
    Test = 2,
}

impl From<SplitKind> for SplitValue {
    fn from(kind: SplitKind) -> Self {
        match kind {
            SplitKind::Train => SplitValue::Train,
            SplitKind::Test => SplitValue::Test,
        }
    }
}

fn split_name(kind: SplitKind) -> &'static str {
    match kind {
        SplitKind::Train => "train",
        SplitKind::Test => "test",
    }
}

/// `added_at` is left to its DEFAULT now().
#[derive(Serialize, Row)]
struct SampleRow<'a> {
    batch_id: &'a str,
    idx: u32,
    split: SplitValue,
    label: u8,
    pixels: &'a [u8],
}

/// An uploaded file as stored.
#[derive(Serialize, Deserialize, Row, Debug)]
pub struct StoredFile {
    pub file_id: String,
    pub name: String,
    pub size: u64,
}

/// A conversation, for the chat page to go back to: its first question as a title.
#[derive(Serialize, Deserialize, Row, Debug)]
pub struct ChatSummary {
    pub chat_id: String,
    pub title: String,
    pub turns: u32,
    pub last_at: u32,
}

/// One committed message; sources are (file_id, index_run_id, chunk_id).
#[derive(Deserialize, Row, Debug)]
pub struct ChatMessageRow {
    pub turn: u32,
    pub role: String,
    pub text: String,
    pub sources: Vec<(String, String, u32)>,
}

#[derive(Deserialize, Row)]
struct FileName {
    file_id: String,
    name: String,
}

/// An uploaded file with its current index: `chunks` is 0 until an index
/// run of it is committed to the run registry.
#[derive(Deserialize, Row, Debug)]
pub struct FileRow {
    pub file_id: String,
    pub name: String,
    pub size: u64,
    pub uploaded_at: u32,
    pub chunks: u64,
    pub embed_model: String,
}

/// `uploaded_at` is left to its DEFAULT now64().
#[derive(Serialize, Row)]
struct FileInsert<'a> {
    file_id: &'a str,
    name: &'a str,
    size: u64,
    object_key: &'a str,
}

#[derive(Clone)]
pub struct ClickHouse {
    client: Client,
}

impl ClickHouse {
    pub fn new(client: Client) -> Self {
        Self { client }
    }

    pub async fn stats(&self) -> Result<Stats> {
        Ok(self
            .client
            .query(
                "SELECT countIf(split = 'train') AS train, countIf(split = 'test') AS test, \
                 uniqExactIf(batch_id, split = 'train') AS train_batches FROM samples",
            )
            .fetch_one::<Stats>()
            .await?)
    }

    /// Train batches in the order they were added.
    pub async fn batches(&self) -> Result<Vec<Batch>> {
        Ok(self
            .client
            .query(
                "SELECT batch_id, count() AS rows, min(idx) AS first_idx, max(idx) AS last_idx, \
                 toUInt32(max(added_at)) AS added_at FROM samples WHERE split = 'train' \
                 GROUP BY batch_id ORDER BY first_idx",
            )
            .fetch_all()
            .await?)
    }

    /// The latest prediction runs, newest first.
    pub async fn prediction_runs(&self, limit: u64) -> Result<Vec<PredictionRun>> {
        Ok(self
            .client
            .query(
                "SELECT run_id, any(model_version) AS model_version, count() AS rows, \
                 avg(label = predicted) AS accuracy, toUInt32(max(predicted_at)) AS predicted_at \
                 FROM predictions GROUP BY run_id ORDER BY predicted_at DESC LIMIT ?",
            )
            .bind(limit)
            .fetch_all()
            .await?)
    }

    /// Test images of a prediction run with their pixels: the most confident
    /// mistakes, or a fixed pseudo-random sample.
    pub async fn examples(&self, run_id: &str, mistakes: bool, limit: u64) -> Result<Vec<Example>> {
        let order = if mistakes { "p.confidence DESC" } else { "cityHash64(p.idx)" };
        let sql = format!(
            "SELECT p.idx AS idx, p.label AS label, p.predicted AS predicted, p.confidence AS confidence, \
             s.pixels AS pixels FROM predictions AS p \
             INNER JOIN (SELECT idx, pixels FROM samples WHERE split = 'test') AS s ON s.idx = p.idx \
             WHERE p.run_id = ? AND (p.label != p.predicted OR NOT ?) ORDER BY {order} LIMIT ?"
        );
        Ok(self.client.query(&sql).bind(run_id).bind(mistakes).bind(limit).fetch_all().await?)
    }

    /// Count of every (true digit, predicted digit) pair in a prediction run.
    pub async fn confusion(&self, run_id: &str) -> Result<Vec<ConfusionCell>> {
        Ok(self
            .client
            .query(
                "SELECT label, predicted, count() AS rows FROM predictions WHERE run_id = ? \
                 GROUP BY label, predicted ORDER BY label, predicted",
            )
            .bind(run_id)
            .fetch_all()
            .await?)
    }

    /// The latest report on a prediction run, empty until the Report step ran
    /// on it. Read through the view: the table keeps every report.
    pub async fn digit_report(&self, prediction_run_id: &str) -> Result<Vec<DigitCount>> {
        Ok(self
            .client
            .query(
                "SELECT digit, recognized, actual FROM digit_report_latest \
                 WHERE prediction_run_id = ? ORDER BY digit",
            )
            .bind(prediction_run_id)
            .fetch_all()
            .await?)
    }

    /// file_id is the sha256 of the bytes: also "were these bytes uploaded before".
    pub async fn file(&self, file_id: &str) -> Result<Option<StoredFile>> {
        Ok(self
            .client
            .query("SELECT file_id, name, size FROM files WHERE file_id = ? LIMIT 1")
            .bind(file_id)
            .fetch_optional()
            .await?)
    }

    /// Written after the bytes are in the object store: a listed file is always there.
    pub async fn insert_file(&self, file_id: &str, name: &str, size: u64, object_key: &str) -> Result<()> {
        let mut insert = self.client.insert::<FileInsert>("files").await?;
        insert.write(&FileInsert { file_id, name, size, object_key }).await?;
        insert.end().await?;
        Ok(())
    }

    /// The index markers of a file, of every run and model: its chunks
    /// drop out of doc_chunks_current at once.
    pub async fn delete_index_markers(&self, file_id: &str) -> Result<()> {
        self.client
            .query("DELETE FROM runs WHERE kind = 'index' AND parents['file_id'] = ?")
            .bind(file_id)
            .execute()
            .await?;
        Ok(())
    }

    pub async fn delete_chunks(&self, file_id: &str) -> Result<()> {
        self.client.query("DELETE FROM doc_chunks WHERE file_id = ?").bind(file_id).execute().await?;
        Ok(())
    }

    pub async fn delete_file(&self, file_id: &str) -> Result<()> {
        self.client.query("DELETE FROM files WHERE file_id = ?").bind(file_id).execute().await?;
        Ok(())
    }

    /// Conversations, the latest first. Through chat_messages_committed: a
    /// turn written halfway never shows (README, rule 11).
    pub async fn chats(&self, limit: u64) -> Result<Vec<ChatSummary>> {
        Ok(self
            .client
            .query(
                "SELECT chat_id, argMinIf(text, turn, role = 'user') AS title, toUInt32(max(turn)) AS turns, \
                 toUInt32(toUnixTimestamp(max(created_at))) AS last_at FROM chat_messages_committed \
                 GROUP BY chat_id ORDER BY last_at DESC LIMIT ?",
            )
            .bind(limit)
            .fetch_all()
            .await?)
    }

    /// The committed turns of one conversation, in order, the question before its answer.
    pub async fn chat_messages(&self, chat_id: &str) -> Result<Vec<ChatMessageRow>> {
        Ok(self
            .client
            .query(
                // The client cannot read named tuples (the column type in 0013): cast to
                // a plain one, the same values.
                "SELECT turn, role, text, CAST(sources, 'Array(Tuple(String, String, UInt32))') AS sources \
                 FROM chat_messages_committed WHERE chat_id = ? ORDER BY turn, role = 'assistant'",
            )
            .bind(chat_id)
            .fetch_all()
            .await?)
    }

    /// Names of files by id; a deleted file has none.
    pub async fn file_names(&self, file_ids: &[String]) -> Result<HashMap<String, String>> {
        let rows: Vec<FileName> = self
            .client
            .query("SELECT file_id, name FROM files FINAL WHERE has(?, file_id)")
            .bind(file_ids)
            .fetch_all()
            .await?;
        Ok(rows.into_iter().map(|r| (r.file_id, r.name)).collect())
    }

    /// Uploaded files, newest first, each with its latest committed index
    /// (README, rule 11: a run exists once its marker is in the registry).
    pub async fn files(&self, limit: u64) -> Result<Vec<FileRow>> {
        Ok(self
            .client
            .query(
                "SELECT f.file_id AS file_id, f.name AS name, f.size AS size, \
                 toUInt32(toUnixTimestamp(f.uploaded_at)) AS uploaded_at, r.chunks AS chunks, \
                 r.embed_model AS embed_model FROM files AS f FINAL \
                 LEFT JOIN (SELECT parents['file_id'] AS file_id, argMax(rows, started_at) AS chunks, \
                 argMax(parents['embed_model'], started_at) AS embed_model \
                 FROM runs FINAL WHERE kind = 'index' GROUP BY file_id) AS r ON r.file_id = f.file_id \
                 ORDER BY f.uploaded_at DESC LIMIT ?",
            )
            .bind(limit)
            .fetch_all()
            .await?)
    }

    /// Accuracy per true digit in one prediction run.
    pub async fn digit_accuracy(&self, run_id: &str) -> Result<Vec<DigitAccuracy>> {
        Ok(self
            .client
            .query(
                "SELECT label AS digit, count() AS rows, avg(label = predicted) AS accuracy \
                 FROM predictions WHERE run_id = ? GROUP BY digit ORDER BY digit",
            )
            .bind(run_id)
            .fetch_all()
            .await?)
    }
}

impl SampleStore for ClickHouse {
    async fn batch_rows(&self, batch_id: &str) -> Result<u64> {
        Ok(self
            .client
            .query("SELECT count() FROM samples WHERE batch_id = ?")
            .bind(batch_id)
            .fetch_one::<u64>()
            .await?)
    }

    async fn split_rows(&self, split: SplitKind, exclude_batch: &str) -> Result<u64> {
        Ok(self
            .client
            .query("SELECT count() FROM samples WHERE split = ? AND batch_id != ?")
            .bind(split_name(split))
            .bind(exclude_batch)
            .fetch_one::<u64>()
            .await?)
    }

    async fn delete_batch(&self, batch_id: &str) -> Result<()> {
        // Lightweight delete: rows are hidden at once, removed on merges.
        self.client.query("DELETE FROM samples WHERE batch_id = ?").bind(batch_id).execute().await?;
        Ok(())
    }

    async fn insert(&self, batch_id: &str, split: SplitKind, data: &Split, rows: Range<usize>) -> Result<()> {
        // One INSERT per batch: ClickHouse writes it atomically while it fits
        // in max_insert_block_size (~1M rows), far above a batch.
        let mut insert = self.client.insert::<SampleRow>("samples").await?;
        for i in rows {
            insert
                .write(&SampleRow {
                    batch_id,
                    idx: i as u32,
                    split: split.into(),
                    label: data.labels[i],
                    pixels: data.image(i),
                })
                .await?;
        }
        insert.end().await?;
        Ok(())
    }
}
