"""Words a PDF itself gets wrong, put right before anything reads them.

Three faults, each of which makes a word unfindable - by search, by the
embedding model, by the answerer quoting it - although a person reading the
page sees it plainly:

*Hyphenated line ends.* Justified text breaks words across lines:
"inspira-" / "tion". Stored as drawn, neither half is the word. The halves
are joined; the hyphen stays only when the hyphenated form is the word
("AI-powered", "open-source"), which a word-frequency list decides: a
joined form that is not a word, or is rarer than its own halves, keeps it.

*Words drawn without spaces.* Some converters place each word with no space
character and no gap between them, so the page itself reads
"Adieselenginehasafuelinjectionsystem". Splitting such runs is only safe
where the page shows the fault: a long run that splits into three or more
common words, little ones like "a", "of", "the" among them, is evidence no
French word ("Totalement"), name ("Halliburton") or technical compound
("flightcrew") gives. On a page with that evidence, a run of letters long
enough to be suspicious, that is not a word, is split into the most
probable sequence of common words - and left alone unless every piece is
one. On any other page nothing is split. Names, codes, CamelCase
identifiers ("JavaScript") and acronyms ("NAVSTARGPS") are never touched.

*Symbol-font bullets.* A bullet drawn in Wingdings or Symbol reaches the text
layer as a private-use code point (U+F0A7, U+F0B7, U+F0D8...): a box
glyph that means nothing to anything downstream. Standing alone, it is
written as the bullet it is.

The word list is wordninja's (MIT): 126,000 English words from Wikipedia by
frequency. Without it installed nothing here changes any text.
"""

import gzip
import importlib.util
import math
import os
import re
from functools import lru_cache

# A run of letters at least this long that is not a word is a candidate for
# splitting. Shorter runs are too often names, codes and abbreviations.
MIN_GLUED = 10
# All-capital runs are acronyms more often than not; only long ones qualify.
MIN_GLUED_CAPITALS = 14
# Inside a token already found glued, shorter runs are split too
# ("outputis150hpat6000rpm").
MIN_GLUED_INSIDE = 5

# Every piece of a split must be at least this common (rank in the list) -
# or, in a run with two or more little words in it, which nothing but glued
# text has, this.
COMMON_RANK = 30_000
LOOSE_RANK = 80_000
# Two pieces and no little word between them is how a compound or a word the
# list lacks splits ("dynamo meter", "poly tropic"): allowed only for very
# common pieces, or in a heading set in capitals.
PAIR_RANK = 3_000
# Pieces longer than this are not looked for.
MAX_WORD = 24

# Evidence that a page has glued words: a run at least this long that splits
# into at least this many pieces, one of them a function word, every other
# piece longer than three letters and this common, and not ending in a
# piece that is a suffix ("experiment at ions", "in toxic at ions").
EVIDENCE_LENGTH = 15
EVIDENCE_PIECES = 3
EVIDENCE_RANK = 30_000
FUNCTION_WORDS = frozenset(
    "a an the of to in on at by for and or is are was were be been as it its "
    "with from that this has have had not into can will how what which do "
    "does if we you your our their his her".split()
)
SUFFIX_PIECES = frozenset(
    "ions ers ings ment ments aire aires ness able ally ives tion tions ance "
    "ence ies ism ist ists ity".split()
)

# A word broken at a line end is joined when the joined form is at least this
# common and one half is a prefix-length piece ("in-put", "ana-log").
JOIN_RANK = 40_000

BULLET = "\u2022"

_LONG_RUN = re.compile(rf"[A-Za-z]{{{MIN_GLUED_INSIDE},}}")
_CANDIDATE = re.compile(rf"[A-Za-z]{{{MIN_GLUED},}}")
_RUN = re.compile(r"[A-Za-z]+")
_NOT_PROSE = re.compile(r"@|://|www\.|\.(com|org|net|in|edu|gov)\b", re.IGNORECASE)
_HYPHEN_END = re.compile(r"([A-Za-z]+)-$")
_LOWER_START = re.compile(r"^([a-z]+)")
_PUNCT_GLUE = re.compile(r"(?<=[A-Za-z]{2})([,;.])(?=[A-Za-z]{2})")
_DIGIT_GLUE = re.compile(r"(?<=[A-Za-z]{2})(?=\d)|(?<=\d)(?=[A-Za-z]{2})")


