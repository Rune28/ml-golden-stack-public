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

# About the first two pages: the title, the authors, the abstract and the
# keywords are there, and they fit the context of a small model.
HEAD = 6000

KINDS = ("article", "book", "notes", "other")

_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_ISBN = re.compile(r"\bISBN(?:-1[03])?:?\s*((?:[0-9][-\s]?){9}[0-9Xx](?:[-\s]?[0-9]){0,3})")
_ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "authors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "orcid": {"type": "string"}},
                "required": ["name"],
            },
        },
        "year": {"type": ["integer", "null"]},
        "kind": {"type": "string", "enum": list(KINDS)},
        "abstract": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "authors", "year", "kind", "abstract", "keywords"],
}

METADATA_PROMPT = """You read the first pages of a document and return its metadata as JSON.
Use only what is written there; never make anything up. The file name and the length of the whole
text come first, as hints: the file name may carry the author or the title when the pages do not,
and a text of hundreds of thousands of characters is a book, not an article.
- title: the title as written.
- authors: the authors in the order listed, each with an ORCID only if one is printed next to the name.
  A person the document is about or quotes is not its author, and neither are editors, translators,
  illustrators or publishers. When the pages name no author, take the author from the file name.
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


def parse_metadata(data: dict) -> Metadata:
    """The model's answer, checked: wrong types become empty values, an unknown
    kind becomes other, a year out of range is dropped, repeated keywords and
    authors are kept once."""
    year = data.get("year")
    year = year if isinstance(year, int) and 1000 <= year <= 2100 else None
    kind = data.get("kind") if data.get("kind") in KINDS else "other"
    keywords = list(dict.fromkeys(k.strip() for k in _strings(data.get("keywords")) if k.strip()))

    authors: dict[str, Author] = {}
    for item in data.get("authors") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            continue
        orcid = item.get("orcid") if isinstance(item.get("orcid"), str) else ""
        author = Author(author_id(item["name"], orcid), item["name"].strip())
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
