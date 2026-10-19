# ml-golden-stack

Reference platform for batch and stream ML: OpenTofu, Kubernetes, Temporal, ClickHouse, MLflow. It runs on kind on one laptop (16 GB) and is designed to move to EKS or GKE by adding a cluster and an environment. One owner builds it as a portfolio piece and has to be able to explain its code.

## How to apply these rules

Each principle below states a purpose. Apply a principle where its purpose applies. If following the wording would defeat the purpose, or a case is not covered, stop and say so: propose a solution and ask, rather than settling it by the letter or by a silent exception.

The CLAUDE.md of a directory describes how the principles are implemented there today. Those are mechanisms: a better mechanism for the same purpose can replace one, and that is a decision to propose, not to make along the way.

README.md explains the decisions to people, with their history. BACKLOG.md lists what is planned; docs/ holds plans and walkthroughs. Read them for context. They are not task lists: build an item from them when asked to.

## Priorities

When principles pull in different directions, the earlier one wins:

1. Data stays correct through retries, partial failures and late runs.
2. The owner can take the code over: plain code, a reason next to anything non-obvious, and the code that needs extra care (below) explained line by line. The owner often builds first and studies the result after, so this is about how the code reads, not about splitting work into small steps.
3. The smallest change that reaches the goal of the task. Ideas beyond it are proposed, not built.
4. The seams that keep a cloud move cheap stay intact (infra/CLAUDE.md). Cloud pieces are not built ahead of need.

## Principles

1. **Workflows carry identifiers; data moves through storage.** A Temporal workflow is the agreed process between services and keeps a decision in one visible history. Its payloads are ids, parameters and metrics; datasets, files and vectors go through ClickHouse and the object store.

2. **Model code computes; the platform does I/O around it.** Data scientists build and test a model library on mock data while the platform runs it, and neither waits for the other. A model library reads no database, bucket or environment; the platform layer does that for it.

3. **The work of a model over a dataset runs as a disposable Job; one event is processed in the worker as it comes.** Batch is a property of the work, not of a pipeline step: training, predicting over a split or re-indexing the whole corpus for a new model is a Job, while one uploaded file or one chat message is an activity against the long-running model server, because starting a pod would take longer than the work. Where a Job runs is decided by the cluster (node pools today, Kueue later), not by the service that starts it.

4. **A run changes only its own results, and readers see a run once it is complete.** Then retries and late runs cannot overwrite or half-show the output of another run. This concerns data written by pipeline runs; reference data, such as uploaded files, has its own natural key. Reference data comes from outside the platform (uploaded files, hand-written lists); what a model derives is the output of a run, even when its key is natural.

5. **A step can be retried safely.** Retries happen at the activity, the pod and the Job. A step finds what an earlier attempt did and finishes it instead of doing it twice. This rests on identifiers made where a request enters the platform (be_service makes `batch_id`, `run_id`, `file_id`) and carried unchanged through the workflow, the Job and the tables, so a retry reuses them. While its inputs and outputs stay fixed, the body of a step can be rewritten freely.

6. **Contracts change compatibly.** Workflow payloads, table schemas and the settings the infrastructure hands to applications outlive a release: running workflows, Jobs on the previous image and the previous release keep working while a change rolls out.

7. **Policy lives in workflows, decided from the metrics steps report.** Promotion is decided by the workflow, so the policy changes without a new image and each decision is in the workflow history.

8. **A fixed bug leaves a test behind.** The case that broke goes into the tests of its service, so CI catches the regression.

9. **Services stay independent.** A service has its own package, image and dependencies, and its own database where it needs one, so it can be built, released and handed to another team alone. A few lines repeated across services are cheaper than a shared library that couples their builds; where a repeated piece is critical, such as `commit_run`, each copy names the other in a comment.

## Code that needs extra care

Code that writes or deletes shared data or moves a pointer (ingest, writing predictions or chunks, the run registry, promotion): keep the change small, explain it line by line in your summary, and cover the retry and partial-failure cases with a test.

## Working in the repo

A change does what its task asks. Bugs and improvements noticed in other code, however small, are reported, not fixed in the same change; adding new code next to old code is fine.

| Project | Checks, as in CI (.github/workflows/ci.yml) |
|---|---|
| be_service, chat_service (Rust) | `cargo fmt --check`, `cargo clippy --all-targets --locked -- -D warnings`, `cargo test --locked` |
| batch_worker, llm_worker, mle_models (Python) | `uv run pytest` |
| infra | `tofu fmt -recursive -check infra`, `tofu validate`, `helm lint infra/charts/*` |

`make test` runs the five test suites.

`make` runs as the user, not root: OpenTofu state written by root cannot be updated later without it.

Ask before commands that destroy state or data: `make platform-down`, `make infra-down`, `tofu destroy`, deleting namespaces, PVCs or ClickHouse rows by hand.
