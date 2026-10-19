"""Workflows of the `llm` task queue: events processed in the worker as they
come, against the model server (README, rule 12). The work of models over
datasets goes to the `batch` queue of batch_worker."""

from .chat import ChatWorkflow
from .index import IndexWorkflow

TASK_QUEUE = "llm"

__all__ = ["TASK_QUEUE", "ChatWorkflow", "IndexWorkflow"]
