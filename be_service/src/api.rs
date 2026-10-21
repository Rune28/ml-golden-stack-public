//! HTTP API for the pipeline page.

use std::collections::BTreeSet;
use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::Mutex;

use crate::auth::{User, Verifier};
use crate::files::{self, ObjectStore};
use crate::ingest::{self, IngestError, IngestResult, TEST_BATCH_ID};
use crate::mlflow::{Mlflow, ModelVersion};
use crate::mnist::{Source, SplitKind};
use crate::store::{
    Batch, ChatSummary, ClickHouse, ConfusionCell, DigitAccuracy, DigitCount, DocumentRow, PredictionRun, Stats,
};
use crate::temporal::{LLM_QUEUE, Pipeline, StartError, Started, Status, Temporal, UpdateError};

/// Upper bound of one Add data: the whole MNIST train split.
const MAX_BATCH: u64 = 60_000;
/// Upper bound of one chat message, the same as the Chat workflow's.
const MAX_MESSAGE_CHARS: usize = 4000;

pub struct AppState {
    pub store: ClickHouse,
    pub mnist: Source,
    pub temporal: Temporal,
    pub mlflow: Mlflow,
    /// Add data reads the offset, then writes: two at once would take the
    /// same examples. Enough while be_service runs one replica.
    pub ingest_lock: Mutex<()>,
    pub objects: ObjectStore,
    /// Checks the platform token of the product routes (files, documents, chats).
    pub verifier: Verifier,
}

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/", get(|| async { Html(include_str!("../static/index.html")) }))
        .route("/api/batches", post(add_data))
        .route("/api/train", post(train))
        .route("/api/run", post(run))
        .route("/api/report", post(report))
        .route("/api/stats", get(stats))
        .route("/api/state", get(page_state))
        .route("/api/runs/{run_id}/examples", get(examples))
        .route("/api/runs/{run_id}/confusion", get(confusion))
        .route("/api/runs/{run_id}/report", get(digit_report))
        .route("/api/files", post(upload_file).layer(DefaultBodyLimit::max(files::MAX_FILE_BYTES)).get(list_files))
        .route("/api/files/{file_id}", delete(delete_file))
        .route("/api/files/{file_id}/index", post(index_file))
        .route("/api/documents", get(list_documents))
        .route("/api/chats", post(new_chat).get(list_chats))
        .route("/api/chats/{chat_id}/messages", post(send_message).get(chat_history))
        .route("/healthz", get(|| async { "ok" }))
        .with_state(state)
}

#[derive(Deserialize)]
struct AddData {
    count: u64,
    /// Optional: a client that retries a request sends the same id and
    /// gets the same batch instead of a second one.
    batch_id: Option<String>,
}

async fn add_data(
    State(state): State<Arc<AppState>>,
    Json(req): Json<AddData>,
) -> Result<Json<IngestResult>, ApiError> {
    if !(1..=MAX_BATCH).contains(&req.count) {
        return Err(ApiError::BadRequest(format!("count must be between 1 and {MAX_BATCH}")));
    }
    let batch_id = match req.batch_id {
        Some(id) => validate_batch_id(id)?,
        // v7 is ordered by time, so new batches go to the end of the
        // ORDER BY (batch_id, idx) key.
        None => uuid::Uuid::now_v7().to_string(),
    };

    let _guard = state.ingest_lock.lock().await;
    let train = state.mnist.split(SplitKind::Train).await?;
    let test = state.mnist.split(SplitKind::Test).await?;
    let result = ingest::ingest(&state.store, &train, &test, &batch_id, req.count).await?;
    tracing::info!(batch_id = %result.batch_id, added = result.added, train_total = result.train_total, "batch added");
    Ok(Json(result))
}

async fn stats(State(state): State<Arc<AppState>>) -> Result<Json<Stats>, ApiError> {
    Ok(Json(state.store.stats().await?))
}

#[derive(Deserialize)]
struct TrainRequest {
    #[serde(default = "default_pool")]
    pool: String,
    #[serde(default = "default_epochs")]
    epochs: u32,
}

fn default_pool() -> String {
    "gpu".into()
}

fn default_epochs() -> u32 {
    3
}

