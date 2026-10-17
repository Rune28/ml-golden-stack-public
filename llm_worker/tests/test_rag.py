import pytest

from rag.chunking import chunk, read_text


def test_read_text_drops_the_bom_and_windows_newlines():
    assert read_text("﻿a\r\nb\rc".encode()) == "a\nb\nc"


def test_short_paragraphs_are_packed_into_one_chunk():
    assert chunk("# Title\n\nFirst.\n\n\nSecond.", size=100, overlap=20) == ["# Title\n\nFirst.\n\nSecond."]


def test_a_new_chunk_repeats_a_short_last_paragraph_of_the_previous_one():
    text = "\n\n".join(["a" * 40, "b" * 10, "c" * 40])

    chunks = chunk(text, size=60, overlap=20)

    assert chunks == ["a" * 40 + "\n\n" + "b" * 10, "b" * 10 + "\n\n" + "c" * 40]


def test_a_long_paragraph_is_cut_into_overlapping_windows_within_size():
    text = "".join(str(i % 10) for i in range(250))

    chunks = chunk(text, size=100, overlap=20)

    assert all(len(c) <= 100 for c in chunks)
    assert chunks[0][-20:] == chunks[1][:20]
    assert chunks[-1].endswith(text[-10:])


def test_no_chunk_is_ever_longer_than_size():
    text = "\n\n".join(["x" * n for n in (5, 90, 15, 70, 3, 99, 12)])

    assert max(len(c) for c in chunk(text, size=100, overlap=20)) <= 100


def test_empty_text_gives_no_chunks_and_overlap_must_be_smaller_than_size():
    assert chunk("  \n\n \n") == []
    with pytest.raises(ValueError):
        chunk("text", size=10, overlap=10)
