# LLM conveyor: upload files, RAG, chat

The next stage of the platform: a page where files are uploaded, indexed for retrieval, and a chat answers from them, remembering the conversation. Login sessions come later.

## Why this layout

Models on this platform are meant to continue each other. The chat produces data (questions, answers, the chunks they used); the next classic model learns from it (a reranker, an answer quality classifier) and comes back as a stage of the chat. So the chain of models lives in the platform, in workflows, in tables with inherited keys and in MLflow versions, and the product stays a thin interface:

| Part | Role | Talks to Temporal |
|---|---|---|
| **chat_service** (Rust, new) | product: upload, file list, chat window; login sessions later | no |
| **be_service** | the platform API: the only door into the platform | yes, the only client |
| **llm_worker** | the conveyor: workflows `Index` and `Chat` on the task queue `llm`, a service of its own, apart from batch_worker of the MNIST pipeline; chunking as a pure library `rag` | worker |
| **mle_models** | batch work of models only (MNIST); the corpus re-index for a new embedding model would be a Job here | no |

A future model stage is a new activity inside `Chat`; chat_service does not change.

The LLM side mirrors the MNIST conveyor:

| MNIST | LLM |
|---|---|
| Add data → `samples` batch | upload → `files`, a file in the object store |
| `train` → model version | `index` → chunks and embeddings of a file |
| `predict` → `predictions` | a message → an answer in `chat_messages` |
| a run in `runs` per prediction | a run in `runs` per index and per answer |

## Flow

```
browser ─▶ chat_service (page: upload, files, chat)          ← product, no Temporal
              │ HTTP
              ▼
           be_service = platform API                          ← only Temporal client
              │ POST /api/files ─────────▶ workflow Index(file_id) ─▶ index_file ─▶ doc_chunks + runs
              │ POST /api/chats/{id}/messages ─ UpdateWithStart ─▶ workflow Chat "chat-<chat_id>"
              ▼                                                    ├ retrieve  (top-k from doc_chunks_current)
           Temporal ─▶ llm_worker                                  ├ generate  (LLM through the adapter)
                                                                   └ record    (chat_messages + runs, kind='answer')
```

## Decisions

1. **The LLM server is Ollama on the host.** Installed without sudo (`~/.local`, a systemd user unit), weights on the GPU (RTX 5060, 8 GB), listening only on the kind bridge `172.18.0.1:11434`, not on every interface. A chat model of 3–4B parameters and a small embedding model. Switching a model is `ollama pull` and a new name in the configuration.
2. **`infra/modules/llm-local`** gives the cluster a Service `llm` without a selector and an EndpointSlice pointing to the host; its output is `base_url`. The same pattern as `postgres-local` against a managed database: later a vLLM Deployment in the cluster can stand behind the same Service, and no caller changes.
3. **An LLM adapter in llm_worker** (`workflow/llm.py`): `chat(model, messages)` and `embed(model, texts)` over the OpenAI-compatible API, which Ollama, llama.cpp and vLLM all serve. Models come from `LLM_CHAT_MODEL` and `LLM_EMBED_MODEL`.
4. **Indexing a file is stream work in the worker** (rule 12), not a Job: an upload is one event, and a pod start per file would take longer than the indexing. A pure library `llm_worker/src/rag/` (reading text, chunking, no I/O) and the activity `index_file`, which reads the file from the object store, embeds the chunks 32 at a time with a heartbeat after each batch, writes `doc_chunks` and commits a marker in `runs` (`kind='index'`, parents `file_id`, `embed_model`). Indexing the whole corpus again (a new embedding model) is batch, a Job (rule 3).
5. **A conversation is an entity workflow `Chat`** with the ID `chat-<chat_id>`. Its history is the workflow state; a message is an Update `send_message` whose result is the answer; the first message is an UpdateWithStart, which starts the workflow and delivers the message in one call (`temporalio-client` 1.1 supports both). After N messages the workflow continues as new, so its history stays bounded. An idle chat costs the worker nothing. A login session (cookie) never reaches Temporal.
6. **Rules 3 and 12, batch against stream:** batch is the work of a model over a dataset and runs as a Job (training, the corpus re-index); an event (an uploaded file, a message) is stream work, an activity of the worker against the model server (indexing a file, answering).
7. **Storage follows rules 5 and 11:**
   - `files`: `file_id`, name, size, upload time;
   - `doc_chunks`: own key `run_id` of the index run, `file_id`, `chunk_id`, `embed_model`, `text`, `embedding Array(Float32)`; readers use the view `doc_chunks_current`, the latest committed index of every file (as `digit_report_latest`);
   - `chat_messages`: own key `run_id` of the answer, `chat_id`, `role`, `text`, `sources`, with a marker `kind='answer'` whose parents are the chat and the index runs used, so every answer traces back to the files it came from;
   - `chat_id` and every `run_id` are made by the platform (be_service, UUID v7), never by a caller; `file_id` is the sha256 of the bytes, so the same file uploaded twice, even through two replicas at once, is one file.
   Retrieval is `cosineDistance` over the view, filtered by the current embedding model: vectors of different models are never compared.
