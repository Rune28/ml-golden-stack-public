# ClickHouse schema

How principles 4–6 of the root CLAUDE.md are implemented for the tables in `ml_data`, as they stand today.

## Migrations

- golang-migrate, applied by the `clickhouse-migrate` Job before the applications start. One statement per file, an `up` and a `down` per number.
- A migration on `main` is changed by a new migration: a cluster that recorded it as done will not run it again, so an edit would silently not reach it. A migration that exists only on the working branch can be edited; the local kind cluster is disposable, and after such an edit it has to be recreated (`make platform-down && make platform-up`, ask first: it drops the data). Correcting a comment in a migration on `main` is fine: it changes no schema.
- Compatible changes (principle 6) go expand, then contract. Adding: the migration ships with or before the code that uses it. Removing or renaming: the code stops using the column first, and the migration that drops it comes in a later release. Migration 0005 is the worked example: `run_id` added with `DEFAULT report_run_id`, the old column to be dropped later.
- The header comment of a migration says why the table or the change exists; keep that style.

## Keys of run outputs

Tables written by pipeline runs (`predictions`, `digit_report`, `doc_chunks`):

- `run_id` is the run that wrote the rows: the unit of rewrite and of idempotency.
- The keys of the inputs keep their name downstream as `<parent>_run_id` (`prediction_run_id`), so lineage is a join. Source data from be_service uses `batch_id`.
- A writer deletes or rewrites rows filtered by its own `run_id`.

Reference data uses its natural key instead: `files` is keyed by `file_id`, the sha256 of the bytes.

## Run registry and views

- A step writes its rows, then commits a marker to `ml_data.runs` (`commit_run`). The marker is written by the platform layer (the `jobs` package in a Job, or the activity in the worker), not by model code.
- `started_at` is the logical start: when the workflow scheduled the step, the same on retries. The latest run is chosen by it, not by the time of an insert.
- Readers of results (be_service, retrieval) read views over `runs`, such as `digit_report_latest` and `doc_chunks_current`, so a table can change its layout without them noticing. A writer checking its own progress reads its table directly, and so can ad-hoc and debugging queries.

## ClickHouse specifics

- `ReplacingMergeTree` keeps duplicates until a merge: read such tables with `FINAL` or one row per key (`LIMIT 1`, `argMax`).
- A column that another column's `DEFAULT` refers to cannot be dropped: `MATERIALIZE COLUMN`, `MODIFY COLUMN ... REMOVE DEFAULT`, then `DROP COLUMN`.
