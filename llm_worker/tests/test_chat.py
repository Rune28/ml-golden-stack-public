import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflow import ChatWorkflow
from workflow.chat import HISTORY
from workflow.contracts import (
    ChatInput,
    ChatMessage,
    Generated,
    GenerateInput,
    MessageInput,
    RecordInput,
    RetrieveInput,
    Source,
)

SOURCE = Source(file_id="file-1", index_run_id="idx-1", chunk_id=3, name="notes.md", distance=0.2)


class FakeSteps:
    """Stands in for retrieve, generate and record: records calls, answers "answer <n>"."""

    def __init__(self):
        self.calls = []

    @activity.defn(name="retrieve")
    async def retrieve(self, input: RetrieveInput) -> list[Source]:
        self.calls.append(("retrieve", input))
        return [SOURCE]

    @activity.defn(name="generate")
    async def generate(self, input: GenerateInput) -> Generated:
        self.calls.append(("generate", input))
        return Generated(text=f"answer to {input.question}", model="qwen3:4b")

    @activity.defn(name="record")
    async def record(self, input: RecordInput) -> None:
        self.calls.append(("record", input))

    def of(self, step):
        return [arg for name, arg in self.calls if name == step]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest.fixture
async def chat(env):
    """A running chat workflow and a way to send it messages."""
    steps, queue, chat_id = FakeSteps(), str(uuid.uuid4()), str(uuid.uuid4())

    async def start(max_turns=50):
        handle = await env.client.start_workflow(
            ChatWorkflow.run, ChatInput(chat_id=chat_id, max_turns=max_turns), id=f"chat-{chat_id}", task_queue=queue
        )

        async def send(text, run_id=None):
            message = MessageInput(text=text, run_id=run_id or str(uuid.uuid4()))
            # The workflow ID without a run: after continue-as-new it reaches the new run.
            latest = env.client.get_workflow_handle(handle.id)
            return await latest.execute_update(ChatWorkflow.send_message, message)

        return handle, send

    async with Worker(env.client, task_queue=queue, workflows=[ChatWorkflow],
                      activities=[steps.retrieve, steps.generate, steps.record]):
        yield steps, start


async def test_a_message_is_retrieved_generated_and_recorded_in_order(chat):
    steps, start = chat
    _, send = await start()

    answer = await send("what is S3?", run_id="ans-1")

    assert [name for name, _ in steps.calls] == ["retrieve", "generate", "record"]
    assert steps.of("generate")[0].sources == [SOURCE]
    [record] = steps.of("record")
    assert (record.run_id, record.turn, record.question, record.answer) == ("ans-1", 1, "what is S3?", "answer to what is S3?")
    assert record.sources == [SOURCE]
    assert (answer.run_id, answer.turn, answer.text, answer.sources) == ("ans-1", 1, "answer to what is S3?", [SOURCE])


async def test_the_second_answer_sees_the_first(chat):
    steps, start = chat
    _, send = await start()

    await send("first")
    second = await send("second")

    first_call, second_call = steps.of("generate")
    assert first_call.history == []
    assert second_call.history == [ChatMessage("user", "first"), ChatMessage("assistant", "answer to first")]
    assert second.turn == 2


async def test_the_model_sees_only_the_last_messages(chat):
    steps, start = chat
    _, send = await start()

    for i in range(HISTORY):
        await send(f"q{i}")

    last = steps.of("generate")[-1].history
    assert len(last) == HISTORY
    assert last[-1] == ChatMessage("assistant", f"answer to q{HISTORY - 2}")


async def test_continue_as_new_carries_the_window_and_the_turn_count(chat, env):
    steps, start = chat
    handle, send = await start(max_turns=2)

    await send("first")
    await send("second")
    third = await send("third")

    description = await env.client.get_workflow_handle(handle.id).describe()
    assert description.run_id != handle.result_run_id
    assert third.turn == 3
    assert steps.of("generate")[-1].history[-2:] == [ChatMessage("user", "second"), ChatMessage("assistant", "answer to second")]


async def test_an_empty_message_is_rejected_before_any_step(chat):
    steps, start = chat
    _, send = await start()

    with pytest.raises(WorkflowUpdateFailedError):
        await send("   ")

    assert steps.calls == []
