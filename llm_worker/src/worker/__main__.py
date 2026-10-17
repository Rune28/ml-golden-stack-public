"""Entry point: serves the `llm` task queue until SIGTERM."""

import asyncio
import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from workflow import TASK_QUEUE, IndexWorkflow
from workflow.activities.index import Indexer

# The activities are blocking and wait on the LLM server. Each holds a file
# and its chunks (tens of MB), so a few at a time.
MAX_CONCURRENT = 4


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")

    client = await Client.connect(address, namespace=namespace)
    indexer = Indexer.from_env()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[IndexWorkflow],
        activities=[indexer.index_file],
        activity_executor=ThreadPoolExecutor(MAX_CONCURRENT),
        max_concurrent_activities=MAX_CONCURRENT,
    )

    # Kubernetes stops a pod with SIGTERM. Leaving `async with` stops polling;
    # running activities are cut and retried on another worker, where they
    # rewrite their own partial rows.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with worker:
        logging.info("polling task queue %s on %s, namespace %s, LLM server %s", TASK_QUEUE, address, namespace, indexer.llm.base_url)
        await stop.wait()
    logging.info("worker stopped")


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    run()
