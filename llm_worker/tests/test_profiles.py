import hashlib
from importlib import resources

import pytest
from temporalio.exceptions import ApplicationError

from rag.profiles import profile_from
from rag.topics import KINDS, map_prompt, route_prompt, similar_prompt
from workflow import profiles
from workflow.profiles import check

# The prompts as they were written in rag.topics before profiles, measured
# on the model on 2026-10-21 (the route with aspects on 2026-10-22, every
# control question right 10 times of 10). The philosophy profile must give
# them byte for byte: the same words laid out otherwise moved a question to
# another kind.
ROUTE_BEFORE = """You decide how to answer a message about the user's documents. Return JSON.
- kind, one of three:
  interpret: the user tells their own story, dream, experience or thought and asks what it means or how to
  understand it ("как бы ты описал этот сон? мне снилось, что…", "то есть во сне отыгрывается страх?").
  topic: the user asks what the documents or an author say about a subject, gathered from many passages
  ("что Пруст говорит о смерти?", "что автор имел в виду под топологией пути?", "как Мамардашвили понимает любовь?").
  fact: a definition, a name, a date, a detail ("что такое трансмировая линия?", "кто автор книги?").
- aspects: when the message lists several subjects to cover one by one ("о A, B и C"), each of them, as
  written in the message; an empty list when it asks about one subject. No "and other questions".
- query: what the message asks of the documents, as one short question in the language of the message
  (a message in Russian gets a query in Russian), at most 20 words: the subject and what is asked about it,
  without the story or the examples around it.
- terms: at most 3 words of the query, as written there: the subject asked about first (смерть, любовь,
  сон), then a name or a word that narrows it (Пруст). Never only a name: "что Пруст говорит о смерти?"
  gives смерти, Пруст."""

SIMILAR_BEFORE = """The user asks about a subject named by the term «сон». Question: меняет ли сон человека?
Below are terms found in the user's documents. Return the ones that name the same subject: a synonym,
another form of the word, or a kind of it (for «сон»: сновидение, тяжёлый сон). Leave out terms that are
only related to it (for «сон»: пробуждение, ночь), that sound alike, or that name something else.
Return the terms exactly as written; an empty list if none.

Terms:
сновидение
мечтание"""

MAP_BEFORE = """You read numbered passages from the user's documents and write down what they say
about the question below: one short note per point, with the number of its passage.
Use only the passages; an empty list if none of them says anything about it.
Passages often quote other authors: say whose words a point is ("Proust writes that..."), and never
write the "I" of a quote as your own. Write the notes in the language of the question.

Question: что Пруст говорит о смерти?"""


def test_the_philosophy_profile_gives_the_prompts_measured_before_profiles():
    philosophy = profiles.load("philosophy")

    assert route_prompt(philosophy) == ROUTE_BEFORE
    assert similar_prompt(philosophy, "сон", "меняет ли сон человека?", ["сновидение", "мечтание"]) == SIMILAR_BEFORE
    assert map_prompt(philosophy, "что Пруст говорит о смерти?") == MAP_BEFORE
    assert (philosophy.similar_max_distance, philosophy.similar_candidates) == (0.3, 15)


@pytest.mark.parametrize("name", profiles.names())
def test_every_profile_file_describes_every_kind(name):
    # A broken profile fails here, not in a chat.
    profile = profiles.load(name)

    assert set(profile.kinds) == set(KINDS) and all(profile.kinds.values())
    raw = (resources.files(profiles) / f"{name}.toml").read_bytes()
    assert profile.version == hashlib.sha256(raw).hexdigest()[:12]


def test_the_default_profile_names_no_book():
    default = profiles.load("default")
    prompts = route_prompt(default) + similar_prompt(default, "t", "q", ["c"]) + map_prompt(default, "q")

    assert not any(name in prompts for name in ("Пруст", "Proust", "Мамардашвили", "трансмиров"))


def test_an_empty_name_is_the_default_of_the_worker(monkeypatch):
    monkeypatch.delenv("LLM_DEFAULT_PROFILE", raising=False)
    assert profiles.load("").name == "default"

    monkeypatch.setenv("LLM_DEFAULT_PROFILE", "philosophy")
    assert profiles.load("").name == "philosophy"
    assert profiles.load("default").name == "default"  # a name given wins


