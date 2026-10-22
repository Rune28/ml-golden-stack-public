"""Keywords of a document at two levels, without a language model.

- Of a chunk: what sets it apart from the other chunks of its document.
  Candidates are nouns and adjective + noun, as lemmas; the ones frequent in
  this chunk and rare in the rest of the document (TF-IDF, the chunks of the
  document as its documents) are ranked by how close their embedding is to
  the chunk's, and the top ones are taken one by one so that each is close
  to the chunk and far from those already taken (MMR).
- Of the document: what it is about. A word in many chunks of the document
  can be its topic ("Пруст" in half of a book on Proust) or just a common
  word ("человек"); TF-IDF within one document cannot tell them apart, so
  the share of chunks is weighed against how common the word is in the
  language (wordfreq).

Measured on a book of 2218 chunks: two seconds for the lemmas and the
counts, two minutes for the embeddings of the candidates; a language model
per chunk took two hours for the same book. Terms stay in the language of
the text; the concept graph (a Job) maps them to canonical concepts.

Pure: the activity extract_mentions reads the chunks, embeds and writes.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import pymorphy3
from wordfreq import zipf_frequency

PER_CHUNK = 5
# Candidates per chunk that reach the embedding: the best by TF-IDF.
POOL = 15
# A term in more of the document's chunks than this says nothing about one chunk.
MAX_SHARE = 0.2
# Of the document: a term in fewer chunks than this is not its topic.
MIN_THEME_CHUNKS = 3
# zipf_frequency: about 7 for the most common words, 0 for words never seen.
# A word at or above COMMON is a common word of the language, whatever its
# share: человек 6.0, время 6.2, жизнь 5.7 (a theme lost this way, such as
# время in lectures on time, is for the concept graph to bring back).
COMMON = 5.5
# MMR: closeness to the chunk against closeness to the terms already taken.
MMR_LAMBDA = 0.7

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]*")
_CYRILLIC = re.compile(r"[а-яё]")
# English has no part-of-speech tagger here: words of these kinds are left out instead.
_EN_STOP = frozenset("""
a about above after again against all also am an and any are as at be because been before being below between
both but by can could did do does doing down during each either et etc few for from further had has have having
he her here hers herself him himself his how however i if in into is it its itself just less may me might more
most much must my myself no nor not now of off on once only or other ought our ours ourselves out over own per
rather same shall she should so some such than that the their theirs them themselves then there these they this
those through thus to too under until up upon us use used using very via was we were what when where whether
which while who whom whose why will with within without would yes yet you your yours yourself yourselves
al fig figure table data result results study studies method methods using based however although
""".split())


@dataclass(frozen=True)
class Theme:
    term: str
    rank: int
    chunks: int
    share: float
    zipf: float
    score: float


@lru_cache(maxsize=1)
def _morph() -> pymorphy3.MorphAnalyzer:
    # Its dictionaries take a second and some 15 MB: once per process.
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=200_000)
def _parse(word: str):
    return _morph().parse(word)[0]


_LATIN = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                  ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                   "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))


def latin(word: str) -> str:
    """A word in Latin letters, lowercase: Мамардашвили and Mamardashvili are one."""
    return "".join(_LATIN.get(c, c) for c in word.lower())


def word_lemmas(text: str) -> set[str]:
    """The lemma of every word of a text, lowercase (ё as е); a word not in
    Russian as it is: "в своих блогах" → {в, свой, блог}."""
    out = set()
    for raw in _WORD.findall(text):
        word = raw.lower().replace("ё", "е")
        out.add(_parse(word).normal_form.replace("ё", "е") if _CYRILLIC.search(word) else word)
    return out


def names_a_person(word: str) -> bool:
    """Whether pymorphy reads a Russian word as a first name, a surname or a
    patronymic in any of its readings: Пруст, Мамардашвили, Мераб; not
    автор, меня or блог."""
    return any({"Name", "Surn", "Patr"} & set(p.tag.grammemes) for p in _morph().parse(word.lower().replace("ё", "е")))


def _english(word: str) -> str | None:
    """A crude lemma of an English word, or None for a word left out."""
    if word in _EN_STOP or len(word) < 3:
        return None
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    return word


def candidates(text: str) -> list[str]:
    """Terms of a text, as many times as they occur. Russian: nouns, and an
    adjective + noun with the adjective agreed (трансмировая линия), not
    pronominal adjectives (наш, какой-то). English: words that are not
    function words, and two of them in a row (back squat)."""
    out: list[str] = []
    previous = None  # ("ru", parse) or ("en", lemma) of the word before
    for raw in _WORD.findall(text):
        word = raw.lower().replace("ё", "е")
        if _CYRILLIC.search(word):
            p = _parse(word)
            if p.tag.POS == "NOUN" and len(p.normal_form) > 2:
                out.append(p.normal_form)
                if previous and previous[0] == "ru" and previous[1].tag.POS == "ADJF" and "Apro" not in previous[1].tag:
                    grammemes = {"nomn", "sing", p.tag.gender} if p.tag.gender else {"nomn", "plur"}
                    agreed = previous[1].inflect(grammemes)
                    out.append(f"{(agreed or previous[1]).word} {p.normal_form}")
            previous = ("ru", p)
        else:
            lemma = _english(word)
            if lemma:
                out.append(lemma)
                if previous and previous[0] == "en":
                    out.append(f"{previous[1]} {lemma}")
            previous = ("en", lemma) if lemma else None
    return out


def pools(texts: list[str], pool: int = POOL, max_share: float = MAX_SHARE) -> tuple[list[list[str]], Counter]:
    """For every chunk, its best candidates by TF-IDF within the document;
    and in how many chunks each term occurs."""
    counts = [Counter(candidates(t)) for t in texts]
    df = Counter(term for tf in counts for term in tf)
    n = len(texts)
    # A document of a few chunks has no "rest of the document" to compare to:
    # then the share is not a reason to leave a term out.
    limit = max_share * n if n >= 10 else n + 1
    best = []
    for tf in counts:
        scored = sorted(((c * math.log(n / df[t]), t) for t, c in tf.items() if df[t] < limit), reverse=True)
        best.append([t for _, t in scored[:pool]])
    return best, df


def unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def top_mentions(chunk: list[float], pool: list[str], vectors: dict[str, list[float]],
                 k: int = PER_CHUNK, lam: float = MMR_LAMBDA) -> list[tuple[str, float]]:
    """The k terms of the pool for one chunk with their closeness to it, by MMR.
    Vectors are unit length, so a dot product is the cosine."""
    closeness = {t: dot(vectors[t], chunk) for t in pool}
    left, taken = list(pool), []
    while left and len(taken) < k:
        best = max(left, key=lambda t: lam * closeness[t] - (1 - lam) * max(
            (dot(vectors[t], vectors[s]) for s, _ in taken), default=0.0))
        left.remove(best)
        taken.append((best, closeness[best]))
    return taken


def zipf(term: str) -> float:
    """How common a term is in the language, the higher of Russian and English."""
    return max(zipf_frequency(term, "ru"), zipf_frequency(term, "en"))


def themes(df: Counter, chunks: int, min_chunks: int = MIN_THEME_CHUNKS) -> list[Theme]:
    """Every term in at least min_chunks chunks that is not a common word of
    the language, ranked: its share of the chunks times its rarity, squared
    so that rarity counts for more than frequency. Readers take the top ones."""
    scored = []
    for term, n in df.items():
        if n < min_chunks:
            continue
        z = zipf(term)
        rarity = max(0.0, COMMON - z)
        if rarity == 0.0:
            continue
        share = n / chunks
        scored.append((share * rarity ** 2, term, n, share, z))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [Theme(term, rank, n, share, z, score) for rank, (score, term, n, share, z) in enumerate(scored, 1)]
