"""The route of a profile measured on its control questions (control/<name>.toml):
the activity route itself, its rules in code included, against the model
server of LLM_BASE_URL, with the user's documents taken from the control
file instead of ClickHouse. A small model answers a question differently run
to run, so every question is asked several times.

    make route-check P=philosophy RUNS=5
    python -m workflow.profiles.check philosophy 5
"""

import collections
import sys
import tomllib
from dataclasses import dataclass
from importlib import resources

from temporalio.testing import ActivityEnvironment

from rag.topics import COMPARE, KINDS, lemmas
from workflow.activities.topic import TopicSteps
from workflow.contracts import RouteInput
from workflow.llm import LLM
from workflow.profiles import load

# What reads the facets: route reads nothing without a user.
USER = "control"


@dataclass
class Question:
    text: str
    kind: str
    # None: not checked; empty: none wanted.
    aspects: list[str] | None = None


@dataclass
class Result:
    question: Question
    kinds: collections.Counter
    # Runs whose aspects were as wanted; None when not checked.
    aspects_right: int | None

    def right(self, runs: int) -> int:
        """Runs right in both: at least the fewer of the two."""
        return min(self.kinds[self.question.kind], runs if self.aspects_right is None else self.aspects_right)


class Facets:
    """Stands in for ClickHouse in route: the user's authors, kinds and groups."""

    def __init__(self, facets: dict):
        self.documents = [[facets.get("authors", []), kind] for kind in facets.get("kinds") or [""]]
        self.groups = [[name, f"group-{i}"] for i, name in enumerate(facets.get("groups", []))]

    def query(self, sql, parameters):
        rows = self.documents if "documents_visible" in sql else self.groups
        return type("Result", (), {"result_rows": rows})


def control(name: str) -> tuple[dict, list[Question]]:
    """The facets and the questions of a profile; ValueError naming what is wrong."""
    path = resources.files(__package__) / "control" / f"{name}.toml"
    if not path.is_file():
        raise ValueError(f"no control questions for profile {name!r}: {path}")
    data = tomllib.loads(path.read_text())
    questions = []
    for item in data.get("question", []):
        if item.get("kind") not in (*KINDS, COMPARE):
            raise ValueError(f"{name}: {item.get('text')!r} wants kind {item.get('kind')!r}")
        questions.append(Question(text=item["text"], kind=item["kind"], aspects=item.get("aspects")))
    if not questions:
        raise ValueError(f"{name}: no [[question]]")
    return data.get("facets", {}), questions


def stored(words: list[str]) -> set[str]:
    return {lemmas(w) or w.strip().lower() for w in words}


def measure(name: str, runs: int, llm: LLM) -> list[Result]:
    facets, questions = control(name)
    steps = TopicSteps(lambda: Facets(facets), llm)
    results = []
    for question in questions:
        kinds, aspects_right = collections.Counter(), 0
        for _ in range(runs):
            route = ActivityEnvironment().run(steps.route, RouteInput(question=question.text, profile=name, user_id=USER))
            kinds[route.kind] += 1
            if question.aspects is not None:
                want, got = stored(question.aspects), stored(route.aspects)
                aspects_right += (want <= got) if want else not got
        results.append(Result(question, kinds, aspects_right if question.aspects is not None else None))
    return results


def report(name: str, runs: int, llm: LLM, results: list[Result]) -> str:
    profile = load(name)
    lines = [f"profile {profile.name} {profile.version}, {llm.chat_model}, {runs} runs a question"]
    for r in results:
        others = ", ".join(f"{k} {n}" for k, n in r.kinds.items() if k != r.question.kind)
        kind = f"{r.kinds[r.question.kind]}/{runs} {r.question.kind}" + (f" ({others})" if others else "")
        aspects = f"aspects {r.aspects_right}/{runs}" if r.aspects_right is not None else ""
        lines.append(f"{kind:<34} {aspects:<14} {r.question.text[:70]}")
    every = sum(r.right(runs) == runs for r in results)
    lines.append(f"right every run: {every} of {len(results)} questions; "
                 f"{sum(r.right(runs) for r in results)} of {runs * len(results)} runs")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) not in (2, 3):
        sys.exit("usage: python -m workflow.profiles.check <profile> [runs]")
    name, runs = sys.argv[1], int(sys.argv[2]) if len(sys.argv) == 3 else 5
    llm = LLM.from_env()
    print(report(name, runs, llm, measure(name, runs, llm)))


if __name__ == "__main__":
    main()
