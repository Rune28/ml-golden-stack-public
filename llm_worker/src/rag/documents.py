"""What a document is, read from the first pages of its text: the keys of the
document and of its authors, and the metadata a language model reads from
those pages. Pure: the activity read_document calls the model and writes the
rows.

The keys are Data Vault business keys. A document is `doi:` when its first
pages carry a DOI, `isbn:` for a book with an ISBN, otherwise `file:` with
the file it came in; so a preprint and the published version of a paper,
two files, are one document. Keys are found by regular expressions, not by
the model, and only in the first pages: the references of a paper list the
DOIs of other papers."""

import re
import unicodedata
from dataclasses import dataclass, field

from rag.mentions import names_a_person

# About the first two pages: the title, the authors, the abstract and the
# keywords are there, and they fit the context of a small model.
HEAD = 6000

KINDS = ("article", "book", "notes", "other")
# The role of a person named on the first pages. Asked to leave out whom the
# document quotes or is about, qwen3:4b still gave a book of lectures on
# Proust the author "М. Пруст" 5 times of 5: the title page names Proust's
# novel, the lecturer only the file name. Asked the role of every person, it
# called Proust the subject; who the author is follows in code (authors_of).
ROLES = ("author", "subject", "quoted", "editor", "translator", "illustrator", "publisher", "other")
# What the model writes for an author no page names.
_NO_AUTHOR = {"автор", "аноним", "неизвестный автор", "неизвестен", "author", "anonymous", "unknown"}
_INITIAL = re.compile(r"\b[A-ZА-ЯЁ]\.")

_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_ISBN = re.compile(r"\bISBN(?:-1[03])?:?\s*((?:[0-9][-\s]?){9}[0-9Xx](?:[-\s]?[0-9]){0,3})")
_ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string", "enum": list(ROLES)},
                    "orcid": {"type": "string"},
                },
                "required": ["name", "role"],
            },
        },
        "file_name_author": {"type": "string"},
        "year": {"type": ["integer", "null"]},
        "kind": {"type": "string", "enum": list(KINDS)},
        "abstract": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "people", "file_name_author", "year", "kind", "abstract", "keywords"],
}

METADATA_PROMPT = """You read the first pages of a document and return its metadata as JSON.
Use only what is written there; never make anything up. The file name and the length of the whole
text come first, as hints: a text of hundreds of thousands of characters is a book, not an article.
- title: the title as written.
- people: every person named on these pages, in the order listed, each with their role in this document:
  author (wrote it), subject (the person or the work it is about), quoted (an epigraph or a quotation),
  editor, translator, illustrator, publisher, or other; an ORCID only if one is printed next to the name.
- file_name_author: a person's name in the file name, as written there with spaces, or an empty string.
- year: the year of publication, null if it is not stated.
- kind: article for a scientific paper, book for a book, notes for personal or informal text, other otherwise.
- abstract: only a passage the document itself labels as its abstract or summary (Abstract, Summary,
  Аннотация, Резюме), exactly as written; otherwise an empty string. An epigraph or a first paragraph
  is not an abstract. Do not write a summary.
- keywords: the keywords the authors list, or an empty list if there are none. Do not choose any yourself."""


@dataclass(frozen=True)
class Author:
    author_id: str
    name: str


@dataclass(frozen=True)
class Metadata:
    title: str
    year: int | None
    kind: str
    abstract: str
    keywords: list[str] = field(default_factory=list)
    authors: list[Author] = field(default_factory=list)


def head(text: str) -> str:
    return text[:HEAD]


def metadata_request(name: str, text: str) -> str:
    """What the model reads: the hints, then the first pages."""
    return f"File name: {name}\nLength of the whole text: {len(text):,} characters\n\nFirst pages:\n{head(text)}"


def find_doi(text: str) -> str | None:
    """The first DOI of the first pages, lowercased (DOIs are case-insensitive)."""
    match = _DOI.search(head(text))
    return match.group(0).rstrip(".,;:)]}").lower() if match else None


def find_isbn(text: str) -> str | None:
    """The first ISBN of the first pages, digits only (and a final X)."""
    match = _ISBN.search(head(text))
    if not match:
        return None
    digits = re.sub(r"[^0-9X]", "", match.group(1).upper())
    return digits if len(digits) in (10, 13) else None


