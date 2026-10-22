# llm_worker

Temporal worker of the `llm` task queue: events processed in the worker as they come, against the long-running model server (principle 3). The workflow `Index` with its activities `index_file`, `read_document` and `extract_mentions`, and the entity workflow `Chat` (one per conversation, a message is an Update) with `route`, then `gather` and `summarize` for a topic or `retrieve` and `generate` for a fact, and `record`. `route` tells a fact, a topic, or the user's own story to interpret (answered as a fact: retrieved by the message as written) and reads a short query a topic is gathered and themed by. A message that lists aspects (жизнь, смерть, любовь) or names two or more of the user's authors, groups or kinds (`compare`) is answered in sections: each aspect in each side's documents gathered apart with a share of the chunks (`gather` with `fill`), then a paragraph per aspect, each note marked with whose documents it is from. No Kubernetes client: this worker starts no Jobs.

`rag` is chunking, PDF text and the keys and metadata of documents, with no I/O (principle 2); `workflow/llm.py` is the adapter to the model server.

## Profiles

What depends on the corpus is a profile, a file in `src/workflow/profiles/` (`rag/profiles.py` says what it holds): what counts as a fact, a topic and a story to interpret, with examples; how to pick the terms; examples and thresholds of synonyms; whose words a passage gives. The steps and the prompt templates in `rag/topics.py` are the same for every corpus.

- A chat's profile is fixed by its first message that names one, or by the default its first route resolved (`LLM_DEFAULT_PROFILE`, else `default`). The workflow carries the name; activities read the file. route returns the name and the version (a hash of the file) into the workflow history.
- The texts go into the prompts exactly as written, line breaks included: for qwen3:4b the layout moved a question between kinds. An edit of a profile is a new version: measure the route on that corpus's control questions (5 runs a question) before the release, as for a prompt in code: `make route-check P=<profile>`, the questions in `src/workflow/profiles/control/<profile>.toml`. A question that went wrong in a chat goes there.
- The kinds themselves (`KINDS`) are code, not profile: each is a path in the workflow. A new kind is a change to the workflow. `compare` is not asked of the model: route sets it when the message names two sides (`sides_of`).
- What a small model is told not to do, it often does: a rule that can be checked in code (such as `document_words`, terms that only name documents) is enforced in the activity, not left to the prompt.

## Workflows and contracts

- Temporal replays workflow code, so it stays deterministic: I/O, clocks, randomness and environment reads go into activities.
- `workflow/contracts.py` is the API with be_service. The dataclasses travel as JSON, so field names are the contract: add fields with defaults; renaming or removing one is a breaking change to plan (principle 6).
- Changing the sequence of activities in a workflow breaks the replay of workflows already running, which matters most for long-lived workflows such as a chat. Locally they can be terminated; say so when a change does this.

## The model server

- Language models are reached through `workflow/llm.py`: OpenAI-compatible API, server from `LLM_BASE_URL`, models from `LLM_CHAT_MODEL` and `LLM_EMBED_MODEL`. Swapping Ollama for vLLM or a managed endpoint then changes configuration, not callers.
- Vectors of different embedding models cannot be compared: chunks carry `embed_model`, and retrieval filters by the model it embeds the question with.
- An activity calls the server a batch at a time and heartbeats after each batch, so the worker holds one batch of vectors, not a whole file's.
- Reading fields out of a text (`chat_json`: a JSON schema, thinking off) takes a second; with thinking, qwen3:4b spends a minute on the same answer.
- Keys of documents (DOI, ISBN) come from the text by regular expressions, and only from its first pages, where the model reads the metadata too: the references of a paper carry the DOIs of other papers.
- A file's chunks are indexed once each (`rag.chunking.distinct`), and retrieval takes one text once and only chunks of files that exist (an index committed after its file was deleted stays unseen).

## What a user may read

- Every message carries the `user_id` be_service took from the platform token, and every step of an answer gets it in its input. The steps read user data only through the views `*_visible(user_id = {u:String})` and, for texts by key, `doc_chunks_owned` (db/CLAUDE.md); an empty `user_id` reads nothing. `tests/test_visible.py` fails when `activities/chat.py`, `topic.py` or `scope.py` names a table or a current view of user data.
- The workflow `Chat` keeps no identity: a message is authorized when it comes.
- A chat's scope (`Scope`: groups, authors, kinds, years, files; docs/search-scope-plan.md) is resolved to files by `resolve_scope` through `documents_visible` and `collections_visible`, every message. The steps that search add `empty(f) OR has(f, file_id)` over their `*_visible` reads: a scope narrows what the user may see, never widens it, and a scope of no file is answered as such, not from every file. What a message names itself ("в блогах", "у Мамардашвили", "в книге") narrows it further: matched on the words (`rag.topics.where_in`), since asked of the model with the lists it picked them all.

## Retry safety (principles 4, 5)

- `index_file` counts the rows of its own run: complete → skip, partial → delete this run's rows and write again; then `commit_run`, last.
- `read_document` checks its marker first: committed → return it without asking the model again; otherwise delete this run's rows in `sat_document` and `link_document_author`, write them again, then `commit_run`.
- `commit_run` here is the stream copy of the one in `mle_models/src/jobs/clickhouse.py` (principle 9): the marker is written by whoever wrote the rows. A change to one is a change to both.
- The logical start passed to the registry is `activity.info().scheduled_time`, stable across retries.
- An error that a retry would only repeat (a file with no text) is raised with `non_retryable=True`.

ClickHouse, S3 and the LLM adapter are blocking, so activities are sync and run on the worker's thread pool, with a ClickHouse client per call, since activities run on several threads at once.

## Tests

`uv run pytest`. A change to a writer comes with a test of its retry case (`tests/test_index.py`, `tests/test_document.py`).
