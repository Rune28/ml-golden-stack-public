"""Chat: one conversation, an entity workflow with the ID chat-<chat_id>.

A message is an Update: the caller (be_service) waits for its result, the
answer. The first message starts the workflow in the same call
(UpdateWithStart). Between messages the workflow only waits, and a waiting
workflow costs the worker nothing.

Messages are answered one at a time, in the order they came, so an answer
sees every answer before it. The model sees the last HISTORY messages; all
of them are in chat_messages. After max_turns answers the workflow continues
as new, carrying that window over, so its history in Temporal stays bounded.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import (
    Answer,
    ChatInput,
    ChatMessage,
    Generated,
    GenerateInput,
    MessageInput,
    RecordInput,
    RetrieveInput,
    Source,
)

# Activities by name: the workflow sandbox never imports ClickHouse or the LLM adapter.
RETRIEVE, GENERATE, RECORD = "retrieve", "generate", "record"
RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2))

# Messages (user and assistant) the model sees besides the question: 3 turns.
HISTORY = 6
MAX_MESSAGE_CHARS = 4000


@workflow.defn(name="Chat")
class ChatWorkflow:
    @workflow.init
    def __init__(self, input: ChatInput) -> None:
        self.chat_id = input.chat_id
        self.history = list(input.history)
        self.turns = input.turns
        self.max_turns = input.max_turns
        self.answered = 0  # in this run
        # Updates run concurrently as soon as they arrive; the lock queues them.
        self.lock = asyncio.Lock()

    @workflow.run
    async def run(self, input: ChatInput) -> None:
        # Wait for the answers in flight too: a handler cut by continue-as-new
        # would leave its caller without an answer.
        await workflow.wait_condition(lambda: self.answered >= self.max_turns and workflow.all_handlers_finished())
        workflow.continue_as_new(
            ChatInput(chat_id=self.chat_id, history=self.history, turns=self.turns, max_turns=self.max_turns)
        )

    @workflow.update(name="send_message")
    async def send_message(self, message: MessageInput) -> Answer:
        async with self.lock:
            sources = await workflow.execute_activity(
                RETRIEVE,
                RetrieveInput(question=message.text),
                result_type=list[Source],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RETRY,
            )
            generated = await workflow.execute_activity(
                GENERATE,
                GenerateInput(question=message.text, sources=sources, history=self.history),
                result_type=Generated,
                # One answer of a small model on the local GPU, thinking included.
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RETRY,
            )
            turn = self.turns + 1
            await workflow.execute_activity(
                RECORD,
                RecordInput(run_id=message.run_id, chat_id=self.chat_id, turn=turn, question=message.text,
                            answer=generated.text, sources=sources),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RETRY,
            )
            # Only after the answer is recorded: a failed step leaves the
            # conversation as it was, and the next message gets the same turn.
            self.history = [*self.history, ChatMessage("user", message.text),
                            ChatMessage("assistant", generated.text)][-HISTORY:]
            self.turns = turn
            self.answered += 1
            return Answer(run_id=message.run_id, turn=turn, text=generated.text, sources=sources,
                          model=generated.model)

    @send_message.validator
    def validate_message(self, message: MessageInput) -> None:
        # A rejected Update leaves no trace in the workflow history.
        if not message.text.strip():
            raise ValueError("the message is empty")
        if len(message.text) > MAX_MESSAGE_CHARS:
            raise ValueError(f"the message is longer than {MAX_MESSAGE_CHARS} characters")
        if not message.run_id:
            raise ValueError("run_id is required")
