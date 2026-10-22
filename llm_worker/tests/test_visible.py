"""The steps of an answer read user data only through the *_visible views
(db/clickhouse/migrations 0030-0038), which take the user_id of the message
and join the owner links: a read that forgot the user cannot get past them.
This fails when the reading code names a table or a current view of user
data directly. The writer of chat_messages (record) and the run registry
are not readers of user data."""

import pathlib
import re

import workflow.activities.chat as chat
import workflow.activities.scope as scope
import workflow.activities.topic as topic

USER_DATA = {
    "files", "doc_chunks", "doc_chunks_current", "sat_document", "sat_document_current",
    "link_document_author", "link_document_author_current", "chunk_mentions", "chunk_mentions_current",
    "document_themes", "document_themes_current", "term_vectors", "term_vectors_current",
    "chat_messages_committed", "hub_document", "hub_author", "link_document_file",
    "document_edits", "collections", "collection_files",
}


def read_names(code: str) -> list[str]:
    """The word after every FROM and JOIN of the SQL in the code."""
    return re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", code)


def test_the_steps_of_an_answer_read_only_visible_views():
    for module in (chat, topic, scope):
        names = read_names(pathlib.Path(module.__file__).read_text())
        assert names, module.__name__
        assert not USER_DATA & set(names), f"{module.__name__} reads {USER_DATA & set(names)} directly"


def test_the_check_sees_a_raw_table():
    assert read_names('"SELECT a FROM files FINAL INNER JOIN doc_chunks_current AS c"') == ["files", "doc_chunks_current"]
