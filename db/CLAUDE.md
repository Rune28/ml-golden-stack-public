# ClickHouse schema

How principles 4–6 of the root CLAUDE.md are implemented for the tables in `ml_data`, as they stand today.

## Migrations

- golang-migrate, applied by the `clickhouse-migrate` Job before the applications start. One statement per file, an `up` and a `down` per number.
- A migration on `main` is changed by a new migration: a cluster that recorded it as done will not run it again, so an edit would silently not reach it. A migration that exists only on the working branch can be edited; the local kind cluster is disposable, and after such an edit it has to be recreated (`make platform-down && make stores-down && make platform-up`, ask first: it drops the data). Correcting a comment in a migration on `main` is fine: it changes no schema.
- Compatible changes (principle 6) go expand, then contract. Adding: the migration ships with or before the code that uses it. Removing or renaming: the code stops using the column first, and the migration that drops it comes in a later release. Migration 0005 is the worked example: `run_id` added with `DEFAULT report_run_id`, the old column to be dropped later.
- The header comment of a migration says why the table or the change exists; keep that style.

## Keys of run outputs

Tables written by pipeline runs (`predictions`, `digit_report`, `doc_chunks`, `chat_messages`, `sat_document`, `link_document_author`):

- `run_id` is the run that wrote the rows: the unit of rewrite and of idempotency.
- The keys of the inputs keep their name downstream as `<parent>_run_id` (`prediction_run_id`), so lineage is a join. Source data from be_service uses `batch_id`.
- A writer deletes or rewrites rows filtered by its own `run_id`.

Reference data uses its natural key instead: `files` is keyed by `file_id`, the sha256 of the bytes. Deleting a file (be_service) removes what was made from it in reader order: the markers of its runs (index, document), the document rows, the chunks, the object, the row last.

## Postgres: the database `chat`

- chat_service's own database (principle 9): users, identities, sessions, failed logins. `db/postgres/chat/migrations`, golang-migrate files like ClickHouse's, applied by a Job (`modules/postgres-migrate`) before chat_service starts; expand, then contract, the same way.
- A user is the `user_id` of `users`; how they log in is a row of `identities` (`provider`, `subject`). The owner links in ClickHouse refer to `user_id`, so a new way to log in is a new identity, not a new user.

## What a user may see

- `file_owners` and `chat_owners` link users to files and chats: reference data with a natural key, written and deleted by be_service. The name of a file and the time of its upload are on the link: each user's own.
- Readers of user data (be_service `visible.rs`, the steps of an answer in llm_worker) read only the parameterized views `*_visible(user_id = ...)`, which join the links; `doc_chunks_owned` is the same for texts by key of every run. A new table or current view of user data comes with its `*_visible` view, and readers read that.

## Data Vault of documents

- Satellites (`sat_*`) and links (`link_*`) are outputs of runs, keyed by `run_id` like any other.
- Hubs (`hub_document`, `hub_author`) are views of the keys of current runs, not tables: these keys are found by a model, so a key of a failed or replaced run must not stay visible (principle 4). Business keys are natural (`doi:`, `isbn:`, `file:`; `orcid:`, `name:`), so a hub keeps a stable identity without a table of its own.
- A table-registry of keys becomes worth it once something outside the runs refers to a key; propose it then.

## Run registry and views

- A step writes its rows, then commits a marker to `ml_data.runs` (`commit_run`). The marker is written by the platform layer (the `jobs` package in a Job, or the activity in the worker), not by model code.
- `started_at` is the logical start: when the workflow scheduled the step, the same on retries. The latest run is chosen by it, not by the time of an insert.
- Readers of results (be_service, retrieval) read views over `runs`, such as `digit_report_latest`, `doc_chunks_current` and `chat_messages_committed`, so a table can change its layout without them noticing. A writer checking its own progress reads its table directly, and so can ad-hoc and debugging queries.

## ClickHouse specifics

- `ReplacingMergeTree` keeps duplicates until a merge: read such tables with `FINAL` or one row per key (`LIMIT 1`, `argMax`).
- A column that another column's `DEFAULT` refers to cannot be dropped: `MATERIALIZE COLUMN`, `MODIFY COLUMN ... REMOVE DEFAULT`, then `DROP COLUMN`.