/// Starts the Train workflow. The run_id is generated here, like batch_id:
/// it names the Jobs, the MLflow run and the result objects.
async fn train(State(state): State<Arc<AppState>>, Json(req): Json<TrainRequest>) -> Result<Json<Started>, ApiError> {
    if !["cpu", "gpu"].contains(&req.pool.as_str()) || !(1..=20).contains(&req.epochs) {
        return Err(ApiError::BadRequest("pool: cpu or gpu, epochs: 1-20".into()));
    }
    let input = json!({ "run_id": uuid::Uuid::now_v7().to_string(), "pool": req.pool, "epochs": req.epochs });
    start(&state, Pipeline::Train, input).await
}

#[derive(Deserialize)]
struct RunRequest {
    #[serde(default = "default_model")]
    model: String,
}

fn default_model() -> String {
    "champion".into()
}

async fn run(State(state): State<Arc<AppState>>, Json(req): Json<RunRequest>) -> Result<Json<Started>, ApiError> {
    let model_ok = !req.model.is_empty() && req.model.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-');
    if !model_ok {
        return Err(ApiError::BadRequest("model: a version number or an alias".into()));
    }
    let input = json!({ "run_id": uuid::Uuid::now_v7().to_string(), "model": req.model, "pool": "cpu" });
    start(&state, Pipeline::Run, input).await
}

#[derive(Deserialize, Default)]
struct ReportRequest {
    /// The prediction run to report on; the latest one when absent.
    prediction_run_id: Option<String>,
}

/// Starts the Report workflow: the distribution of the digits a prediction
/// run recognized, next to the true one.
async fn report(State(state): State<Arc<AppState>>, Json(req): Json<ReportRequest>) -> Result<Json<Started>, ApiError> {
    let prediction_run_id = match req.prediction_run_id {
        Some(id) => validate_run_id("prediction_run_id", id)?,
        None => match state.store.prediction_runs(1).await?.into_iter().next() {
            Some(latest) => latest.run_id,
            None => return Err(ApiError::Conflict("nothing to report on: Run model first".into())),
        },
    };
    let input = json!({ "run_id": uuid::Uuid::now_v7().to_string(), "prediction_run_id": prediction_run_id });
    start(&state, Pipeline::Report, input).await
}

async fn start(state: &AppState, pipeline: Pipeline, input: Value) -> Result<Json<Started>, ApiError> {
    match state.temporal.start(pipeline, input).await {
        Ok(started) => {
            tracing::info!(workflow_id = %started.workflow_id, run_id = %started.run_id, "workflow started");
            Ok(Json(started))
        }
        Err(StartError::AlreadyRunning) => {
            Err(ApiError::Conflict(format!("{} is already running", pipeline.workflow_type())))
        }
        Err(StartError::Other(e)) => Err(ApiError::Internal(e)),
    }
}

#[derive(Deserialize)]
struct UploadQuery {
    name: String,
}

#[derive(Serialize)]
struct Uploaded {
    file_id: String,
    name: String,
    size: u64,
    /// The same bytes were uploaded before or at the same moment: the
    /// existing file, indexed or not.
    duplicate: bool,
    index: Option<Started>,
}

/// Upload a text file: `POST /api/files?name=notes.md` with the file as the
/// body. The bytes go to the object store, then a row to `files`, then the
/// user's link to it, then the Index workflow starts. file_id is the sha256
/// of the bytes, so the same bytes uploaded again, by a retry, by another
/// replica at the same moment or by another user, are the same file: stored
/// once, indexed once.
///
/// `duplicate` is about the user's own files only. Bytes another user has
/// get the user's link and no second index, and the answer reads as a new
/// upload: an upload does not tell anyone what others have. What still
/// shows is the time: such a file is indexed at once (docs/auth-plan.md).
async fn upload_file(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Query(q): Query<UploadQuery>,
    body: Bytes,
) -> Result<Json<Uploaded>, ApiError> {
    files::validate(&q.name, &body).map_err(ApiError::BadRequest)?;
    let file_id = files::sha256_hex(&body);
    let size = body.len() as u64;

    if let Some(mine) = state.store.visible(&user).file(&file_id).await? {
        return Ok(Json(Uploaded { file_id, name: mine.name, size: mine.size, duplicate: true, index: None }));
    }
    if state.store.stored_file(&file_id).await?.is_some() {
        state.store.add_owner(&user, &file_id, &q.name).await?;
        tracing::info!(%file_id, %user, "file linked to another owner");
        return Ok(Json(Uploaded { file_id, name: q.name, size, duplicate: false, index: None }));
    }
    // Two uploads of the same bytes at once both get here. They write the
    // same object and the same row (the duplicate merges away), and Temporal
    // lets only one of them start the index.
    let object_key = files::object_key(&file_id);
    state.objects.put(&object_key, body).await?;
    state.store.insert_file(&file_id, &q.name, size, &object_key).await?;
    state.store.add_owner(&user, &file_id, &q.name).await?;

    tracing::info!(%file_id, %user, name = %q.name, size, "file uploaded");
    let index = match start_index(&state, &file_id).await {
        Ok(started) => Some(started),
        // The other upload of these bytes started it.
        Err(ApiError::Conflict(_)) => None,
        Err(e) => return Err(e),
    };
    Ok(Json(Uploaded { file_id, name: q.name, size, duplicate: false, index }))
}

