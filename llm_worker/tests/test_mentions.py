from collections import Counter

from rag.mentions import candidates, pools, themes, top_mentions, unit


def test_russian_candidates_are_nouns_and_agreed_adjective_noun_pairs():
    terms = candidates("Он говорит о трансмировых линиях и нашей жадности.")

    assert "трансмировая линия" in terms and "линия" in terms and "жадность" in terms
    # A verb is not a term, and a pronominal adjective does not make a pair.
    assert "говорить" not in terms and not any(t.startswith("наш") for t in terms)


def test_english_candidates_leave_function_words_out():
    terms = candidates("The back squats of the lifters")

    assert terms == ["back", "squat", "back squat", "lifter"]


def test_a_term_in_most_chunks_of_a_long_document_says_nothing_about_one_chunk():
    texts = [f"Пруст и {word}." for word in ["смерть", "стиль", "время", "память", "сон",
                                              "город", "море", "звук", "свет", "сад", "дом", "лес"]]

    best, df = pools(texts)

    assert df["пруст"] == 12
    assert all("пруст" not in pool for pool in best)
    assert best[1] == ["стиль"]


def test_in_a_short_document_every_term_can_be_a_keyword():
    best, _ = pools(["Пруст и смерть.", "Пруст и стиль."])

    assert "пруст" in best[0]


def test_mmr_takes_terms_close_to_the_chunk_and_far_from_each_other():
    # Closeness to the chunk as bge-m3 gives it for good terms (about 0.5),
    # and two near-duplicates: стиль and стилистика.
    chunk = [1.0, 0.0, 0.0]
    vectors = {
        "стиль": unit([0.55, 0.835, 0.0]),
        "стилистика": unit([0.54, 0.84, 0.0]),
        "бюффон": unit([0.49, 0.0, 0.87]),
    }

    taken = top_mentions(chunk, ["стилистика", "стиль", "бюффон"], vectors, k=2)

    assert [t for t, _ in taken] == ["стиль", "бюффон"]
    assert abs(taken[0][1] - 0.55) < 0.01


def test_themes_are_frequent_in_the_document_and_rare_in_the_language():
    # In a book on Proust: Пруст in half of the chunks, человек in 41%, Альбертина in 8%.
    df = Counter({"пруст": 48, "человек": 41, "альбертина": 8, "сад": 2})

    found = themes(df, chunks=100)

    assert [t.term for t in found][:2] == ["пруст", "альбертина"]
    assert "человек" not in [t.term for t in found]  # a common word of the language
    assert "сад" not in [t.term for t in found]  # in too few chunks
    assert [t.rank for t in found] == list(range(1, len(found) + 1))
