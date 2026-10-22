"""The profiles of corpora, one file each next to this module, read in the
activities of an answer (rag.profiles says what a profile holds).

A chat names its profile; the workflow carries the name and the activities
read the file, so the workflow never reads a file (it replays). An empty
name is the worker's default: LLM_DEFAULT_PROFILE, else "default". The
version is a hash of the file, returned by route into the workflow history:
what an answer was made with.
"""

import hashlib
import os
import re
import tomllib
from functools import cache
from importlib import resources

from temporalio.exceptions import ApplicationError

from rag.profiles import Profile, profile_from

DEFAULT = "default"
# The name comes from the API and becomes a file name: no dots, no slashes.
# be_service checks the same form (api.rs, validate_profile) to refuse early.
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


def resolve(name: str) -> str:
    return name or os.environ.get("LLM_DEFAULT_PROFILE") or DEFAULT


def load(name: str) -> Profile:
    """The profile of that name, or of the default for an empty one. A name
    that is no profile, or a broken file, fails without retries: a retry
    reads the same file."""
    name = resolve(name)
    if not NAME.fullmatch(name):
        raise ApplicationError(f"profile {name!r}: a name is a-z, 0-9, _ and -", non_retryable=True)
    try:
        return _read(name)
    except FileNotFoundError:
        raise ApplicationError(f"unknown profile {name!r}; there are {', '.join(names())}", non_retryable=True)
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise ApplicationError(f"profile {name!r}: {e}", non_retryable=True) from e


def names() -> list[str]:
    return sorted(f.name.removesuffix(".toml") for f in resources.files(__package__).iterdir()
                  if f.name.endswith(".toml"))


@cache
def _read(name: str) -> Profile:
    # Files change only with the image: read once per process.
    raw = (resources.files(__package__) / f"{name}.toml").read_bytes()
    return profile_from(name, hashlib.sha256(raw).hexdigest()[:12], tomllib.loads(raw.decode()))
