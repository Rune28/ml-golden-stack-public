"""Workflows of the `batch` task queue: the work of models over datasets, each
step a Kubernetes Job (README, rule 3). Events go to the `llm` queue of
llm_worker."""

from .report import ReportWorkflow
from .run import RunWorkflow
from .train import TrainWorkflow

TASK_QUEUE = "batch"

__all__ = ["TASK_QUEUE", "ReportWorkflow", "RunWorkflow", "TrainWorkflow"]
