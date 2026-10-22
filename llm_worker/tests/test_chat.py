import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflow import ChatWorkflow
from workflow.chat import HISTORY
from workflow.contracts import (
    Broad,
    ChatInput,
    ChatMessage,
    GatherInput,
    Gathered,
    GenerateInput,
    Generated,
    MessageInput,
    RecordInput,
    RetrieveInput,
    Route,
    RouteInput,
    Scope,
    ScopeInput,
    Scoped,
    Section,
    Side,
    Source,
    SummarizeInput,
)

SOURCE = Source(file_id="file-1", index_run_id="idx-1", chunk_id=3, name="notes.md", distance=0.2)


TOPIC = [Source(file_id="file-1", index_run_id="idx-1", chunk_id=n, name="book.txt") for n in (7, 9)]


class FakeSteps:
    """Stands in for the steps of an answer: records calls, answers "answer <n>".
    `route` and `gathered` set what a topic message gets; by default every
    message is about a fact."""

    def __init__(self):
        self.calls = []
        self.route_to = Route(kind="fact")
        self.gathered = Gathered(mode="chunks", terms=["смерть"], found=2, sources=TOPIC)
        self.summary = "summary of {n} chunks"
        self.scoped = Scoped(file_ids=["file-1"], label="1 file · group Блоги")

    @activity.defn(name="route")
    async def route(self, input: RouteInput) -> Route:
        self.calls.append(("route", input))
        return self.route_to

    @activity.defn(name="gather")
    async def gather(self, input: GatherInput) -> Gathered:
        self.calls.append(("gather", input))
        return self.gathered

    @activity.defn(name="summarize")
    async def summarize(self, input: SummarizeInput) -> Generated:
        self.calls.append(("summarize", input))
        return Generated(text=self.summary.format(n=len(input.sources)), model="qwen3:4b")

    @activity.defn(name="resolve_scope")
    async def resolve_scope(self, input: ScopeInput) -> Scoped:
        self.calls.append(("resolve_scope", input))
        return self.scoped

    @activity.defn(name="retrieve")
    async def retrieve(self, input: RetrieveInput) -> list[Source]:
        self.calls.append(("retrieve", input))
        return [SOURCE]

    @activity.defn(name="generate")
    async def generate(self, input: GenerateInput) -> Generated:
        self.calls.append(("generate", input))
        return Generated(text=f"answer to {input.question}", model="qwen3:4b")

    @activity.defn(name="record")
    async def record(self, input: RecordInput) -> None:
        self.calls.append(("record", input))

    def of(self, step):
        return [arg for name, arg in self.calls if name == step]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest.fixture
async def chat(env):
    """A running chat workflow and a way to send it messages."""
    steps, queue, chat_id = FakeSteps(), str(uuid.uuid4()), str(uuid.uuid4())

    async def start(max_turns=50):
        handle = await env.client.start_workflow(
            ChatWorkflow.run, ChatInput(chat_id=chat_id, max_turns=max_turns), id=f"chat-{chat_id}", task_queue=queue
        )

        async def send(text, run_id=None, user_id="ann", profile="", scope=None):
            message = MessageInput(text=text, run_id=run_id or str(uuid.uuid4()), user_id=user_id, profile=profile,
                                   scope=scope or Scope())
            # The workflow ID without a run: after continue-as-new it reaches the new run.
            latest = env.client.get_workflow_handle(handle.id)
            return await latest.execute_update(ChatWorkflow.send_message, message)

        return handle, send

    async with Worker(env.client, task_queue=queue, workflows=[ChatWorkflow],
                      activities=[steps.retrieve, steps.generate, steps.record,
                                  steps.route, steps.gather, steps.summarize, steps.resolve_scope]):
        yield steps, start


async def test_a_message_is_retrieved_generated_and_recorded_in_order(chat):
    steps, start = chat
    _, send = await start()

    answer = await send("what is S3?", run_id="ans-1")

    assert [name for name, _ in steps.calls] == ["route", "retrieve", "generate", "record"]
    assert steps.of("generate")[0].sources == [SOURCE]
    [record] = steps.of("record")
    assert (record.run_id, record.turn, record.question, record.answer) == ("ans-1", 1, "what is S3?", "answer to what is S3?")
    assert record.sources == [SOURCE]
    assert (answer.run_id, answer.turn, answer.text, answer.sources) == ("ans-1", 1, "answer to what is S3?", [SOURCE])