@pytest.mark.parametrize("name, message", [
    ("fitness", "unknown profile 'fitness'; there are default, philosophy"),
    ("../secrets", "a name is a-z, 0-9, _ and -"),
    ("Philosophy", "a name is a-z, 0-9, _ and -"),
], ids=["no such profile", "a path", "capitals"])
def test_a_name_that_is_no_profile_fails_without_retries(name, message):
    with pytest.raises(ApplicationError, match=message) as error:
        profiles.load(name)

    assert error.value.non_retryable


def valid():
    return {"route": {"terms": "t", **{k: {"text": "d (\"e\")."} for k in KINDS}},
            "similar": {"max_distance": 0.3, "candidates": 15, "same": "s", "related": "r"},
            "map": {"attribution": "a"}}


@pytest.mark.parametrize("change, message", [
    (lambda d: d["route"].pop("interpret"), r"\[route.interpret\] is missing"),
    (lambda d: d["route"]["topic"].pop("text"), "route.topic.text"),
    (lambda d: d["route"]["fact"].update(text=" "), "route.fact.text"),
    (lambda d: d["similar"].update(max_distance=1.5), "similar.max_distance"),
    (lambda d: d["similar"].update(candidates=True), "similar.candidates"),
    (lambda d: d.pop("map"), r"\[map\] is missing"),
], ids=["a kind missing", "no text", "an empty text", "distance over 1", "not a number", "no attribution"])
def test_a_broken_profile_names_what_is_wrong(change, message):
    data = valid()
    change(data)

    with pytest.raises(ValueError, match=message):
        profile_from("broken", "v", data)


def test_the_layout_of_a_text_is_the_author_s_only_its_ends_are_trimmed():
    data = valid()
    data["map"]["attribution"] = "\n say whose\n   words a point is \n"

    assert profile_from("p", "v", data).attribution == "say whose\n   words a point is"


def test_document_words_are_kept_as_lemmas_and_may_be_left_out():
    data = valid()
    data["route"]["document_words"] = ["книги", "Блог", "файл"]
    assert profile_from("p", "v", data).document_words == {"книга", "блог", "файл"}

    del data["route"]["document_words"]
    assert profile_from("p", "v", data).document_words == frozenset()

    data["route"]["document_words"] = "книга"
    with pytest.raises(ValueError, match="route.document_words"):
        profile_from("p", "v", data)


def test_kind_words_are_lemmas_of_kinds_a_document_can_have():
    data = valid()
    data["route"]["kind_words"] = {"книги": "book", "заметка": "notes"}
    assert profile_from("p", "v", data).kind_words == {"книга": "book", "заметка": "notes"}

    data["route"]["kind_words"] = {"блог": "blog"}
    with pytest.raises(ValueError, match="route.kind_words"):
        profile_from("p", "v", data)


def test_every_control_file_is_of_a_profile_and_wants_known_kinds():
    names = [f.name.removesuffix(".toml") for f in (resources.files("workflow.profiles") / "control").iterdir()
             if f.name.endswith(".toml")]

    assert "philosophy" in names
    for name in names:
        assert name in profiles.names()
        check.control(name)  # a broken file fails here, not in a measurement


def test_the_route_check_counts_what_route_returns_after_its_rules_in_code(monkeypatch):
    questions = [check.Question("что Саша думает о жизни, смерти и любви?", "topic", ["жизнь", "смерть", "любовь"]),
                 check.Question("чем заметки Саши отличаются от книги Мамардашвили?", "compare")]
    monkeypatch.setattr(check, "control", lambda name: ({"authors": ["Саша", "Мамардашвили"]}, questions))

    class FakeLLM:
        chat_model = "qwen3:4b"

        def __init__(self, answers):
            self.answers = answers

        def chat_json(self, messages, schema):
            return self.answers.pop(0)

    listed = {"kind": "interpret", "aspects": ["жизни", "смерти", "любви"], "query": "q", "terms": []}
    lost = {"kind": "interpret", "aspects": [], "query": "q", "terms": []}
    fact = {"kind": "fact", "aspects": [], "query": "q", "terms": []}
    llm = FakeLLM([listed, lost, fact, fact])

    results = check.measure("philosophy", 2, llm)

    # Aspects make a topic of an interpret; two authors make a comparison of a fact.
    assert (dict(results[0].kinds), results[0].aspects_right) == ({"topic": 1, "interpret": 1}, 1)
    assert (dict(results[1].kinds), results[1].aspects_right) == ({"compare": 2}, None)
    assert check.report("philosophy", 2, llm, results).endswith("right every run: 1 of 2 questions; 3 of 4 runs")
