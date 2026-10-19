//! Temporal client: starts the workflows of batch_worker (Train, Run, Report)
//! and llm_worker (Index) and reports their state. be_service is the platform
//! API and the only Temporal client; the workers (Python) execute the
//! workflows, one per task queue. The contract is the workflow type name, the
//! task queue and the JSON input.

use std::collections::HashMap;
use std::time::SystemTime;

use anyhow::{Context, Result, anyhow};
use serde::Serialize;
use temporalio_client::errors::{
    WorkflowGetResultError, WorkflowStartError, WorkflowUpdateError, WorkflowUpdateWithStartError,
};
use temporalio_client::{
    Client, ClientOptions, ConnectionOptions, RpcOptions, UntypedUpdate, UntypedWorkflow, Url, WorkflowDescribeOptions,
    WorkflowExecutionStatus, WorkflowGetResultOptions, WorkflowIdConflictPolicy, WorkflowStartOptions,
    WorkflowUpdateWithStartOptions,
};
use temporalio_common::data_converters::RawValue;
use temporalio_common::protos::temporal::api::common::v1::Payload;
use temporalio_common::protos::{ENCODING_PAYLOAD_KEY, JSON_ENCODING_VAL};
use tokio::sync::OnceCell;

/// batch_worker: work of models over datasets, as Kubernetes Jobs: Train, Run, Report.
pub const BATCH_QUEUE: &str = "batch";
/// llm_worker: events processed in the worker against the LLM server: Index, Chat.
pub const LLM_QUEUE: &str = "llm";

/// The client waits for an answer by polling: each poll is a long poll the
/// server holds for about a minute, and the client gives it 70 seconds by
/// default. A server slow to answer one poll (a host swapping) ended the
/// wait with "Timeout expired" while the answer was still being written;
/// the client does not retry that. A wider deadline per poll keeps the wait
/// going; the whole wait is bounded by the caller (the chat page, 6 minutes).
const UPDATE_POLL_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(120);

/// One button, one fixed workflow ID: a second press while the first run is
/// in progress is refused by Temporal instead of starting a parallel run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Pipeline {
    Train,
    Run,
    Report,
}

impl Pipeline {
    pub fn workflow_type(self) -> &'static str {
        match self {
            Pipeline::Train => "Train",
            Pipeline::Run => "Run",
            Pipeline::Report => "Report",
        }
    }

    pub fn workflow_id(self) -> &'static str {
        match self {
            Pipeline::Train => "train",
            Pipeline::Run => "run",
            Pipeline::Report => "report",
        }
    }
}

#[derive(Serialize, Debug)]
pub struct Started {
    pub workflow_id: String,
    pub run_id: String,
}

#[derive(Serialize, Debug)]
pub struct Status {
    pub workflow_id: String,
    pub status: String,
    pub started_at: Option<u64>,
    pub closed_at: Option<u64>,
    /// The workflow's return value once completed (TrainResult / RunResult).
    pub result: Option<serde_json::Value>,
    pub error: Option<String>,
}

pub enum StartError {
    AlreadyRunning,
    Other(anyhow::Error),
}

pub enum UpdateError {
    /// The workflow refused the message or a step of the answer failed.
    Failed(String),
    Other(anyhow::Error),
}

pub struct Temporal {
    address: String,
    namespace: String,
    // Connected on first use: be_service starts even while Temporal is down.
    client: OnceCell<Client>,
}

impl Temporal {
    pub fn new(address: String, namespace: String) -> Self {
        Self { address, namespace, client: OnceCell::new() }
    }

    async fn client(&self) -> Result<&Client> {
        self.client
            .get_or_try_init(|| async {
                let url = Url::parse(&self.address).with_context(|| format!("TEMPORAL_ADDRESS {}", self.address))?;
                let connection = ConnectionOptions::new(url).identity("be_service").build();
                let client = Client::connect(connection, ClientOptions::new(self.namespace.clone()).build()).await?;
                Ok(client)
            })
            .await
    }

    pub async fn start(&self, pipeline: Pipeline, input: serde_json::Value) -> Result<Started, StartError> {
        self.start_workflow(BATCH_QUEUE, pipeline.workflow_type(), pipeline.workflow_id(), input).await
    }

    /// Starts a workflow under an ID of its own, e.g. `index-<file_id>`: a
    /// second start while one runs is refused, a start after it ended runs again.
    pub async fn start_workflow(
        &self,
        task_queue: &str,
        workflow_type: &str,
        workflow_id: &str,
        input: serde_json::Value,
    ) -> Result<Started, StartError> {
        let client = self.client().await.map_err(StartError::Other)?;
        let options = WorkflowStartOptions::new(task_queue, workflow_id)
            .id_conflict_policy(WorkflowIdConflictPolicy::Fail)
            .build();
        match client.start_workflow(UntypedWorkflow::new(workflow_type), json_payload(&input), options).await {
            Ok(handle) => Ok(Started {
                workflow_id: workflow_id.to_owned(),
                run_id: handle.info().run_id.clone().unwrap_or_default(),
            }),
            Err(WorkflowStartError::AlreadyStarted { .. }) => Err(StartError::AlreadyRunning),
            Err(e) => Err(StartError::Other(e.into())),
        }
    }

