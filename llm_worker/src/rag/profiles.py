"""What the steps of an answer need to know about a corpus: what counts as
which kind of message, which terms are synonyms, whose words a passage
gives. A book of lectures and papers on training differ in all three; the
steps themselves (route, gather, summarize) do not.

The texts of a profile go into the prompts exactly as written in its file,
line breaks and indents included: for a small model the layout is part of
the prompt. The same words laid out one line per kind sent "что такое
пирожное мадлен у Пруста?" to a fact 4 times of 10 instead of 9 (qwen3:4b,
2026-10-22).

Pure: a profile is made from the parsed data of its file. Reading the file
and choosing the profile of a chat is the platform's (workflow.profiles).
"""

from dataclasses import dataclass

from rag.documents import KINDS as DOCUMENT_KINDS
from rag.mentions import candidates, word_lemmas
from rag.topics import KINDS


@dataclass(frozen=True)
class Profile:
    name: str
    # Of the file's content: what an answer was made with, in its history.
    version: str
    # route: what counts as each kind of rag.topics.KINDS, with examples, and
    # how to pick the terms.
    kinds: dict[str, str]
    terms: str
    # Lemmas of the words that name the documents themselves (книга, блог,
    # автор): a term made of them only is dropped, since it says where to
    # look, not what about. Told so in the prompt, qwen3:4b still gave such
    # terms in 26 messages of 75 (2026-10-22); dropped in code, none.
    document_words: frozenset[str]
    # Words a message names a kind of document by ("книга" → book), as
    # lemmas: a kind the message names narrows where to look (rag.topics.where_in).
    kind_words: dict[str, str]
    # gather: candidates for synonyms, and examples of what is one and what is not.
    similar_max_distance: float
    similar_candidates: int
    similar_same: str
    similar_related: str
    # summarize: whose words a point of a passage is.
    attribution: str


def profile_from(name: str, version: str, data: dict) -> Profile:
    """A profile from the parsed data of its file; ValueError naming what is
    missing or wrong, so a broken profile fails where it is loaded."""

    def text(section: dict, key: str, where: str) -> str:
        value = section.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"profile {name}: {where}.{key} must be a non-empty string")
        return value.strip()  # the ends only: the layout inside is the author's

    def section(parent: dict, key: str, where: str) -> dict:
        value = parent.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"profile {name}: [{where}{key}] is missing")
        return value

    route = section(data, "route", "")
    kinds = {kind: text(section(route, kind, "route."), "text", f"route.{kind}") for kind in KINDS}
    named = route.get("kind_words", {})
    if not isinstance(named, dict) or not all(isinstance(w, str) and v in DOCUMENT_KINDS for w, v in named.items()):
        raise ValueError(f"profile {name}: route.kind_words must map words to kinds of {', '.join(DOCUMENT_KINDS)}")
    kind_words = {lemma: kind for word, kind in named.items() for lemma in word_lemmas(word)}
    words = route.get("document_words", [])
    if not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words):
        raise ValueError(f"profile {name}: route.document_words must be a list of words")

    similar = section(data, "similar", "")
    distance, how_many = similar.get("max_distance"), similar.get("candidates")
    if not isinstance(distance, (int, float)) or isinstance(distance, bool) or not 0 <= distance <= 1:
        raise ValueError(f"profile {name}: similar.max_distance must be a number from 0 to 1")
    if not isinstance(how_many, int) or isinstance(how_many, bool) or how_many < 0:
        raise ValueError(f"profile {name}: similar.candidates must be a whole number, 0 or more")

    return Profile(
        name=name,
        version=version,
        kinds=kinds,
        terms=text(route, "terms", "route"),
        # As stored: "книгах" in a question and "книга" here are one word.
        kind_words=kind_words,
        document_words=frozenset(lemma for w in words for lemma in candidates(w.strip()) or [w.strip().lower()]),
        similar_max_distance=float(distance),
        similar_candidates=how_many,
        similar_same=text(similar, "same", "similar"),
        similar_related=text(similar, "related", "similar"),
        attribution=text(section(data, "map", ""), "attribution", "map"),
    )

