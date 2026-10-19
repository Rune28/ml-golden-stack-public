"""Inputs and outputs of the workflows and activities of the `llm` queue.

These dataclasses are the contract with whoever starts the workflows
(be_service): they travel as JSON, so field names are the API. New fields
only with defaults, so running workflows keep decoding.
"""

from dataclasses import dataclass, field


@dataclass
class IndexInput:
    # The uploaded file, made by be_service (POST /api/files).
    file_id: str
    run_id: str = ""


@dataclass
class IndexResult:
    run_id: str
    file_id: str
    name: str
    chunks: int
    chars: int
    embed_model: str


@dataclass
class ChatMessage:
    role: str  # user | assistant
    content: str


@dataclass
class ChatInput:
    # Made by be_service (POST /api/chats); the workflow ID is chat-<chat_id>.
    chat_id: str
    # Carried over by continue-as-new: the messages the model still sees and
    # the number of the last turn, so turns keep counting across runs.
    history: list[ChatMessage] = field(default_factory=list)
    turns: int = 0
    # Answers per workflow run before it continues as new.
    max_turns: int = 50


@dataclass
class MessageInput:
    text: str
    # The answer run, made by be_service (UUID v7). Also the Update ID, so a
    # call retried by the client is answered once.
    run_id: str


@dataclass
class Source:
    """A chunk an answer was given: its key in doc_chunks, and the file it is from."""

    file_id: str
    index_run_id: str
    chunk_id: int
    name: str = ""
    distance: float = 0.0


@dataclass
class Answer:
    run_id: str
    turn: int
    text: str
    sources: list[Source]
    model: str


@dataclass
class RetrieveInput:
    question: str
    k: int = 5


@dataclass
class GenerateInput:
    question: str
    sources: list[Source]
    history: list[ChatMessage]


@dataclass
class Generated:
    text: str
    model: str


@dataclass
class RecordInput:
    run_id: str
    chat_id: str
    turn: int
    question: str
    answer: str
    sources: list[Source]
