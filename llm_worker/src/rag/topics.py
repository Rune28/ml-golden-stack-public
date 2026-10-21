"""Questions about a topic, as opposed to questions about a fact. Pure: the
prompts, the schema of the routing answer, and how the words of a question
become the terms the keywords of the chunks are stored by.

A question about a fact ("what is a transworld line?") is answered from the
few chunks closest to it. A question about a topic ("what does the book say
about death?") needs every chunk the topic is a keyword of, summarized a
group at a time and then together (map-reduce).
"""

import re

from rag.mentions import candidates

# Chunks one topic answer reads by default; more are ranked by closeness to
# the question. The workflow passes its own (principle 7). At 60 every
# keyword of a 2218-chunk book is read whole (the most frequent is in 57
# chunks), in about two and a half minutes; 200 would take some eight, past
# what the chat page waits, and keep the model server from every other chat.
MAX_CHUNKS = 60
# A topic in fewer chunks is answered as a fact: retrieve reads 5 chunks
# closest to the whole question, and a map-reduce over one or two chunks
# retells them. "Настоящая жизнь" was a keyword of a single chunk of a book,
# cut in the middle of the thought, and its answer was that chunk retold.
MIN_CHUNKS = 6
# Chunks per call of the map step: they fit the context of a small model.
GROUP = 6

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["fact", "topic"]},
        "terms": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["kind", "terms"],
}

ROUTE_PROMPT = """You decide how to answer a message about the user's documents. Return JSON.
- kind: topic when the message asks to gather what the documents say about a subject across many
  passages: what someone meant, what is said about something, an overview, everything about it.
  fact when a few passages answer it: a definition, a detail, a specific question.
- terms: for a topic, the words that name the subject, as written in the message (names and nouns,
  at most 3); for a fact, an empty list."""

# Map and reduce answer in JSON: a schema makes the model write the answer
# from its first token, so qwen3 does not think first. In free text it
# thinks whatever it is asked (a minute a call instead of ten seconds) and
# writes the thinking into the answer; five such notes overflowed the
# context of the reduce step, which then saw only the last of them.
MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"passage": {"type": "integer"}, "note": {"type": "string"}},
                "required": ["passage", "note"],
            },
        }
    },
    "required": ["notes"],
}

MAP_PROMPT = """You read numbered passages from the user's documents and write down what they say
about the question below: one short note per point, with the number of its passage.
Use only the passages; an empty list if none of them says anything about it.
Passages often quote other authors: say whose words a point is ("Proust writes that..."), and never
write the "I" of a quote as your own. Write the notes in the language of the question.

Question: {question}"""

# Reduce in steps: themes first, then a paragraph per theme. One call asked
# to group the notes and write the whole answer gave either a paragraph per
# note in a row (a retelling, not an answer) or, told to group, a single
# sentence listing the groups; a small model does one of the two well at a
# time. A paragraph sees only the notes of its theme.
THEMES_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"theme": {"type": "string"}, "passages": {"type": "array", "items": {"type": "integer"}}},
                "required": ["theme", "passages"],
            },
        }
    },
    "required": ["themes"],
}

THEMES_PROMPT = """Group the notes below, taken on numbered passages of the user's documents, into 3 to 5 themes
that answer the question. Each theme: a short phrase in the language of the question, and the passages it covers.
Every passage goes to one theme.

Question: {question}

Notes:
{notes}"""

PARAGRAPH_SCHEMA = {"type": "object", "properties": {"paragraph": {"type": "string"}}, "required": ["paragraph"]}

# The paragraph and condense steps never see the question: the theme carries
# its focus, the notes its language. Given a long message (a dream retold
# over 700 characters), qwen3 answered the message instead and cited its
# sentences as passages; the instructions now go in the system message and
# the notes in the user's, as in the map step.
PARAGRAPH_PROMPT = """Write one paragraph on the theme below from the notes the user gives, taken on numbered
passages of the user's documents. Say what the notes have in common and where they differ, in 3 to 5 sentences,
in the language of the notes, citing passages as [3]. Start with the idea itself: no "the notes say", no list of
passages. Keep whose words a point is as the notes give it. Use only the notes.

Theme: {theme}"""

# The second level of the reduce: notes too many for one call are condensed a
# batch at a time, keeping their passage numbers, until they fit.
CONDENSE_PROMPT = """The user gives notes taken on numbered passages of the user's documents.
Merge the notes that say the same thing and shorten the rest: one note per point, each with the number
of a passage it comes from, in the language of the notes. Keep whose words a point is. Use only the notes."""

# The notes the reduce step reads, at most: with the prompt, they fit the context.
MAX_NOTES_CHARS = 12_000


def lemmas(term: str) -> str | None:
    """The stored form of a term from a question: "Пруста" → пруст, "трансмировых
    линий" → трансмировая линия (the longest candidate: the whole phrase)."""
    found = candidates(term)
    return max(found, key=lambda t: (len(t.split()), len(t))) if found else None


def groups(items: list, size: int = GROUP) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def notes_from(answer: dict, passages: set[int]) -> list[tuple[int, str]]:
    """The notes of a map answer, checked: a note must name one of the
    passages it was given and say something."""
    out = []
    for item in answer.get("notes") or []:
        if not isinstance(item, dict):
            continue
        passage, note = item.get("passage"), item.get("note")
        if isinstance(passage, int) and passage in passages and isinstance(note, str) and note.strip():
            out.append((passage, note.strip()))
    return out


def render_notes(notes: list[tuple[int, str]], limit: int | None = MAX_NOTES_CHARS) -> str:
    """Notes as the reduce step reads them, in the order of the passages, cut at the limit."""
    lines, size = [], 0
    for passage, note in sorted(notes):
        line = f"[{passage}] {note}"
        if limit is not None and size + len(line) > limit:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def note_batches(notes: list[tuple[int, str]], limit: int) -> list[list[tuple[int, str]]]:
    """Notes in the order of the passages, in batches of at most `limit` characters."""
    batches, size = [[]], 0
    for passage, note in sorted(notes):
        if batches[-1] and size + len(note) > limit:
            batches.append([])
            size = 0
        batches[-1].append((passage, note))
        size += len(note) + 8
    return [b for b in batches if b]


def themes_from(answer: dict, passages: set[int]) -> list[tuple[str, list[int]]]:
    """The themes of an answer, checked: a theme has a name and passages it was
    given, and a passage belongs to the first theme that names it."""
    out, taken = [], set()
    for item in answer.get("themes") or []:
        if not isinstance(item, dict) or not isinstance(item.get("theme"), str) or not item["theme"].strip():
            continue
        mine = [p for p in item.get("passages") or [] if isinstance(p, int) and p in passages and p not in taken]
        if mine:
            out.append((item["theme"].strip(), mine))
            taken.update(mine)
    return out


_CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def cites(paragraph: str, passages: list[int]) -> bool:
    """Whether a paragraph cites one of the passages of its theme. A paragraph
    that cites none is not an answer: the model wrote instructions to itself
    ("only the answer, no labels...") instead of the paragraph."""
    cited = {int(n) for group in _CITATION.findall(paragraph) for n in group.split(",")}
    return bool(cited & set(passages))


def assemble(paragraphs: list[tuple[str, str]]) -> str:
    """The answer: a heading and a paragraph per theme."""
    return "\n\n".join(f"**{theme}**\n{paragraph}" for theme, paragraph in paragraphs)
