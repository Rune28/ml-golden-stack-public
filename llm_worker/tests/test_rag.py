import pytest

from pdf_fixture import make_pdf
from rag.chunking import chunk, distinct, read_text
from rag.documents import (
    HEAD,
    author_id,
    document_id,
    find_doi,
    find_isbn,
    looks_like_a_person,
    metadata_request,
    parse_metadata,
)


def test_read_text_drops_the_bom_and_windows_newlines():
    assert read_text("﻿a\r\nb\rc".encode()) == "a\nb\nc"


def test_a_pdf_is_read_from_its_text_layer_a_page_a_paragraph():
    text = read_text(make_pdf([["Squat depth", "and knee load"], ["Second page"]]))

    pages = text.split("\n\n")
    assert len(pages) == 2
    assert "Squat depth" in pages[0] and "knee load" in pages[0] and "Second page" in pages[1]


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


def test_distinct_keeps_the_first_of_each_text_in_order():
    assert distinct(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_a_doi_is_taken_from_the_first_pages_only():
    assert find_doi("Squats. https://doi.org/10.1519/JSC.0000000000003456. Abstract") == "10.1519/jsc.0000000000003456"
    # A paper without a DOI of its own still cites the DOIs of others.
    assert find_doi("x" * HEAD + " References: 10.1000/cited.paper") is None


def test_an_isbn_is_kept_as_digits():
    assert find_isbn("Printed in 2020. ISBN 978-5-17-118366-1") == "9785171183661"
    assert find_isbn("ISBN-10: 0-306-40615-2") == "0306406152"
    assert find_isbn("no isbn here") is None


def test_the_document_key_is_the_doi_then_the_isbn_then_the_file():
    assert document_id("10.1/a", "9785171183661", "f") == "doi:10.1/a"
    assert document_id(None, "9785171183661", "f") == "isbn:9785171183661"
    assert document_id(None, None, "f") == "file:f"


def test_an_author_is_one_key_however_the_name_is_written():
    assert author_id("Smith, John A.") == author_id("John A. Smith") == "name:smith j a"
    assert author_id("Мария Иванова") == "name:иванова м"
    # Initials written without a space before the surname.
    assert author_id("М.Пруст") == author_id("М. Пруст") == "name:пруст м"
    assert author_id("J.A. Smith") == "name:smith j a"
    assert author_id("José Núñez", "https://orcid.org/0000-0002-1825-009x") == "orcid:0000-0002-1825-009X"
    assert author_id("José Núñez", "not an orcid") == "name:nunez j"


def test_the_model_answer_is_checked():
    meta = parse_metadata({
        "title": " Squat depth ",
        "people": [{"name": "John A. Smith", "role": "author"}, {"name": "Smith, J. A.", "role": "author"},
                   {"name": "", "role": "author"}, {"name": "Jane Doe", "role": "editor"}, "broken"],
        "year": 3021,
        "kind": "poem",
        "abstract": None,
        "keywords": ["squat", "squat", " knee ", 5],
    })

    assert (meta.title, meta.year, meta.kind, meta.abstract) == ("Squat depth", None, "other", "")
    assert meta.keywords == ["squat", "knee"]
    assert [a.author_id for a in meta.authors] == ["name:smith j a"]


def test_the_model_gets_the_file_name_and_the_length_of_the_whole_text():
    text = "Глава 1.\n\n" + "слово " * 100_000

    request = metadata_request("Merab_Mamardashvili_-_Topologia.txt", text)

    assert "File name: Merab_Mamardashvili_-_Topologia.txt" in request
    assert f"Length of the whole text: {len(text):,} characters" in request
    assert request.endswith(text[:HEAD])


BOOK = "Merab_Mamardashvili_-_Psikhologicheskaya_topologia_puti_single.txt"


def test_whom_a_book_is_about_or_who_edited_it_is_not_its_author():
    # The title page of the lectures names Proust's novel and the editors;
    # the lecturer is only in the file name. Asked for authors, the model
    # gave "М. Пруст" 5 times of 5.
    meta = parse_metadata({
        "people": [{"name": "М.Пруст", "role": "subject"}, {"name": "О. Мандельштам", "role": "quoted"},
                   {"name": "Ю. П. Сенокосов", "role": "editor"}, {"name": "И. К. Мамардашвили", "role": "editor"}],
        "file_name_author": "Merab_Mamardashvili",
    }, BOOK)

    assert [a.name for a in meta.authors] == ["Merab Mamardashvili"]


def test_what_the_model_writes_for_no_author_is_no_author():
    # Measured on the blogs, which name no author: "автор", "Меня", the file name.
    for people, from_name, file in [
        ([{"name": "автор", "role": "author"}], "", "блог.txt"),
        ([{"name": "Меня", "role": "author"}], "", "блог 4.txt"),
        ([], "блог 5.txt", "блог 5.txt"),
        ([], "блог 3", "блог 3.txt"),
        ([], "Иван Петров", "блог.txt"),  # not in the file name
    ]:
        assert parse_metadata({"people": people, "file_name_author": from_name}, file).authors == [], (people, from_name)


def test_a_person_is_initials_a_latin_name_or_a_name_pymorphy_knows():
    assert looks_like_a_person("Ю. П. Сенокосов") and looks_like_a_person("Merab Mamardashvili")
    assert looks_like_a_person("Мамардашвили") and looks_like_a_person("Келли")
    assert not looks_like_a_person("автор") and not looks_like_a_person("Меня") and not looks_like_a_person("блог")
    assert not looks_like_a_person("Squats", "Squats.pdf")


def test_a_file_named_after_a_person_not_among_the_page_authors_is_theirs():
    # With roles the model still marked Proust the author 4 times of 5.
    book = parse_metadata({"people": [{"name": "М.Пруст", "role": "author"}],
                           "file_name_author": "Merab Mamardashvili"}, BOOK)
    paper = parse_metadata({"people": [{"name": "John A. Smith", "role": "author"},
                                       {"name": "Maria Ivanova", "role": "author"}],
                            "file_name_author": "Smith"}, "Smith2021_squat.pdf")

    assert [a.name for a in book.authors] == ["Merab Mamardashvili"]
    assert [a.name for a in paper.authors] == ["John A. Smith", "Maria Ivanova"]