8. **File formats:** `.txt` and `.md`; PDF (`pypdf`) closes the indexing slice if it fits.
9. **chat_service has no frontend framework** (like the MNIST page). Without sessions the `chat_id` lives in the page.

## Slices

Each slice ends working end to end. After each one: 2–3 check questions, and the critical function of the slice is written or read line by line by the platform owner (rule 7).

| # | Slice | Built | Critical part | Done when |
|---|---|---|---|---|
| S1 | LLM endpoint | Ollama and models on the host; `modules/llm-local`; the adapter; `make llm-check`; README rule 12 | the contract of `llm-local` and the adapter | a pod in kind gets an answer and an embedding |
| S2 | Files and index | migrations `files`, `doc_chunks` and the view; platform API `POST/GET /api/files`; workflow `Index`; `rag/` and the activity `index_file`; `make upload FILE=...` | chunk keys, idempotent index write | a file becomes chunks in ClickHouse with a `kind='index'` marker; uploading it again duplicates nothing |
| S3 | Conversation | workflow `Chat` (Update, UpdateWithStart, continue-as-new), activities `retrieve`, `generate`, `record`; migration `chat_messages`; platform API `POST /api/chats`, `POST /api/chats/{id}/messages`; `make chat` | the Update handler and continue-as-new | the second question builds on the first; answers name their sources; every step is visible in the Temporal UI |
| S4 | chat_service | a Rust crate on the be_service skeleton; the page: upload, files with their index status, chat window; module, chart, `images.sh`, CI | calling the platform API and its errors | upload a file, ask about it, get an answer from it |

## Later

Cookie sessions in chat_service and a `principal` passed to the platform API (the way to ZTNA) · an agent with tools over `runs` and MLflow · an evaluation set and promotion of a RAG configuration in MLflow · ratings of answers · the next model stage (reranker or answer quality classifier) in place of MNIST · a portal of micro-frontends · serving in the cluster and own weights from MLflow · streaming · Kueue and the GPU in kind · a read-only ClickHouse user for retrieval · questions about a whole book (a summary per chapter, asked before the chunks) · the chat page on a server: an Ingress in front of the chat-service Service (and ingress-nginx on one port mapping when the kind cluster is recreated anyway); on the laptop `make chat-ui-service` port-forwards · switching the embedding model without a blind window: every file indexed with the new model first, retrieval switched after (today both read one `LLM_EMBED_MODEL`, so search finds nothing until files are indexed again).

## Memory budget

The host has 16 GB. Desktop with an editor and a few browser tabs ~3–4 GB, the kind cluster ~2.5–2.9 GB, chat_service ~0.05 GB, Ollama ~1 GB (weights in VRAM), an index Job ~1 GB at its peak: **about 8–9 GB of 15.6**. MNIST training and indexing do not run at the same time.

## Tooling notes

There is no `uv` or `cargo` on the host: Python lock files are updated through the `ghcr.io/astral-sh/uv` image, and Rust builds inside Docker during `make platform-up`.