/// Index a file again: after a failed index, or with a new embedding model.
async fn index_file(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Path(file_id): Path<String>,
) -> Result<Json<Started>, ApiError> {
    let file_id = validate_run_id("file_id", file_id)?;
    if state.store.visible(&user).file(&file_id).await?.is_none() {
        return Err(ApiError::NotFound(format!("no file {file_id}")));
    }
    Ok(Json(start_index(&state, &file_id).await?))
}

/// A user deletes their file: `DELETE /api/files/<file_id>`. Their link to
/// it goes; the file and everything made from it go when no other user has
/// it (files::remove). Refused while the file is being indexed: an index
/// committed after the deletion would bring its chunks back (retrieval also
/// skips chunks of files that are gone). A file of someone else is a 404,
/// as one that does not exist. A call retried after a failure finishes the job.
async fn delete_file(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Path(file_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let file_id = validate_run_id("file_id", file_id)?;
    if state.store.visible(&user).file(&file_id).await?.is_none() {
        return Err(ApiError::NotFound(format!("no file {file_id}")));
    }
    // Temporal unreachable is an error, not "not indexing": no deletion on a guess.
    if let Some(index) = state.temporal.status_of(&format!("index-{file_id}")).await?
        && index.status == "Running"
    {
        return Err(ApiError::Conflict(format!("file {file_id} is being indexed: delete it when that is done")));
    }
    let stores = files::Stores { store: &state.store, objects: &state.objects };
    let gone = files::remove(&stores, &stores, &user, &file_id).await?;
    tracing::info!(%file_id, %user, gone, "file deleted for a user");
    Ok(Json(json!({ "file_id": file_id, "deleted": true })))
}

/// One Index workflow per file at a time (`index-<file_id>`); the run_id
/// names the chunks it writes, the document_run_id the document read from
/// the file, the mentions_run_id the keywords of its chunks. All are made
/// here, where the request enters the platform.
async fn start_index(state: &AppState, file_id: &str) -> Result<Started, ApiError> {
    let input = json!({
        "file_id": file_id,
        "run_id": uuid::Uuid::now_v7().to_string(),
        "document_run_id": uuid::Uuid::now_v7().to_string(),
        "mentions_run_id": uuid::Uuid::now_v7().to_string(),
    });
    match state.temporal.start_workflow(LLM_QUEUE, "Index", &format!("index-{file_id}"), input).await {
        Ok(started) => Ok(started),
        Err(StartError::AlreadyRunning) => Err(ApiError::Conflict(format!("file {file_id} is already being indexed"))),
        Err(StartError::Other(e)) => Err(ApiError::Internal(e)),
    }
}

#[derive(Serialize)]
struct FileView {
    file_id: String,
    name: String,
    size: u64,
    uploaded_at: u32,
    /// indexed | indexing | failed | not indexed | unknown (Temporal unreachable)
    status: String,
    chunks: u64,
    embed_model: String,
    error: Option<String>,
}

/// Uploaded files with the state of their index: committed chunks from the
/// run registry, and for a file without them, its Index workflow.
async fn list_files(State(state): State<Arc<AppState>>, User(user): User) -> Result<Json<Vec<FileView>>, ApiError> {
    let rows = state.store.visible(&user).files(100).await?;
    let mut views = Vec::with_capacity(rows.len());
    for row in rows {
        let (status, error) = if row.chunks > 0 {
            ("indexed".to_owned(), None)
        } else {
            match state.temporal.status_of(&format!("index-{}", row.file_id)).await {
                Ok(Some(wf)) if wf.status == "Running" => ("indexing".to_owned(), None),
                Ok(Some(wf)) if wf.error.is_some() => ("failed".to_owned(), wf.error),
                // Completed, but its marker is not read yet: the next poll shows it.
                Ok(Some(_)) => ("indexing".to_owned(), None),
                Ok(None) => ("not indexed".to_owned(), None),
                Err(e) => {
                    tracing::warn!(error = format!("{e:#}"), "index status");
                    ("unknown".to_owned(), None)
                }
            }
        };
        views.push(FileView {
            file_id: row.file_id,
            name: row.name,
            size: row.size,
            uploaded_at: row.uploaded_at,
            status,
            chunks: row.chunks,
            embed_model: row.embed_model,
            error,
        });
    }
    Ok(Json(views))
}

#[derive(Deserialize)]
struct DocumentsQuery {
    /// Themes per document: every theme is stored with its rank, the top
    /// is chosen here.
    #[serde(default = "default_top_themes")]
    top: u32,
}

fn default_top_themes() -> u32 {
    15
}

/// Documents read from the uploaded files: a preprint and the published
/// version of a paper are one document with two files.
async fn list_documents(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Query(q): Query<DocumentsQuery>,
) -> Result<Json<Vec<DocumentRow>>, ApiError> {
    Ok(Json(state.store.visible(&user).documents(100, q.top.min(100)).await?))
}

#[derive(Serialize)]
struct NewChat {
    chat_id: String,
}

/// A new conversation: its id, made here, and the user's link to it. The
/// Chat workflow starts with the first message.
async fn new_chat(State(state): State<Arc<AppState>>, User(user): User) -> Result<Json<NewChat>, ApiError> {
    let chat_id = uuid::Uuid::now_v7().to_string();
    state.store.add_chat_owner(&user, &chat_id).await?;
    Ok(Json(NewChat { chat_id }))
}

/// The user's conversations, the latest first, for the chat page to go back to one.
async fn list_chats(State(state): State<Arc<AppState>>, User(user): User) -> Result<Json<Vec<ChatSummary>>, ApiError> {
    Ok(Json(state.store.visible(&user).chats(50).await?))
}

#[derive(Serialize)]
struct SourceView {
    file_id: String,
    index_run_id: String,
    chunk_id: u32,
    /// Empty once the file is deleted: the answer keeps the keys it used.
    name: String,
}

#[derive(Serialize)]
struct MessageView {
    turn: u32,
    role: String,
    text: String,
    sources: Vec<SourceView>,
}

/// The committed turns of a chat, in order: what the chat page shows when it
/// opens one. A chat with no committed turn yet has none.
async fn chat_history(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Path(chat_id): Path<String>,
) -> Result<Json<Vec<MessageView>>, ApiError> {
    let chat_id = validate_run_id("chat_id", chat_id)?;
    let visible = state.store.visible(&user);
    if !visible.owns_chat(&chat_id).await? {
        return Err(ApiError::NotFound(format!("no chat {chat_id}")));
    }
    let rows = visible.chat_messages(&chat_id).await?;
    let file_ids: Vec<String> = rows
        .iter()
        .flat_map(|r| r.sources.iter().map(|(f, _, _)| f.clone()))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    // A source the user no longer has (deleted since) shows without a name.
    let names = if file_ids.is_empty() { Default::default() } else { visible.file_names(&file_ids).await? };
    let messages = rows
        .into_iter()
        .map(|r| MessageView {
            turn: r.turn,
            role: r.role,
            text: r.text,
            sources: r
                .sources
                .into_iter()
                .map(|(file_id, index_run_id, chunk_id)| SourceView {
                    name: names.get(&file_id).cloned().unwrap_or_default(),
                    file_id,
                    index_run_id,
                    chunk_id,
                })
                .collect(),
        })
        .collect();
    Ok(Json(messages))
}

#[derive(Deserialize)]
struct MessageRequest {
    text: String,
}

/// One message of a chat, answered from the uploaded files:
/// `POST /api/chats/<chat_id>/messages` with `{"text": "..."}`. Waits for the
/// answer: seconds to a minute for a small model on the local GPU.
///
/// The chat must be the user's. The user_id goes with the message to the
/// workflow, and every step of the answer reads only the user's files with
/// it: the workflow itself keeps no identity.
async fn send_message(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Path(chat_id): Path<String>,
    Json(req): Json<MessageRequest>,
) -> Result<Json<Value>, ApiError> {
    let chat_id = validate_run_id("chat_id", chat_id)?;
    let text = req.text.trim();
    if text.is_empty() || text.chars().count() > MAX_MESSAGE_CHARS {
        return Err(ApiError::BadRequest(format!("text: 1-{MAX_MESSAGE_CHARS} characters")));
    }
    if !state.store.visible(&user).owns_chat(&chat_id).await? {
        return Err(ApiError::NotFound(format!("no chat {chat_id}")));
    }
    // The answer's run, made where the request enters the platform (principle 5).
    let run_id = uuid::Uuid::now_v7().to_string();
    match state.temporal.send_message(&chat_id, &run_id, text, &user).await {
        Ok(answer) => Ok(Json(answer)),
        Err(UpdateError::Failed(message)) => Err(ApiError::Upstream(message)),
        Err(UpdateError::Other(e)) => Err(ApiError::Internal(e)),
    }
}

#[derive(Deserialize)]
struct ExamplesQuery {
    #[serde(default)]
    mistakes: bool,
    #[serde(default = "default_examples")]
    limit: u64,
}

fn default_examples() -> u64 {
    24
}

#[derive(Serialize)]
struct ExampleView {
    idx: u32,
    label: u8,
    predicted: u8,
    confidence: f32,
    /// 784 grayscale bytes, base64: ~1 KB per image instead of ~3 KB of JSON numbers.
    pixels: String,
}

async fn examples(
    State(state): State<Arc<AppState>>,
    Path(run_id): Path<String>,
    Query(q): Query<ExamplesQuery>,
) -> Result<Json<Vec<ExampleView>>, ApiError> {
    use base64::Engine;
    let rows = state.store.examples(&run_id, q.mistakes, q.limit.min(100)).await?;
    Ok(Json(
        rows.into_iter()
            .map(|e| ExampleView {
                idx: e.idx,
                label: e.label,
                predicted: e.predicted,
                confidence: e.confidence,
                pixels: base64::engine::general_purpose::STANDARD.encode(&e.pixels),
            })
            .collect(),
    ))
}

async fn digit_report(
    State(state): State<Arc<AppState>>,
    Path(run_id): Path<String>,
) -> Result<Json<Vec<DigitCount>>, ApiError> {
    Ok(Json(state.store.digit_report(&run_id).await?))
}

async fn confusion(
    State(state): State<Arc<AppState>>,
    Path(run_id): Path<String>,
) -> Result<Json<Vec<ConfusionCell>>, ApiError> {
    Ok(Json(state.store.confusion(&run_id).await?))
}

/// Everything the page shows, in one call it polls. Each part fails on its
/// own: with MLflow down the page still shows the data and the workflows.
#[derive(Serialize)]
struct PageState {
    stats: Option<Stats>,
    batches: Option<Vec<Batch>>,
    workflows: Value,
    models: Option<Vec<ModelVersion>>,
    predictions: Option<Vec<PredictionRun>>,
    digits: Option<Vec<DigitAccuracy>>,
    errors: Vec<String>,
}

/// The value of one part of the page state, or None and a note in `errors`.
fn part<T>(errors: &mut Vec<String>, name: &str, result: anyhow::Result<T>) -> Option<T> {
    result
        .map_err(|e| {
            tracing::warn!(part = name, error = format!("{e:#}"), "page state");
            errors.push(format!("{name}: unavailable"));
        })
        .ok()
}

async fn page_state(State(state): State<Arc<AppState>>) -> Json<PageState> {
    let mut errors = Vec::new();
    let (stats, batches, models, predictions, train, run, report) = tokio::join!(
        state.store.stats(),
        state.store.batches(),
        state.mlflow.versions(),
        state.store.prediction_runs(10),
        state.temporal.status(Pipeline::Train),
        state.temporal.status(Pipeline::Run),
        state.temporal.status(Pipeline::Report),
    );
    let stats = part(&mut errors, "data", stats);
    let batches = part(&mut errors, "data", batches);
    let models = part(&mut errors, "models", models);
    let predictions = part(&mut errors, "predictions", predictions);
    let train: Option<Option<Status>> = part(&mut errors, "temporal", train);
    let run: Option<Option<Status>> = part(&mut errors, "temporal", run);
    let report: Option<Option<Status>> = part(&mut errors, "temporal", report);

    let latest = predictions.as_ref().and_then(|p| p.first()).map(|p| p.run_id.clone());
    let digits = match latest {
        Some(run_id) => part(&mut errors, "predictions", state.store.digit_accuracy(&run_id).await),
        None => None,
    };
    errors.dedup();

    Json(PageState {
        stats,
        batches,
        workflows: json!({ "train": train.flatten(), "run": run.flatten(), "report": report.flatten() }),
        models,
        predictions,
        digits,
        errors,
    })
}

fn validate_batch_id(id: String) -> Result<String, ApiError> {
    let well_formed =
        (1..=64).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_');
    if !well_formed || id == TEST_BATCH_ID {
        return Err(ApiError::BadRequest(
            "batch_id: 1-64 letters, digits, '-' or '_', and not the reserved test batch id".into(),
        ));
    }
    Ok(id)
}

/// A run id sent by a client: 1-64 letters, digits, '-' or '_'.
fn validate_run_id(field: &str, id: String) -> Result<String, ApiError> {
    let well_formed =
        (1..=64).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_');
    if !well_formed {
        return Err(ApiError::BadRequest(format!("{field}: 1-64 letters, digits, '-' or '_'")));
    }
    Ok(id)
}

pub enum ApiError {
    BadRequest(String),
    /// No valid platform token: the product routes are for a user.
    Unauthorized(String),
    NotFound(String),
    Conflict(String),
    /// A workflow behind the API failed: the message is its error.
    Upstream(String),
    Internal(anyhow::Error),
}

impl From<anyhow::Error> for ApiError {
    fn from(e: anyhow::Error) -> Self {
        ApiError::Internal(e)
    }
}

impl From<IngestError> for ApiError {
    fn from(e: IngestError) -> Self {
        match e {
            IngestError::Exhausted(n) => ApiError::Conflict(format!("all {n} MNIST train examples are already added")),
            IngestError::Store(e) => ApiError::Internal(e),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            ApiError::BadRequest(m) => (StatusCode::BAD_REQUEST, m),
            ApiError::Unauthorized(m) => (StatusCode::UNAUTHORIZED, m),
            ApiError::NotFound(m) => (StatusCode::NOT_FOUND, m),
            ApiError::Conflict(m) => (StatusCode::CONFLICT, m),
            ApiError::Upstream(m) => (StatusCode::BAD_GATEWAY, m),
            ApiError::Internal(e) => {
                tracing::error!(error = format!("{e:#}"), "request failed");
                (StatusCode::INTERNAL_SERVER_ERROR, "internal error".to_owned())
            }
        };
        (status, Json(json!({ "error": message }))).into_response()
    }
}

#[cfg(test)]
mod tests {
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    use super::*;

    /// Validation fails before any I/O, so nothing listens behind these.
    fn app() -> Router {
        router(Arc::new(AppState {
            store: ClickHouse::new(clickhouse::Client::default().with_url("http://127.0.0.1:9")),
            mnist: Source::new("http://127.0.0.1:9/".into()),
            temporal: Temporal::new("http://127.0.0.1:9".into(), "default".into()),
            mlflow: Mlflow::new("http://127.0.0.1:9".into()),
            ingest_lock: Mutex::default(),
            objects: ObjectStore::new("http://127.0.0.1:9", "pipeline", "key", "secret").unwrap(),
            verifier: Verifier::from_pem(crate::auth::tests::PUBLIC).unwrap(),
        }))
    }

    /// The header of a valid platform token of a user.
    fn bearer() -> String {
        format!("Bearer {}", crate::auth::tests::token(crate::auth::tests::USER))
    }

    async fn post(body: &str) -> StatusCode {
        let req =
            Request::post("/api/batches").header("content-type", "application/json").body(Body::from(body.to_owned()));
        app().oneshot(req.unwrap()).await.unwrap().status()
    }

    #[tokio::test]
    async fn rejects_bad_requests_before_touching_storage() {
        assert_eq!(post(r#"{"count": 0}"#).await, StatusCode::BAD_REQUEST);
        assert_eq!(post(r#"{"count": 60001}"#).await, StatusCode::BAD_REQUEST);
        assert_eq!(post(r#"{"count": 10, "batch_id": "mnist-test"}"#).await, StatusCode::BAD_REQUEST);
        assert_eq!(post(r#"{"count": 10, "batch_id": "a b"}"#).await, StatusCode::BAD_REQUEST);
        assert_eq!(post(r#"{"batch_id": "x"}"#).await, StatusCode::UNPROCESSABLE_ENTITY);
    }

    async fn upload(name: &str, body: &'static [u8]) -> StatusCode {
        let req = Request::post(format!("/api/files?name={name}"))
            .header("authorization", bearer())
            .body(Body::from(body))
            .unwrap();
        app().oneshot(req).await.unwrap().status()
    }

    #[tokio::test]
    async fn refuses_files_that_cannot_be_indexed_before_storing_them() {
        assert_eq!(upload("photo.png", b"x").await, StatusCode::BAD_REQUEST);
        assert_eq!(upload("empty.md", b"").await, StatusCode::BAD_REQUEST);
        assert_eq!(upload("binary.txt", &[0xff, 0xfe]).await, StatusCode::BAD_REQUEST);
        let no_name = Request::post("/api/files").header("authorization", bearer()).body(Body::from("text")).unwrap();
        assert_eq!(app().oneshot(no_name).await.unwrap().status(), StatusCode::BAD_REQUEST);
    }

    async fn message(chat_id: &str, body: &str) -> StatusCode {
        let req = Request::post(format!("/api/chats/{chat_id}/messages"))
            .header("authorization", bearer())
            .header("content-type", "application/json")
            .body(Body::from(body.to_owned()));
        app().oneshot(req.unwrap()).await.unwrap().status()
    }

    #[tokio::test]
    async fn refuses_chat_messages_before_reaching_temporal() {
        assert_eq!(message("chat-1", r#"{"text": "   "}"#).await, StatusCode::BAD_REQUEST);
        let long = format!(r#"{{"text": "{}"}}"#, "a".repeat(MAX_MESSAGE_CHARS + 1));
        assert_eq!(message("chat-1", &long).await, StatusCode::BAD_REQUEST);
        assert_eq!(message("a%20b", r#"{"text": "hi"}"#).await, StatusCode::BAD_REQUEST);
        assert_eq!(message("chat-1", r#"{}"#).await, StatusCode::UNPROCESSABLE_ENTITY);
    }

    #[tokio::test]
    async fn refuses_a_malformed_file_id_before_any_lookup() {
        let req = Request::delete("/api/files/a%20b").header("authorization", bearer()).body(Body::empty()).unwrap();
        assert_eq!(app().oneshot(req).await.unwrap().status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn refuses_a_malformed_chat_id_before_reading_history() {
        let req =
            Request::get("/api/chats/a%20b/messages").header("authorization", bearer()).body(Body::empty()).unwrap();
        assert_eq!(app().oneshot(req).await.unwrap().status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn the_product_routes_need_a_valid_token_before_anything_is_read() {
        let expired = crate::auth::tests::token_with(
            crate::auth::tests::PRIVATE,
            crate::auth::tests::USER,
            crate::auth::AUDIENCE,
            crate::auth::ISSUER,
            -120,
        );
        for (method, path) in [
            ("GET", "/api/files"),
            ("POST", "/api/files?name=a.md"),
            ("DELETE", "/api/files/abc"),
            ("POST", "/api/files/abc/index"),
            ("GET", "/api/documents"),
            ("GET", "/api/chats"),
            ("POST", "/api/chats"),
            ("GET", "/api/chats/c-1/messages"),
            ("POST", "/api/chats/c-1/messages"),
        ] {
            for auth in [None, Some("Bearer not-a-token".to_owned()), Some(format!("Bearer {expired}"))] {
                let mut req = Request::builder().method(method).uri(path).header("content-type", "application/json");
                if let Some(auth) = &auth {
                    req = req.header("authorization", auth);
                }
                let res = app().oneshot(req.body(Body::from(r#"{"text": "hi"}"#)).unwrap()).await.unwrap();
                assert_eq!(res.status(), StatusCode::UNAUTHORIZED, "{method} {path} with {auth:?}");
            }
        }
    }

    #[tokio::test]
    async fn healthz_answers() {
        let res = app().oneshot(Request::get("/healthz").body(Body::empty()).unwrap()).await.unwrap();
        assert_eq!(res.status(), StatusCode::OK);
    }
}
