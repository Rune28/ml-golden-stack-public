import asyncio

import pytest

from worker.__main__ import serve


class FakeWorker:
    """run() ends when shutdown() is called, or fails at once with `error`."""

    def __init__(self, error=None):
        self.error, self.shut_down, self._done = error, False, asyncio.Event()

    async def run(self):
        if self.error:
            raise self.error
        await self._done.wait()

    async def shutdown(self):
        self.shut_down = True
        self._done.set()


async def test_a_failed_worker_ends_the_process_with_its_error():
    # Right after the platform is created: the Temporal namespace is not there yet.
    worker = FakeWorker(RuntimeError("Namespace default was not found"))

    with pytest.raises(RuntimeError, match="Namespace default was not found"):
        await asyncio.wait_for(serve(worker, asyncio.Event()), timeout=2)


async def test_sigterm_shuts_the_worker_down_and_returns():
    worker, stop = FakeWorker(), asyncio.Event()
    stop.set()

    await asyncio.wait_for(serve(worker, stop), timeout=2)

    assert worker.shut_down