    /// A message to the chat `chat-<chat_id>`, which starts with its first
    /// message (UpdateWithStart), and the answer, once it is recorded. The
    /// Update ID is the answer's run_id: a call retried with it is answered
    /// once.
    pub async fn send_message(
        &self,
        chat_id: &str,
        run_id: &str,
        text: &str,
    ) -> Result<serde_json::Value, UpdateError> {
        let client = self.client().await.map_err(UpdateError::Other)?;
        let options = WorkflowUpdateWithStartOptions::new(
            LLM_QUEUE,
            format!("chat-{chat_id}"),
            WorkflowIdConflictPolicy::UseExisting,
        )
        .update_id(run_id)
        .rpc_options(RpcOptions::builder().timeout(UPDATE_POLL_TIMEOUT).build())
        .build();
        let input = serde_json::json!({ "chat_id": chat_id });
        let message = serde_json::json!({ "text": text, "run_id": run_id });
        let result = client
            .execute_update_with_start_workflow(
                UntypedWorkflow::new("Chat"),
                json_payload(&input),
                UntypedUpdate::<UntypedWorkflow>::new("send_message"),
                json_payload(&message),
                options,
            )
            .await;
        match result {
            Ok(answer) => {
                from_json_payload(answer).ok_or_else(|| UpdateError::Other(anyhow!("an answer without a payload")))
            }
            Err(WorkflowUpdateWithStartError::Update(WorkflowUpdateError::Failed(failure))) => {
                Err(UpdateError::Failed(failure_message(&format!("{failure:?}"))))
            }
            Err(e) => Err(UpdateError::Other(e.into())),
        }
    }

    /// State of the latest execution of the pipeline's workflow, None if it never ran.
    pub async fn status(&self, pipeline: Pipeline) -> Result<Option<Status>> {
        self.status_of(pipeline.workflow_id()).await
    }

    /// State of the latest execution of a workflow, None if it never ran.
    pub async fn status_of(&self, workflow_id: &str) -> Result<Option<Status>> {
        let client = self.client().await?;
        let handle = client.get_workflow_handle::<UntypedWorkflow>(workflow_id);
        let description = match handle.describe(WorkflowDescribeOptions::default()).await {
            Ok(d) => d,
            Err(e) if format!("{e:?}").contains("NotFound") => return Ok(None),
            Err(e) => return Err(e.into()),
        };

        let status = description.status();
        let (mut result, mut error) = (None, None);
        // Only finished runs: get_result on a running one would wait for it.
        if !matches!(status, WorkflowExecutionStatus::Running) {
            match handle.get_result(WorkflowGetResultOptions::default()).await {
                Ok(value) => result = from_json_payload(value),
                Err(WorkflowGetResultError::Failed(failure)) => error = Some(failure_message(&format!("{failure:?}"))),
                Err(e) => error = Some(e.to_string()),
            }
        }

        let info = description.raw_description.workflow_execution_info.as_ref();
        Ok(Some(Status {
            workflow_id: workflow_id.to_owned(),
            status: format!("{status:?}"),
            started_at: description.start_time().and_then(epoch_seconds),
            closed_at: info.and_then(|i| i.close_time.as_ref()).map(|t| t.seconds as u64),
            result,
            error,
        }))
    }
}

/// The standard JSON payload every SDK reads: metadata encoding json/plain.
fn json_payload(value: &serde_json::Value) -> RawValue {
    RawValue::new(vec![Payload {
        metadata: HashMap::from([(ENCODING_PAYLOAD_KEY.to_owned(), JSON_ENCODING_VAL.as_bytes().to_vec())]),
        data: serde_json::to_vec(value).expect("JSON value serializes"),
        ..Default::default()
    }])
}

fn from_json_payload(value: RawValue) -> Option<serde_json::Value> {
    value.payloads.first().and_then(|p| serde_json::from_slice(&p.data).ok())
}

fn epoch_seconds(t: SystemTime) -> Option<u64> {
    t.duration_since(SystemTime::UNIX_EPOCH).ok().map(|d| d.as_secs())
}

/// The first `message: "..."` of a failure chain: the error raised in the workflow or activity.
fn failure_message(debug: &str) -> String {
    debug
        .split("message: \"")
        .nth(1)
        .and_then(|rest| rest.split('"').next())
        .map(|m| m.replace("\\n", "\n"))
        .unwrap_or_else(|| debug.chars().take(500).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_payload_round_trips() {
        let value = serde_json::json!({ "run_id": "r1", "pool": "gpu" });

        let raw = json_payload(&value);

        assert_eq!(raw.payloads[0].metadata[ENCODING_PAYLOAD_KEY], b"json/plain");
        assert_eq!(from_json_payload(raw), Some(value));
    }

    #[test]
    fn failure_message_takes_the_first_message() {
        let debug = r#"Failure { message: "Activity task failed", cause: Some(Failure { message: "job x failed" }) }"#;
        assert_eq!(failure_message(debug), "Activity task failed");
    }
}
