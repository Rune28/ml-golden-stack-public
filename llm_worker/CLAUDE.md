# llm_worker

Temporal worker of the `llm` task queue: events processed in the worker as they come, against the long-running model server (principle 3). The workflow `Index` with its activity `index_file`, and the entity workflow `Chat` (one per conversation, a message is an Update) with `retrieve`, `generate` and `record`. No Kubernetes client: this worker starts no Jobs.

`rag` is chunking with no I/O (principle 2); `workflow/llm.py` is the adapter to the model server.

## Workflows and contracts

- Temporal replays workflow code, so it stays deterministic: I/O, clocks, randomness and environment reads go into activities.
- `workflow/contracts.py` is the API with be_service. The dataclasses travel as JSON, so field names are the contract: add fields with defaults; renaming or removing one is a breaking change to plan (principle 6).
- Changing the sequence of activities in a workflow breaks the replay of workflows already running, which matters most for long-lived workflows such as a chat. Locally they can be terminated; say so when a change does this.

## The model server

- Language models are reached through `workflow/llm.py`: OpenAI-compatible API, server from `LLM_BASE_URL`, models from `LLM_CHAT_MODEL` and `LLM_EMBED_MODEL`. Swapping Ollama for vLLM or a managed endpoint then changes configuration, not callers.
- Vectors of different embedding models cannot be compared: chunks carry `embed_model`, and retrieval filters by the model it embeds the question with.
- An activity calls the server a batch at a time and heartbeats after each batch, so the worker holds one batch of vectors, not a whole file's.
- A file's chunks are indexed once each (`rag.chunking.distinct`), and retrieval takes one text once and only chunks of files that exist (an index committed after its file was deleted stays unseen).

## Retry safety (principles 4, 5)

- `index_file` counts the rows of its own run: complete → skip, partial → delete this run's rows and write again; then `commit_run`, last.
- `commit_run` here is the stream copy of the one in `mle_models/src/jobs/clickhouse.py` (principle 9): the marker is written by whoever wrote the rows. A change to one is a change to both.
- The logical start passed to the registry is `activity.info().scheduled_time`, stable across retries.
- An error that a retry would only repeat (a file with no text) is raised with `non_retryable=True`.

ClickHouse, S3 and the LLM adapter are blocking, so activities are sync and run on the worker's thread pool, with a ClickHouse client per call, since activities run on several threads at once.

## Tests

`uv run pytest`. A change to a writer comes with a test of its retry case (`tests/test_index.py`).