async def test_the_second_answer_sees_the_first(chat):
    steps, start = chat
    _, send = await start()

    await send("first")
    second = await send("second")

    first_call, second_call = steps.of("generate")
    assert first_call.history == []
    assert second_call.history == [ChatMessage("user", "first"), ChatMessage("assistant", "answer to first")]
    assert second.turn == 2


async def test_the_model_sees_only_the_last_messages(chat):
    steps, start = chat
    _, send = await start()

    for i in range(HISTORY):
        await send(f"q{i}")

    last = steps.of("generate")[-1].history
    assert len(last) == HISTORY
    assert last[-1] == ChatMessage("assistant", f"answer to q{HISTORY - 2}")


async def test_continue_as_new_carries_the_window_and_the_turn_count(chat, env):
    steps, start = chat
    handle, send = await start(max_turns=2)

    await send("first")
    await send("second")
    third = await send("third")

    description = await env.client.get_workflow_handle(handle.id).describe()
    assert description.run_id != handle.result_run_id
    assert third.turn == 3
    assert steps.of("generate")[-1].history[-2:] == [ChatMessage("user", "second"), ChatMessage("assistant", "answer to second")]


async def test_an_empty_message_is_rejected_before_any_step(chat):
    steps, start = chat
    _, send = await start()

    with pytest.raises(WorkflowUpdateFailedError):
        await send("   ")

    assert steps.calls == []


async def test_a_topic_is_gathered_from_its_chunks_and_summarized(chat):
    steps, start = chat
    steps.route_to = Route(kind="topic", terms=["смерти"])
    _, send = await start()

    answer = await send("что там говорится о смерти?", run_id="ans-1")

    assert [name for name, _ in steps.calls] == ["route", "gather", "summarize", "record"]
    assert steps.of("gather")[0].terms == ["смерти"]
    assert steps.of("gather")[0].min_chunks == ChatInput("c").min_topic_chunks
    assert steps.of("summarize")[0].sources == TOPIC
    [record] = steps.of("record")
    assert (record.answer, record.sources) == ("summary of 2 chunks", TOPIC)
    assert (answer.text, answer.sources) == ("summary of 2 chunks", TOPIC)


async def test_a_topic_too_broad_is_answered_without_reading_anything(chat):
    steps, start = chat
    steps.route_to = Route(kind="topic", terms=["Пруст"])
    steps.gathered = Gathered(mode="too_broad", terms=["пруст"],
                              broad=[Broad(term="пруст", file_id="file-1", name="Топология пути", chunks=1064)])
    _, send = await start()

    answer = await send("что имел в виду Пруст?")

    assert [name for name, _ in steps.calls] == ["route", "gather", "record"]
    assert "слишком широкая" in answer.text and "~1064" in answer.text
    assert answer.sources == []


async def test_a_topic_whose_words_name_nothing_stored_is_answered_as_a_fact(chat):
    steps, start = chat
    steps.route_to = Route(kind="topic", terms=["квазары"])
    steps.gathered = Gathered(mode="none", terms=["квазар"])
    _, send = await start()

    answer = await send("что там о квазарах?")

    assert [name for name, _ in steps.calls] == ["route", "gather", "retrieve", "generate", "record"]
    assert answer.sources == [SOURCE]


async def test_chunks_of_a_topic_that_say_nothing_about_the_question_leave_it_to_a_fact(chat):
    steps, start = chat
    steps.route_to = Route(kind="topic", terms=["смерти"])
    steps.summary = ""
    _, send = await start()

    answer = await send("что там о смерти бабочек?")

    assert [name for name, _ in steps.calls] == ["route", "gather", "summarize", "retrieve", "generate", "record"]
    assert answer.sources == [SOURCE]


