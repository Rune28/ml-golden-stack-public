# batch_worker

Temporal worker of the `batch` task queue: workflows `Train`, `Run`, `Report` and one activity, `run_job`, which runs a step of the mle-models image as a Kubernetes Job and waits for it (principle 3). The worker holds neither torch nor the dataset; the Job does.

## Workflows and contracts

- Temporal replays workflow code, so it stays deterministic: I/O, clocks, randomness and environment reads go into activities.
- `workflow/contracts.py` is the API with be_service and the temporal CLI. The dataclasses travel as JSON, so field names are the contract: add fields with defaults; renaming or removing one is a breaking change to plan (principle 6).
- Changing the sequence of activities in a workflow breaks the replay of workflows already running. Locally they can be terminated; say so when a change does this.
- Promotion is decided in the workflow from the metrics the train step returns (`train.py`).

## run_job (principles 4, 5)

- The Job is named from step and `run_id`, so a retried activity finds the Job it created and waits for it.
- The pod retries once (`backoff_limit=1`) and the activity has its own retries; the two are kept from multiplying.
- A failed Job is raised with `non_retryable=True`: a retry would find the same failed Job.
- The logical start of the run, `activity.info().scheduled_time`, goes to the Job as `--started-at` for the run registry.
- Cancelling the workflow deletes the Job; the result is read from `result.json` in S3.
- The Job runs on a pool from the cluster contract (`cpu`, `gpu`); the `gpu` pool gets the toleration and the `nvidia.com/gpu` resource.

The Kubernetes client and boto3 are blocking, so `run_job` is a sync activity on the worker's thread pool.

## Tests

`uv run pytest`. A change to `run_job` comes with a test of its retry case (`tests/test_run_job.py`).