@lru_cache(maxsize=1)
def _ranks() -> dict:
    spec = importlib.util.find_spec("wordninja")
    if spec is None or not spec.origin:
        return {}
    path = os.path.join(
        os.path.dirname(spec.origin), "wordninja", "wordninja_words.txt.gz"
    )
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            words = handle.read().split()
    except OSError:
        return {}
    return {word: rank for rank, word in enumerate(words)}


def _rank(word: str):
    return _ranks().get(word.lower())


# -- bullets -----------------------------------------------------------------


def is_symbol_bullet(text: str) -> bool:
    """One private-use glyph from a symbol font's bullet range, alone."""
    return len(text) == 1 and "\uf000" <= text <= "\uf0ff"


def bullet(text: str) -> str:
    return BULLET if is_symbol_bullet(text) else text


# -- hyphenated line ends ------------------------------------------------------


def _keeps_hyphen(head: str, tail: str) -> bool:
    """Whether "head-" + "tail" is a hyphenated word rather than a broken one."""
    joined = _rank(head + tail)
    if joined is None:
        return True
    head_rank, tail_rank = _rank(head), _rank(tail)
    if head_rank is None or tail_rank is None:
        return False
    # A prefix-length half ("in", "sub", "ana") is a common word in its own
    # right and says nothing; the joined word decides.
    if min(len(head), len(tail)) <= 3:
        return joined > JOIN_RANK
    # Both halves are words: "con-tribute" joins (contribute is commoner than
    # tribute), "open-source" does not (opensource is rarer than both).
    return joined > max(head_rank, tail_rank)


def dehyphenate(lines: list) -> list:
    """Join words broken across consecutive lines by a hyphen.

    The rest of the word moves up to the line it started on, so each line
    still ends where the page's did, give or take that one word. Only a
    lower-case continuation is joined: "Retrieval-" / "Augmented" is a
    compound whichever way it falls.
    """
    if not _ranks():
        return list(lines)
    out = list(lines)
    index = 0
    while index < len(out) - 1:
        line, following = out[index], out[index + 1]
        end = _HYPHEN_END.search(line)
        start = _LOWER_START.match(following.lstrip()) if following else None
        if not end or not start or line.endswith("--"):
            index += 1
            continue
        head, tail = end.group(1), start.group(1)
        first, _, rest = following.lstrip().partition(" ")
        if _keeps_hyphen(head, tail):
            if _rank(head + tail) is None and tail in FUNCTION_WORDS:
                # "pre-" / "and post-processing": a hyphen left hanging on
                # purpose. "where-" / "as" never gets here - whereas is a word.
                index += 1
                continue
            out[index] = line + first
        else:
            out[index] = line[:-1] + first
        if rest.strip():
            out[index + 1] = rest
            index += 1
        else:
            del out[index + 1]
    return out


# -- glued words ---------------------------------------------------------------


def _segment(run: str):
    """The cheapest split of `run` into listed words, as slices of it."""
    ranks = _ranks()
    lower = run.lower()
    size = len(lower)
    scale = math.log(len(ranks))
    best = [0.0] + [math.inf] * size
    back = [0] * (size + 1)
    for end in range(1, size + 1):
        for begin in range(max(0, end - MAX_WORD), end):
            if best[begin] == math.inf:
                continue
            piece = lower[begin:end]
            rank = ranks.get(piece)
            if rank is None or (len(piece) == 1 and piece not in ("a", "i")):
                continue
            cost = best[begin] + math.log((rank + 1) * scale)
            if cost < best[end]:
                best[end], back[end] = cost, begin
    if best[size] == math.inf:
        return None
    pieces = []
    end = size
    while end > 0:
        begin = back[end]
        pieces.append(run[begin:end])
        end = begin
    return pieces[::-1]


def _case_breaks(run: str) -> set:
    return {i for i in range(1, len(run)) if run[i - 1].islower() and run[i].isupper()}


