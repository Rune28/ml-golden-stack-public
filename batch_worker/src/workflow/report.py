"""Report: what a prediction run adds up to. The recognized digits are
counted next to the true ones, so a model that leans towards some digit
shows up as a bar higher than its truth."""

from temporalio import workflow

from .contracts import JobRequest, ReportInput, ReportResult
from .train import RUN_JOB, job_options


@workflow.defn(name="Report")
class ReportWorkflow:
    @workflow.run
    async def run(self, input: ReportInput) -> ReportResult:
        run_id = input.run_id or str(workflow.uuid4())

        report = JobRequest(
            step="report",
            run_id=run_id,
            args=["--run-id", run_id, "--prediction-run-id", input.prediction_run_id],
            memory="512Mi",
            deadline_seconds=300,
        )
        result = await workflow.execute_activity(RUN_JOB, report, result_type=dict, **job_options(report.deadline_seconds))
        return ReportResult(**result)
