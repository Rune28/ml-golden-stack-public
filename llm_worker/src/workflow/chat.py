"""Chat: one conversation, an entity workflow with the ID chat-<chat_id>.

A message is an Update: the caller (be_service) waits for its result, the
answer. A message about a fact is answered from the chunks closest to it
(retrieve, generate); a message about a topic from every chunk the topic is
a keyword of (route, gather, summarize: map-reduce). The first message starts the workflow in the same call
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
    Gathered,
    GatherInput,
    Generated,
    GenerateInput,
    MessageInput,
    RecordInput,
    RetrieveInput,
    Route,
    RouteInput,
    Source,
    SummarizeInput,
)

# Activities by name: the workflow sandbox never imports ClickHouse or the LLM adapter.
RETRIEVE, GENERATE, RECORD = "retrieve", "generate", "record"
ROUTE, GATHER, SUMMARIZE = "route", "gather", "summarize"
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
        self.max_topic_chunks = input.max_topic_chunks
        self.answered = 0  # in this run
        # Updates run concurrently as soon as they arrive; the lock queues them.
        self.lock = asyncio.Lock()

    @workflow.run
    async def run(self, input: ChatInput) -> None:
        # Wait for the answers in flight too: a handler cut by continue-as-new
        # would leave its caller without an answer.
        await workflow.wait_condition(lambda: self.answered >= self.max_turns and workflow.all_handlers_finished())
        workflow.continue_as_new(
            ChatInput(chat_id=self.chat_id, history=self.history, turns=self.turns, max_turns=self.max_turns,
                      max_topic_chunks=self.max_topic_chunks)
        )

    @workflow.update(name="send_message")
    async def send_message(self, message: MessageInput) -> Answer:
        async with self.lock:
            # Topics came after the first release: chats started before
            # replay without them (principle 6).
            topic = await self.topic(message) if workflow.patched("topics") else None
            if topic:
                generated, sources = topic
            else:
                generated, sources = await self.fact(message)
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

    async def fact(self, message: MessageInput) -> tuple[Generated, list[Source]]:
        """From the chunks closest to the question."""
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
        return generated, sources

    async def topic(self, message: MessageInput) -> tuple[Generated, list[Source]] | None:
        """From every chunk the topic is a keyword of; None when the message is
        about a fact or its words name nothing stored."""
        route = await workflow.execute_activity(
            ROUTE, RouteInput(question=message.text), result_type=Route,
            start_to_close_timeout=timedelta(seconds=60), retry_policy=RETRY,
        )
        if route.kind != "topic":
            return None
        gathered = await workflow.execute_activity(
            GATHER, GatherInput(question=message.text, terms=route.terms, max_chunks=self.max_topic_chunks),
            result_type=Gathered,
            start_to_close_timeout=timedelta(seconds=60), retry_policy=RETRY,
        )
        if gathered.mode == "too_broad":
            text = too_broad(message.text, [(b.term, b.name, b.chunks) for b in gathered.broad])
            return Generated(text=text, model=""), []
        if gathered.mode != "chunks":
            return None
        generated = await workflow.execute_activity(
            SUMMARIZE, SummarizeInput(question=message.text, sources=gathered.sources), result_type=Generated,
            # A call per group of chunks, one for the themes, one per theme: two or three minutes.
            start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )
        # The chunks of the topic say nothing about the question: as a fact.
        return (generated, gathered.sources) if generated.text else None

    @send_message.validator
    def validate_message(self, message: MessageInput) -> None:
        # A rejected Update leaves no trace in the workflow history.
        if not message.text.strip():
            raise ValueError("the message is empty")
        if len(message.text) > MAX_MESSAGE_CHARS:
            raise ValueError(f"the message is longer than {MAX_MESSAGE_CHARS} characters")
        if not message.run_id:
            raise ValueError("run_id is required")


def too_broad(question: str, broad: list[tuple[str, str, int]]) -> str:
    """The answer when every word of a topic is a theme of whole documents,
    (term, document name, chunks it is in): too many chunks to gather."""
    listed = "; ".join(f"«{term}» — {name}, ~{n}" for term, name, n in broad)
    if any("а" <= c <= "я" for c in question.lower()):
        return (f"Тема слишком широкая, чтобы собрать её целиком: {listed} фрагментов. "
                "Уточни, о чём именно, например: что там говорится о смерти, о времени, о стиле.")
    return (f"The topic is too broad to gather whole: {listed} passages. "
            "Narrow it down, for example: what is said about death, about time, about style.")
