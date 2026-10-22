"""Entry point: serves the `llm` task queue until SIGTERM."""

import asyncio
import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from workflow import TASK_QUEUE, ChatWorkflow, IndexWorkflow
from workflow.activities.chat import Chatter
from workflow.activities.document import Reader
from workflow.activities.index import Indexer
from workflow.activities.mentions import Extractor
from workflow.activities.scope import ScopeSteps
from workflow.activities.topic import TopicSteps

# The activities are blocking and wait on the LLM server. index_file holds a
# file and its chunks (tens of MB), so a few at a time.
MAX_CONCURRENT = 4


async def serve(worker, stop: asyncio.Event) -> None:
    """Runs the worker until SIGTERM, or until the worker itself ends.

    A worker that fails must take the process down with it. Right after the
    platform is created the Temporal namespace may not exist yet: the worker
    then fails at once, and the process exits so that Kubernetes starts it
    again, instead of a dead worker sitting in a Running pod with nobody
    polling the queue. The same in batch_worker/src/worker/__main__.py.
    """
    running = asyncio.create_task(worker.run())
    stopping = asyncio.create_task(stop.wait())
    await asyncio.wait({running, stopping}, return_when=asyncio.FIRST_COMPLETED)
    if running.done():
        stopping.cancel()
        running.result()  # the worker's error, if it failed
        raise RuntimeError("the worker stopped by itself")
    # SIGTERM: stop polling, let running activities finish or be cut; a cut
    # one is retried on another worker.
    await worker.shutdown()
    await running


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")

    client = await Client.connect(address, namespace=namespace)
    indexer = Indexer.from_env()
    reader = Reader.from_env()
    extractor = Extractor.from_env()
    topics = TopicSteps.from_env()
    chatter = Chatter.from_env()
    scoper = ScopeSteps.from_env()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[IndexWorkflow, ChatWorkflow],
        activities=[indexer.index_file, reader.read_document, extractor.extract_mentions, chatter.retrieve, chatter.generate, chatter.record,
                    topics.route, topics.gather, topics.summarize, scoper.resolve_scope],
        activity_executor=ThreadPoolExecutor(MAX_CONCURRENT),
        max_concurrent_activities=MAX_CONCURRENT,
    )

    # Kubernetes stops a pod with SIGTERM: serve() then stops polling;
    # running activities are cut and retried on another worker, where they
    # rewrite their own partial rows.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    logging.info("polling task queue %s on %s, namespace %s, LLM server %s", TASK_QUEUE, address, namespace, indexer.llm.base_url)
    await serve(worker, stop)
    logging.info("worker stopped")


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    run()
