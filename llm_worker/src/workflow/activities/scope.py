"""resolve_scope: the user's files a chat's scope comes to
(docs/search-scope-plan.md, M3).

Read through documents_visible and collections_visible, so a scope can only
narrow what the user may see: a group, an author or a file of someone else
matches nothing. Resolved for every message, so a file uploaded or
corrected since counts at once. The scope of the chat and what the message
narrows it to both hold; within one facet, any of its values will do.
"""

from dataclasses import dataclass

from temporalio import activity

from workflow.activities.index import connect
from workflow.contracts import Scope, Scoped, ScopeInput


@dataclass(frozen=True)
class Document:
    """One of the user's files as documents_visible gives it."""

    file_id: str
    authors: tuple[str, ...]
    kind: str
    year: int | None
    collection_ids: tuple[str, ...]


def _same(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").split())


def matches(document: Document, scope: Scope) -> bool:
    """Whether a document is in a scope: in every facet the scope names, one
    of its values. A year facet leaves out a document of no known year."""
    if scope.file_ids and document.file_id not in scope.file_ids:
        return False
    if scope.collections and not set(scope.collections) & set(document.collection_ids):
        return False
    if scope.authors and not {_same(a) for a in scope.authors} & {_same(a) for a in document.authors}:
        return False
    if scope.kinds and document.kind not in scope.kinds:
        return False
    if scope.year_from is not None or scope.year_to is not None:
        if document.year is None:
            return False
        if scope.year_from is not None and document.year < scope.year_from:
            return False
        if scope.year_to is not None and document.year > scope.year_to:
            return False
    return True


def label(scopes: list[Scope], names: dict[str, str], files: int) -> str:
    """What the answer was looked for in, for the page: "4 files · group
    Блоги · author Merab Mamardashvili"."""
    parts = [f"{files} file" + ("" if files == 1 else "s")]
    for scope in scopes:
        if scope.collections:
            parts.append("group " + ", ".join(names.get(c, "?") for c in scope.collections))
        if scope.authors:
            parts.append("author " + ", ".join(scope.authors))
        if scope.kinds:
            parts.append("kind " + ", ".join(scope.kinds))
        if scope.year_from is not None or scope.year_to is not None:
            parts.append(f"{scope.year_from or ''}–{scope.year_to or ''}")
        if scope.file_ids:
            parts.append(f"{len(scope.file_ids)} chosen files")
    return " · ".join(parts)


class ScopeSteps:
    def __init__(self, connect):
        # A ClickHouse client per call: activities run on several threads at once.
        self.connect = connect

    @classmethod
    def from_env(cls) -> "ScopeSteps":
        return cls(connect)

    @activity.defn(name="resolve_scope")
    def resolve_scope(self, input: ScopeInput) -> Scoped:
        # With no user, nothing (fail closed), as every step that reads.
        if not input.user_id:
            return Scoped(label=label([input.scope, input.narrow], {}, 0))
        client = self.connect()
        user = {"u": input.user_id}
        documents = [
            Document(file_id=f, authors=tuple(a), kind=k, year=y, collection_ids=tuple(c))
            for f, a, k, y, c in client.query(
                "SELECT file_id, authors, kind, year, collection_ids FROM documents_visible(user_id = {u:String})",
                parameters=user,
            ).result_rows
        ]
        names = dict(client.query(
            "SELECT collection_id, name FROM collections_visible(user_id = {u:String})", parameters=user,
        ).result_rows)
        file_ids = sorted(d.file_id for d in documents if matches(d, input.scope) and matches(d, input.narrow))
        return Scoped(file_ids=file_ids, label=label([input.scope, input.narrow], names, len(file_ids)))
