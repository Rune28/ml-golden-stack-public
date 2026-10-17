"""Run: predict the test split with a model version (the champion by default)."""

from temporalio import workflow

from .contracts import JobRequest, RunInput, RunResult
from .train import RUN_JOB, job_options


@workflow.defn(name="Run")
class RunWorkflow:
    @workflow.run
    async def run(self, input: RunInput) -> RunResult:
        run_id = input.run_id or str(workflow.uuid4())

        predict = JobRequest(
            step="predict",
            run_id=run_id,
            args=["--run-id", run_id, "--model", input.model],
            pool=input.pool,
            memory="768Mi",
            deadline_seconds=600,
        )
        result = await workflow.execute_activity(
            RUN_JOB, predict, result_type=dict, **job_options(predict.deadline_seconds)
        )
        return RunResult(
            run_id=run_id,
            model_version=result["model_version"],
            accuracy=result["accuracy"],
            rows=result["rows"],
        )