async def test_a_topic_is_gathered_by_its_query_and_its_notes_taken_on_the_message(chat):
    # A message retold around its question was gathered by its story.
    steps, start = chat
    message = "Вопрос про сон. Сначала ты летишь и смотришь на магистраль..."
    steps.route_to = Route(kind="topic", terms=["сон"], query="меняет ли сон человека?")
    steps.summary = ""  # so the fact branch runs too
    _, send = await start()

    await send(message)

    assert steps.of("gather")[0].question == "меняет ли сон человека?"
    [summarize] = steps.of("summarize")
    assert (summarize.question, summarize.query) == (message, "меняет ли сон человека?")
    # A fact is searched by the message: the story finds the passages about it.
    assert steps.of("retrieve")[0].question == message
    assert steps.of("generate")[0].question == message
    assert steps.of("record")[0].question == message


async def test_a_story_to_interpret_is_answered_from_the_chunks_closest_to_it(chat):
    # A dream sent to a topic came back an overview of the book that never
    # saw the question; from the closest chunks, an answer about that dream.
    steps, start = chat
    message = "как бы ты описал этот сон? мне снилось, что я лечу над магистралью"
    steps.route_to = Route(kind="interpret", terms=["сон"], query="как описать этот сон?")
    _, send = await start()

    answer = await send(message)

    assert [name for name, _ in steps.calls] == ["route", "retrieve", "generate", "record"]
    assert steps.of("retrieve")[0].question == message and steps.of("generate")[0].question == message
    assert answer.sources == [SOURCE]


async def test_a_topic_without_a_query_is_gathered_by_the_message(chat):
    steps, start = chat
    steps.route_to = Route(kind="topic", terms=["смерти"])
    _, send = await start()

    await send("что там о смерти?")

    assert steps.of("gather")[0].question == "что там о смерти?"
    assert steps.of("summarize")[0].query == "что там о смерти?"


async def test_the_user_of_a_message_reaches_every_step_that_reads(chat):
    steps, start = chat
    _, send = await start()

    await send("what is S3?", user_id="ann")
    steps.route_to = Route(kind="topic", terms=["смерти"])
    await send("что там говорится о смерти?", user_id="ann")

    for step in ("retrieve", "generate", "gather", "summarize"):
        assert [i.user_id for i in steps.of(step)] == ["ann"], step


async def test_the_first_profile_named_sticks_to_the_chat_and_reaches_every_step_that_reads_it(chat):
    steps, start = chat
    steps.route_to = Route(kind="topic", terms=["присед"], profile="fitness", profile_version="abc")
    _, send = await start()

    await send("что исследования говорят о приседе?", profile="fitness")
    await send("а о жиме?", profile="philosophy")  # one chat, one corpus
    answer = await send("а о становой?")

    assert answer.profile == "fitness"
    for step in ("route", "gather", "summarize"):
        assert [i.profile for i in steps.of(step)] == ["fitness"] * 3, step


async def test_a_chat_that_names_no_profile_keeps_the_default_its_first_route_resolved(chat):
    steps, start = chat
    steps.route_to = Route(kind="fact", profile="philosophy", profile_version="abc")
    _, send = await start()

    await send("что такое трансмировая линия?")
    await send("а кто автор?", profile="fitness")

    # The first route asked for the default; from then on the chat asks for what it resolved.
    assert [i.profile for i in steps.of("route")] == ["", "philosophy"]


async def test_continue_as_new_carries_the_profile(chat, env):
    steps, start = chat
    handle, send = await start(max_turns=1)

    await send("first", profile="fitness")
    await send("second")

    description = await env.client.get_workflow_handle(handle.id).describe()
    assert description.run_id != handle.result_run_id
    assert [i.profile for i in steps.of("route")] == ["fitness", "fitness"]


BLOGS = Scope(collections=["c-blogs"])


async def test_a_chat_without_a_scope_reads_every_visible_file_and_resolves_nothing(chat):
    steps, start = chat
    _, send = await start()

    answer = await send("what is S3?")

    assert "resolve_scope" not in [name for name, _ in steps.calls]
    assert steps.of("retrieve")[0].file_ids == [] and answer.scope_label == ""


