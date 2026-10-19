"""Text of an uploaded file, cut into chunks that are embedded and retrieved
one by one. A chunk should hold one thought: paragraphs are kept whole when
they fit, packed together up to `size` characters, and a short paragraph at
the end of a chunk is repeated at the start of the next one, so a question
about the boundary still finds both sides."""

import re

SIZE = 1000
OVERLAP = 200

_PARAGRAPHS = re.compile(r"\n\s*\n")


def read_text(raw: bytes) -> str:
    """UTF-8 text with Unix newlines; a byte-order mark is dropped."""
    text = raw.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def chunk(text: str, size: int = SIZE, overlap: int = OVERLAP) -> list[str]:
    if overlap >= size:
        raise ValueError(f"overlap {overlap} must be smaller than size {size}")
    pieces = [p for paragraph in _paragraphs(text) for p in _split_long(paragraph, size, overlap)]

    chunks: list[list[str]] = []
    length = 0
    for piece in pieces:
        if chunks and length + len(piece) + 2 <= size:
            chunks[-1].append(piece)
            length += len(piece) + 2
            continue
        # A new chunk starts with the last piece of the previous one, if that
        # one is short and both still fit (a chunk of one piece is not repeated).
        last = chunks[-1][-1] if chunks and len(chunks[-1]) > 1 else None
        carry = [last] if last and len(last) <= overlap and len(last) + 2 + len(piece) <= size else []
        chunks.append(carry + [piece])
        length = sum(len(p) + 2 for p in chunks[-1])
    return ["\n\n".join(c) for c in chunks]


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARAGRAPHS.split(text) if p.strip()]


def _split_long(paragraph: str, size: int, overlap: int) -> list[str]:
    """A paragraph longer than a chunk: windows of `size`, each overlapping the previous by `overlap`."""
    if len(paragraph) <= size:
        return [paragraph]
    step = size - overlap
    return [paragraph[start : start + size] for start in range(0, len(paragraph) - overlap, step)]


def distinct(chunks: list[str]) -> list[str]:
    """Each text once, the first time it appears, in order. A file that repeats
    itself (a document converted twice into one) would otherwise put the same
    text into the index twice, and retrieval would spend two of its few places
    on it. Deterministic, so a retried index writes the same chunks."""
    return list(dict.fromkeys(chunks))
