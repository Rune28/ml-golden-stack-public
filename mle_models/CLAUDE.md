# mle_models

The image the pipeline Jobs run: `python -m jobs <step> ...`.

| Package | Role |
|---|---|
| `mnist` | the model library: arrays in, a model and arrays out. No ClickHouse, MLflow, S3 or environment variables. |
| `jobs` | the platform layer: reads inputs, calls the library, writes results (ClickHouse, the MLflow registry, `result.json` in S3) and commits the run. |

## The split (principle 2)

- Code that touches storage, the registry or configuration belongs in `jobs`; computation belongs in the library. When a library function needs data, `jobs` reads it and passes it in.
- A new model is onboarded in four steps: run the notebook, keep its output as a golden test, move the code into a library, run it as a Job. It is accepted when the Job reproduces the notebook within an agreed tolerance.
- The library runs on CPU when no CUDA device is present: the local `gpu` pool is simulated.

## Steps

- A step learns which run it is and what to use (`--run-id`, a model version or alias, parent run ids) from its arguments, and returns a JSON dict: the contract with `run_job`, written to `--result-key`.
- `--started-at` is the logical start from the workflow; it goes on to `commit_run`.
- Writes follow the retry pattern in `jobs/clickhouse.py`: own `run_id` only, complete → skip, partial → rewrite, then `commit_run`.
- `commit_run` has a stream copy in `llm_worker/src/workflow/activities/index.py` (principle 9): a change to one is a change to both.
- Steps report metrics and leave promotion to the workflow; `promote` moves the `champion` alias to the version it is given.

## Tests

`uv run pytest`: model code on mock data (`tests/test_mnist.py`), writers against retries (`tests/test_jobs_clickhouse.py`).
