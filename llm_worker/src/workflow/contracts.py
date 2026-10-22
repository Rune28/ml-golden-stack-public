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
class Scope:
    """Where an answer may look, among the user's files (docs/search-scope-plan.md):
    the files of these groups, by these authors, of these kinds, of these
    years, or these files. Facets narrow each other (and); the values of one
    facet widen it (or). Empty: every file the user may see."""

    file_ids: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)  # collection ids
    authors: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None

    def empty(self) -> bool:
        return not (self.file_ids or self.collections or self.authors or self.kinds
                    or self.year_from is not None or self.year_to is not None)


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
    # The profile of the corpus (workflow.profiles): fixed by the first
    # message that names one, or by the route of the first message, which
    # resolves the default; carried over by continue-as-new.
    profile: str = ""
    # Where the chat looks: fixed by the first message that names a scope,
    # carried over by continue-as-new.
    scope: Scope = field(default_factory=Scope)


@dataclass
class MessageInput:
    text: str
    # The answer run, made by be_service (UUID v7). Also the Update ID, so a
    # call retried by the client is answered once.
    run_id: str
    # Who asks: from the platform token, checked by be_service, which also
    # checked that the chat is theirs. Every step of the answer reads only
    # this user's files; empty, a step reads nothing (fail closed).
    user_id: str = ""
    # The profile asked for (be_service checked its form); it counts only
    # while the chat has none.
    profile: str = ""
    # The scope asked for (be_service checked it); it counts only while the
    # chat has none.
    scope: Scope = field(default_factory=Scope)


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
    # The profile of the chat, for the page to show what the answer was made with.
    profile: str = ""
    # What the answer was looked for in ("4 files: group Блоги"); empty: everywhere.
    scope_label: str = ""


@dataclass
class RetrieveInput:
    question: str
    k: int = 5
    user_id: str = ""
    # Only these of the user's files (the chat's scope, resolved); empty: all of them.
    file_ids: list[str] = field(default_factory=list)


@dataclass
class GenerateInput:
    question: str
    sources: list[Source]
    history: list[ChatMessage]
    user_id: str = ""


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
    # The profile of the chat (workflow.profiles); empty: the worker's default.
    profile: str = ""
    # Whose groups, authors and kinds the message may narrow its search to.
    user_id: str = ""


@dataclass
class Side:
    """One of the things a message compares: an author, a group or a kind of
    the user's documents it names, and the scope that is its documents."""

    label: str
    scope: Scope = field(default_factory=Scope)


@dataclass
class Route:
    # fact | topic | interpret (the user's own story: answered as a fact) |
    # compare (set in code: the message names two or more sides).
    kind: str = "fact"
    # The words of the query that name the topic.
    terms: list[str] = field(default_factory=list)
    # What the message asks of the documents, short, without the story
    # around it: what a topic is gathered and themed by. Empty from a route
    # before it existed: the message is used then.
    query: str = ""
    # The profile the route was read with, by name and version (a hash of
    # its file): what this answer was made with. Empty before profiles.
    profile: str = ""
    profile_version: str = ""
    # Where the message itself narrows the search, among the user's own
    # groups, authors and kinds ("in my blogs"); empty: it does not.
    filters: Scope = field(default_factory=Scope)
    # The subjects the message lists to be answered one by one (жизнь,
    # смерть, любовь): a section of the answer each. Fewer than two: none.
    aspects: list[str] = field(default_factory=list)
    # What a compare message compares (two or more), from filters.
    sides: list[Side] = field(default_factory=list)


@dataclass
class GatherInput:
    question: str
    terms: list[str]
    max_chunks: int = 60
    # Fewer chunks found: answered as a fact (rag.topics.MIN_CHUNKS).
    min_chunks: int = 6
    user_id: str = ""
    # The profile of the chat (workflow.profiles); empty: the worker's default.
    profile: str = ""
    # Only these of the user's files (the chat's scope, resolved); empty: all of them.
    file_ids: list[str] = field(default_factory=list)
    # Take max_chunks whatever the keywords give: their chunks first, the
    # closest to the question for the rest. For a part of a sectioned answer,
    # which has to say something on its subject even where the documents
    # never use its word.
    fill: bool = False


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
class Section:
    """The passages gathered for one aspect of a question, in the documents
    of one side; either may be empty."""

    aspect: str = ""
    side: str = ""
    passages: list[int] = field(default_factory=list)


@dataclass
class SummarizeInput:
    # The message as written: the notes are taken on what it asks.
    question: str
    sources: list[Source]
    # The short query of the route: themes are named after it, not after the
    # story of a long message. Empty: the question.
    query: str = ""
    user_id: str = ""
    # The profile of the chat (workflow.profiles); empty: the worker's default.
    profile: str = ""
    # An answer in sections (an aspect, a side, or both): which passages
    # (numbers of sources, from 1) each was gathered for. Empty: one topic.
    sections: list[Section] = field(default_factory=list)


@dataclass
class ScopeInput:
    user_id: str
    # The chat's scope, then what the message narrows it to: both hold.
    scope: Scope = field(default_factory=Scope)
    narrow: Scope = field(default_factory=Scope)


@dataclass
class Scoped:
    """The user's files a scope comes to, and how to say what it was."""

    file_ids: list[str] = field(default_factory=list)
    label: str = ""
