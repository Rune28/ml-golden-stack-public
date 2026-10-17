import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflow import ReportWorkflow, RunWorkflow, TrainWorkflow
from workflow.contracts import JobRequest, ReportInput, RunInput, TrainInput


class FakeJobs:
    """Stands in for run_job: records requests, answers like the mle-models steps."""

    def __init__(self, accuracy: float, champion: dict | None):
        self.accuracy, self.champion, self.requests = accuracy, champion, []

    @activity.defn(name="run_job")
    async def run_job(self, req: JobRequest) -> dict:
        self.requests.append(req)
        if req.step == "train":
            return {"version": "7", "accuracy": self.accuracy, "train_samples": 5000, "champion": self.champion}
        if req.step == "promote":
            return {"champion": req.args[-1]}
        if req.step == "report":
            digits = [{"digit": d, "recognized": 1000, "actual": 1000} for d in range(10)]
            return {"run_id": req.run_id, "prediction_run_id": req.args[-1], "model_version": "7", "total": 10000,
                    "digits": digits, "most_over": {"digit": 8, "bias": 20}, "most_under": {"digit": 3, "bias": -15}}
        return {"model_version": "7", "accuracy": 0.95, "rows": 10000}


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def execute(env, jobs: FakeJobs, workflow, arg):
    queue = str(uuid.uuid4())
    async with Worker(env.client, task_queue=queue, workflows=[TrainWorkflow, RunWorkflow, ReportWorkflow], activities=[jobs.run_job]):
        return await env.client.execute_workflow(workflow, arg, id=str(uuid.uuid4()), task_queue=queue)


@pytest.mark.parametrize(
    "accuracy, champion, promoted",
    [
        (0.90, None, True),
        (0.95, {"version": "6", "accuracy": 0.93}, True),
        (0.92, {"version": "6", "accuracy": 0.93}, False),
    ],
    ids=["first model", "better than champion", "worse than champion"],
)
async def test_train_promotes_only_a_better_model(env, accuracy, champion, promoted):
    jobs = FakeJobs(accuracy, champion)

    result = await execute(env, jobs, TrainWorkflow.run, TrainInput(run_id="r1"))

    assert result.promoted is promoted
    assert result.previous_champion == champion
    assert [r.step for r in jobs.requests] == (["train", "promote"] if promoted else ["train"])
    if promoted:
        assert jobs.requests[1].args == ["--version", "7"]


async def test_train_runs_on_the_requested_pool_with_its_run_id(env):
    jobs = FakeJobs(0.9, None)

    await execute(env, jobs, TrainWorkflow.run, TrainInput(run_id="r1", epochs=5, pool="gpu"))

    train = jobs.requests[0]
    assert (train.pool, train.run_id) == ("gpu", "r1")
    assert train.args == ["--run-id", "r1", "--epochs", "5", "--pool", "gpu"]


async def test_run_without_run_id_gets_one(env):
    jobs = FakeJobs(0.9, None)

    result = await execute(env, jobs, RunWorkflow.run, RunInput())

    assert uuid.UUID(result.run_id)
    assert (result.model_version, result.rows) == ("7", 10000)
    assert jobs.requests[0].args == ["--run-id", result.run_id, "--model", "champion"]


async def test_report_runs_one_cpu_job_on_the_given_prediction_run(env):
    jobs = FakeJobs(0.9, None)

    result = await execute(env, jobs, ReportWorkflow.run, ReportInput(prediction_run_id="pred-1", run_id="rep-1"))

    [req] = jobs.requests
    assert (req.step, req.pool, req.run_id) == ("report", "cpu", "rep-1")
    assert req.args == ["--run-id", "rep-1", "--prediction-run-id", "pred-1"]
    assert (result.prediction_run_id, result.most_over["digit"], len(result.digits)) == ("pred-1", 8, 10)

