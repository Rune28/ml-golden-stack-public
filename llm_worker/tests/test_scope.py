from temporalio.testing import ActivityEnvironment

from workflow.activities.scope import Document, ScopeSteps, label, matches
from workflow.contracts import Scope, ScopeInput

BOOK = Document("f-book", ("Merab Mamardashvili",), "book", 1997, ())
BLOG = Document("f-blog", (), "notes", None, ("c-blogs",))
BLOG_2 = Document("f-blog-2", ("Ann Lee",), "notes", 2025, ("c-blogs", "c-other"))


def test_a_scope_is_every_facet_it_names_and_any_value_of_one():
    assert matches(BOOK, Scope()) and matches(BLOG, Scope())
    assert [d.file_id for d in (BOOK, BLOG, BLOG_2) if matches(d, Scope(collections=["c-blogs"]))] == ["f-blog", "f-blog-2"]
    # Names match however they are cased or spaced.
    assert matches(BOOK, Scope(authors=["merab  mamardashvili"])) and not matches(BLOG, Scope(authors=["Ann Lee"]))
    assert matches(BLOG_2, Scope(collections=["c-blogs"], authors=["Ann Lee"], kinds=["notes", "book"]))
    assert not matches(BLOG_2, Scope(collections=["c-blogs"], kinds=["book"]))
    assert matches(BOOK, Scope(file_ids=["f-book"])) and not matches(BLOG, Scope(file_ids=["f-book"]))


def test_a_year_facet_leaves_out_a_document_of_no_known_year():
    assert matches(BOOK, Scope(year_from=1990, year_to=2000)) and not matches(BOOK, Scope(year_from=2000))
    assert not matches(BLOG, Scope(year_to=2030))


def test_the_label_says_what_the_answer_was_looked_for_in():
    text = label([Scope(collections=["c-blogs"]), Scope(authors=["Ann Lee"])], {"c-blogs": "Блоги"}, 1)
    assert text == "1 file · group Блоги · author Ann Lee"
    assert label([Scope(year_from=2015), Scope()], {}, 3) == "3 files · 2015–"


class FakeClickHouse:
    def __init__(self, documents, collections):
        self.documents, self.collections, self.queries = documents, collections, []

    def query(self, sql, parameters):
        self.queries.append((sql, parameters))
        rows = self.documents if "documents_visible" in sql else self.collections
        return type("Result", (), {"result_rows": rows})


def resolve(clickhouse, scope, narrow=Scope(), user="ann"):
    steps = ScopeSteps(lambda: clickhouse)
    return ActivityEnvironment().run(steps.resolve_scope, ScopeInput(user_id=user, scope=scope, narrow=narrow))


def test_the_chat_s_scope_and_the_message_s_narrowing_both_hold():
    clickhouse = FakeClickHouse(
        documents=[["f-book", ["Merab Mamardashvili"], "book", 1997, []], ["f-blog", [], "notes", None, ["c-blogs"]],
                   ["f-blog-2", ["Ann Lee"], "notes", 2025, ["c-blogs"]]],
        collections=[["c-blogs", "Блоги"]],
    )

    scoped = resolve(clickhouse, Scope(collections=["c-blogs"]), narrow=Scope(authors=["Ann Lee"]))

    assert (scoped.file_ids, scoped.label) == (["f-blog-2"], "1 file · group Блоги · author Ann Lee")
    # Only through the views of what the user may see.
    assert all("_visible(user_id = {u:String})" in sql and p == {"u": "ann"} for sql, p in clickhouse.queries)


def test_a_scope_of_none_of_the_user_s_files_comes_to_nothing():
    # Another user's group id: not among the user's own, so it matches no file.
    clickhouse = FakeClickHouse(documents=[["f-blog", [], "notes", None, ["c-blogs"]]], collections=[["c-blogs", "Блоги"]])

    assert resolve(clickhouse, Scope(collections=["c-bobs"])).file_ids == []


def test_with_no_user_a_scope_reads_nothing():
    clickhouse = FakeClickHouse(documents=[["f-blog", [], "notes", None, []]], collections=[])

    assert resolve(clickhouse, Scope(kinds=["notes"]), user="").file_ids == [] and clickhouse.queries == []