def _split_run(run: str, minimum: int):
    """`run` as separate words, or None when it should stay as it is."""
    if len(run) < minimum or _rank(run) is not None:
        return None
    capitals = run.isupper()
    if capitals and len(run) < max(minimum, MIN_GLUED_CAPITALS):
        return None
    pieces = _segment(run)
    if not pieces or len(pieces) < 2:
        return None
    lowered = [piece.lower() for piece in pieces]
    little = sum(1 for piece in lowered if piece in FUNCTION_WORDS)
    limit = LOOSE_RANK if little >= 2 else COMMON_RANK
    if any(_rank(piece) > limit for piece in lowered):
        return None
    # One-letter pieces other than "a", and two-letter ones that are not
    # little words, are how a split of a word that is merely unlisted -
    # "Hall i burton", "act iv at ions" - gives itself away.
    for piece in lowered:
        if len(piece) == 1 and piece != "a":
            return None
        if len(piece) == 2 and piece not in FUNCTION_WORDS:
            return None
    if capitals and any(len(piece) < 3 for piece in pieces):
        return None
    if len(pieces) == 2 and not little and not capitals:
        if any(len(piece) < 4 or _rank(piece) > PAIR_RANK for piece in lowered):
            return None
    # "JavaScript", "PowerPoint": a split exactly at the capitals is how the
    # name is meant to be written.
    breaks, positions, at = _case_breaks(run), set(), 0
    for piece in pieces[:-1]:
        at += len(piece)
        positions.add(at)
    if breaks and positions <= breaks:
        return None
    short = sum(1 for piece in pieces if len(piece) <= 2)
    if short > len(pieces) / 2 or len(run) / len(pieces) < 2.5:
        return None
    return pieces


def _split_token(token: str) -> str:
    if _NOT_PROSE.search(token):
        return token
    pieces = []
    glued = False
    position = 0
    for match in _RUN.finditer(token):
        pieces.append(token[position : match.start()])
        words = _split_run(match.group(), MIN_GLUED)
        if words:
            glued = True
            pieces.append(" ".join(words))
        else:
            pieces.append(match.group())
        position = match.end()
    pieces.append(token[position:])
    if not glued:
        return token

    # Found glued: shorter runs in the same token are suspect too, and the
    # punctuation and figures stuck between words get their spaces back.
    text = "".join(pieces)
    text = _RUN.sub(
        lambda m: " ".join(_split_run(m.group(), MIN_GLUED_INSIDE) or [m.group()]),
        text,
    )
    text = _PUNCT_GLUE.sub(r"\1 ", text)
    return _DIGIT_GLUE.sub(" ", text)


def _is_evidence(run: str) -> bool:
    if len(run) < EVIDENCE_LENGTH or run.isupper():
        return False
    pieces = _split_run(run, EVIDENCE_LENGTH)
    if not pieces or len(pieces) < EVIDENCE_PIECES:
        return False
    lowered = [piece.lower() for piece in pieces]
    if not any(piece in FUNCTION_WORDS for piece in lowered):
        return False
    if lowered[-1] in SUFFIX_PIECES:
        return False
    return all(
        piece in FUNCTION_WORDS or (len(piece) > 3 and _rank(piece) <= EVIDENCE_RANK)
        for piece in lowered
    )


def has_glued_words(texts) -> bool:
    """Whether these texts (one page's words) show words drawn without spaces."""
    if not _ranks():
        return False
    for text in texts:
        if not text or not _CANDIDATE.search(text) or _NOT_PROSE.search(text):
            continue
        if any(_is_evidence(run) for run in _CANDIDATE.findall(text)):
            return True
    return False


def split_glued(text: str) -> str:
    """`text` with words drawn without spaces between them separated.

    For a page `has_glued_words` found the fault on; elsewhere, leave the
    text alone.
    """
    if not text or not _LONG_RUN.search(text) or not _ranks():
        return text
    if not _CANDIDATE.search(text):
        return text
    return " ".join(
        _split_token(token) if len(token) >= MIN_GLUED else token
        for token in text.split(" ")
    )


def repair_lines(text: str) -> str:
    """`split_glued` on each line of a multi-line string."""
    return "\n".join(split_glued(line) for line in text.split("\n"))