async def test_the_first_scope_sticks_and_every_step_that_reads_gets_its_files(chat):
    steps, start = chat
    _, send = await start()

    await send("проследи путь автора", scope=BLOGS)
    steps.route_to = Route(kind="topic", terms=["путь"])
    answer = await send("а что о времени?", scope=Scope(kinds=["book"]))  # one chat, one scope

    assert [i.scope for i in steps.of("resolve_scope")] == [BLOGS, BLOGS]
    assert steps.of("retrieve")[0].file_ids == ["file-1"] and steps.of("gather")[0].file_ids == ["file-1"]
    assert answer.scope_label == "1 file · group Блоги"


async def test_a_scope_of_no_file_is_said_and_nothing_is_searched(chat):
    # Never answered from every file instead.
    steps, start = chat
    steps.scoped = Scoped(file_ids=[], label="0 files · group Пустая")
    _, send = await start()

    answer = await send("что там о смерти?", scope=Scope(collections=["c-empty"]))

    assert [name for name, _ in steps.calls] == ["route", "resolve_scope", "record"]
    assert answer.text.startswith("В выбранной области нет ни одного документа (0 files · group Пустая)")
    assert answer.sources == []


async def test_the_message_s_own_words_narrow_the_chat_s_scope(chat):
    steps, start = chat
    steps.route_to = Route(kind="fact", filters=Scope(authors=["Merab Mamardashvili"]))
    _, send = await start()

    await send("что Мамардашвили говорит о времени?", scope=BLOGS)

    [resolved] = steps.of("resolve_scope")
    assert (resolved.scope, resolved.narrow) == (BLOGS, Scope(authors=["Merab Mamardashvili"]))


async def test_continue_as_new_carries_the_scope(chat, env):
    steps, start = chat
    handle, send = await start(max_turns=1)

    await send("first", scope=BLOGS)
    await send("second")

    description = await env.client.get_workflow_handle(handle.id).describe()
    assert description.run_id != handle.result_run_id
    assert [i.scope for i in steps.of("resolve_scope")] == [BLOGS, BLOGS]


async def test_a_comparison_gathers_every_aspect_in_every_side_apart_and_summarizes_them_in_sections(chat):
    steps, start = chat
    steps.route_to = Route(kind="compare", query="чем отличаются", terms=["философия"], aspects=["смерть", "любовь"],
                           sides=[Side("Саша", Scope(authors=["Саша"])), Side("Мамардашвили", Scope(authors=["Мамардашвили"]))])
    _, send = await start()

    answer = await send("что Саша думает о смерти и любви, и чем он отличается от Мамардашвили?")

    # The documents of each side, then a share of the chunks for each aspect in them.
    assert [s.narrow.authors for s in steps.of("resolve_scope")] == [["Саша"], ["Мамардашвили"]]
    gathers = steps.of("gather")
    assert [(g.question, g.terms, g.file_ids, g.fill) for g in gathers] == [
        ("смерть", ["смерть"], ["file-1"], True), ("любовь", ["любовь"], ["file-1"], True)] * 2
    assert all(g.max_chunks == 15 for g in gathers)  # 60 for 2 sides of 2 aspects
    [summarize] = steps.of("summarize")
    # A chunk gathered twice is one passage, numbered once.
    assert summarize.sources == TOPIC and answer.sources == TOPIC
    assert summarize.sections == [Section("смерть", "Саша", [1, 2]), Section("любовь", "Саша", [1, 2]),
                                  Section("смерть", "Мамардашвили", [1, 2]), Section("любовь", "Мамардашвили", [1, 2])]


async def test_a_comparison_whose_sides_have_no_files_is_answered_as_a_fact(chat):
    steps, start = chat
    steps.route_to = Route(kind="compare", sides=[Side("A", Scope(authors=["A"])), Side("B", Scope(authors=["B"]))])
    steps.scoped = Scoped(file_ids=[], label="0 files")
    _, send = await start()

    await send("чем A отличается от B?")

    assert [name for name, _ in steps.calls] == ["route", "resolve_scope", "resolve_scope", "retrieve", "generate", "record"]
