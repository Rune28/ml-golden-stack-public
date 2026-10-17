"""Train: a new model version on all data, promoted if it beats the champion.

The promotion policy lives here, not in the training code: it can change
without rebuilding the image, and every decision is in the workflow history.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import JobRequest, TrainInput, TrainResult

# Activity by name: the workflow sandbox never imports the Kubernetes client.
RUN_JOB = "run_job"
RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))


def job_options(deadline_seconds: int) -> dict:
    return {
        # A bit over the Job's own deadline, so Kubernetes stops it first.
        "start_to_close_timeout": timedelta(seconds=deadline_seconds + 120),
        # run_job heartbeats while it waits: a dead worker is noticed in 30 s
        # and the retry picks up the same Job on another worker.
        "heartbeat_timeout": timedelta(seconds=30),
        "retry_policy": RETRY,
    }


@workflow.defn(name="Train")
class TrainWorkflow:
    @workflow.run
    async def run(self, input: TrainInput) -> TrainResult:
        run_id = input.run_id or str(workflow.uuid4())

        train = JobRequest(
            step="train",
            run_id=run_id,
            args=["--run-id", run_id, "--epochs", str(input.epochs), "--pool", input.pool],
            pool=input.pool,
            cpu="2",
            memory="1536Mi",
            deadline_seconds=1800,
        )
        trained = await workflow.execute_activity(
            RUN_JOB, train, result_type=dict, **job_options(train.deadline_seconds)
        )

        champion = trained.get("champion")
        promoted = champion is None or trained["accuracy"] > champion["accuracy"] + input.min_improvement
        workflow.logger.info(
            "trained version %s, accuracy %.4f, champion %s, promote: %s",
            trained["version"], trained["accuracy"], champion, promoted,
        )

        if promoted:
            promote = JobRequest(
                step="promote",
                run_id=run_id,
                args=["--version", trained["version"]],
                memory="256Mi",
                deadline_seconds=120,
            )
            await workflow.execute_activity(RUN_JOB, promote, result_type=dict, **job_options(promote.deadline_seconds))

        return TrainResult(
            run_id=run_id,
            version=trained["version"],
            accuracy=trained["accuracy"],
            train_samples=trained["train_samples"],
            promoted=promoted,
            previous_champion=champion,
        )
