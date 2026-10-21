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
    # The run that reads what the document is (read_document), made by be_service too.
    document_run_id: str = ""
    # The run that finds the keywords of the chunks (extract_mentions), made by be_service too.
    mentions_run_id: str = ""


@dataclass
class IndexResult:
    run_id: str
    file_id: str
    name: str
    chunks: int
    chars: int
    embed_model: str
    # Business key of the document the file is (doi:, isbn:, file:); empty
    # for a workflow started before read_document was a step of Index.
    document_id: str = ""
    # Keywords found in the chunks, and the top themes of the document.
    mentions: int = 0
    themes: list[str] = field(default_factory=list)


@dataclass
class DocumentInput:
    file_id: str
    run_id: str


@dataclass
class MentionsInput:
    file_id: str
    # The index run whose chunks the keywords are found in.
    index_run_id: str
    run_id: str


@dataclass
class MentionsResult:
    run_id: str
    file_id: str
    mentions: int
    themes: int
    top_themes: list[str] = field(default_factory=list)


@dataclass
class DocumentResult:
    run_id: str
    file_id: str
    document_id: str
    title: str
    authors: int


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
    # Chunks an answer about a topic reads at most (rag.topics.MAX_CHUNKS).
    max_topic_chunks: int = 60
    # Fewer chunks found for a topic: answered as a fact (rag.topics.MIN_CHUNKS).
    min_topic_chunks: int = 6


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


@dataclass
class RouteInput:
    question: str


@dataclass
class Route:
    # fact | topic | interpret (the user's own story: answered as a fact).
    kind: str = "fact"
    # The words of the query that name the topic.
    terms: list[str] = field(default_factory=list)
    # What the message asks of the documents, short, without the story
    # around it: what a topic is gathered and themed by. Empty from a route
    # before it existed: the message is used then.
    query: str = ""


@dataclass
class GatherInput:
    question: str
    terms: list[str]
    max_chunks: int = 60
    # Fewer chunks found: answered as a fact (rag.topics.MIN_CHUNKS).
    min_chunks: int = 6


@dataclass
class Broad:
    """A term that is a theme of a whole document: in this many of its chunks."""

    term: str
    file_id: str
    name: str
    chunks: int


@dataclass
class Gathered:
    mode: str  # chunks | too_broad | none
    # The terms as stored (lemmas).
    terms: list[str] = field(default_factory=list)
    # Chunks the terms are keywords of; sources are the ones taken.
    found: int = 0
    broad: list[Broad] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    # The stored terms close to each term that were taken with it (сон →
    # [сновидение]), so the choice is in the workflow history.
    similar: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class SummarizeInput:
    # The message as written: the notes are taken on what it asks.
    question: str
    sources: list[Source]
    # The short query of the route: themes are named after it, not after the
    # story of a long message. Empty: the question.
    query: str = ""
