"""Entry point: serves the `batch` task queue until SIGTERM."""

import asyncio
import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from workflow import TASK_QUEUE, ReportWorkflow, RunWorkflow, TrainWorkflow
from workflow.activities.run_job import JobRunner

# run_job mostly sleeps between status polls; one thread per running Job.
MAX_CONCURRENT_JOBS = 8


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")

    client = await Client.connect(address, namespace=namespace)
    runner = JobRunner.from_env()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[TrainWorkflow, RunWorkflow, ReportWorkflow],
        activities=[runner.run_job],
        activity_executor=ThreadPoolExecutor(MAX_CONCURRENT_JOBS),
        max_concurrent_activities=MAX_CONCURRENT_JOBS,
    )

    # Kubernetes stops a pod with SIGTERM. Leaving `async with` stops polling;
    # running activities are cut and retried on another worker, where they
    # pick up the same Job.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with worker:
        logging.info("polling task queue %s on %s, namespace %s, jobs in %s", TASK_QUEUE, address, namespace, runner.config.namespace)
        await stop.wait()
    logging.info("worker stopped")


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    run()