def document_id(doi: str | None, isbn: str | None, file_id: str) -> str:
    if doi:
        return f"doi:{doi}"
    if isbn:
        return f"isbn:{isbn}"
    return f"file:{file_id}"


def author_id(name: str, orcid: str = "") -> str:
    """orcid: when printed, otherwise the surname and initials: "Smith, John A."
    and "John A. Smith" are both `name:smith j a`."""
    orcid = orcid.strip().upper().removeprefix("HTTPS://ORCID.ORG/")
    if _ORCID.match(orcid):
        return f"orcid:{orcid}"
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)).lower()
    # "М.Пруст": an initial glued to the surname is a word of its own.
    plain = plain.replace(".", ". ")
    if "," in plain:
        surname, _, given = plain.partition(",")
    else:
        *given_parts, surname = plain.split() or [""]
        given = " ".join(given_parts)
    initials = [p[0] for p in re.split(r"[\s.\-]+", given) if p]
    return "name:" + " ".join([re.sub(r"[^\w-]", "", surname.strip()), *initials]).strip()


def looks_like_a_person(name: str, file_name: str = "") -> bool:
    """Whether a name the model gave can be a person: initials (Ю. П.
    Сенокосов), a capitalized Latin word (Merab Mamardashvili), or a word
    pymorphy reads as a name or a surname; not what the model writes for no
    author ("автор", "Аноним"), not a pronoun ("Меня"), not the file name."""
    name = name.strip()
    stem = file_name.rsplit(".", 1)[0].strip().lower()
    if not name or name.lower() in _NO_AUTHOR or name.lower() in (file_name.strip().lower(), stem):
        return False
    if _INITIAL.search(name):
        return True
    words = [w for w in re.split(r"[\s_\-]+", name) if w]
    return any(w[0].isupper() and (w.isascii() or names_a_person(w)) for w in words if w[0].isalpha())


def authors_of(data: dict, file_name: str) -> list[tuple[str, str]]:
    """(name, orcid) of the authors: the people with the role author, unless
    the file is named after a person none of them shares a surname with;
    then that person. With roles, the model still called Proust the author
    of the lectures on him 4 times of 5: his name heads the title page, the
    lecturer's is only in the file name. "Smith2021_squat.pdf" by Smith and
    Ivanova keeps both. Underscores of a file name are spaces."""
    out = []
    for item in data.get("people") or []:
        if not isinstance(item, dict) or item.get("role") != "author" or not isinstance(item.get("name"), str):
            continue
        name = item["name"].replace("_", " ").strip()
        if looks_like_a_person(name, file_name):
            out.append((name, item.get("orcid") if isinstance(item.get("orcid"), str) else ""))
    named = data.get("file_name_author")
    named = named.replace("_", " ").strip() if isinstance(named, str) else ""
    in_name = " ".join(file_name.replace("_", " ").split()).lower()
    if not (named and " ".join(named.split()).lower() in in_name and looks_like_a_person(named, file_name)):
        return out
    if any(_surname(name) == _surname(named) for name, _ in out):
        return out
    return [(named, "")]


def _surname(name: str) -> str:
    return author_id(name).removeprefix("name:").split(" ")[0]


def parse_metadata(data: dict, file_name: str = "") -> Metadata:
    """The model's answer, checked: wrong types become empty values, an unknown
    kind becomes other, a year out of range is dropped, repeated keywords and
    authors are kept once, and an author is a person (authors_of)."""
    year = data.get("year")
    year = year if isinstance(year, int) and 1000 <= year <= 2100 else None
    kind = data.get("kind") if data.get("kind") in KINDS else "other"
    keywords = list(dict.fromkeys(k.strip() for k in _strings(data.get("keywords")) if k.strip()))

    authors: dict[str, Author] = {}
    for name, orcid in authors_of(data, file_name):
        author = Author(author_id(name, orcid), name)
        authors.setdefault(author.author_id, author)

    return Metadata(
        title=_string(data.get("title")),
        year=year,
        kind=kind,
        abstract=_string(data.get("abstract")),
        keywords=keywords,
        authors=list(authors.values()),
    )


def _string(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
