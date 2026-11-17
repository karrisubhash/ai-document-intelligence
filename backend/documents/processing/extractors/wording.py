"""Words a PDF itself gets wrong, put right before anything reads them.

Three faults, each of which makes a word unfindable - by search, by the
embedding model, by the answerer quoting it - although a person reading the
page sees it plainly:

*Hyphenated line ends.* Justified text breaks words across lines:
"inspira-" / "tion". Stored as drawn, neither half is the word. The halves
are joined; the hyphen stays only when the hyphenated form is the word
("AI-powered", "open-source"), which a word-frequency list decides. Where the
join could change what is said - "vis-" / "a-vis", "go-" / "to", "short-" /
"but" - nothing is touched: a line left as drawn is a small blemish, a word
changed into another word is a wrong text.

*Words drawn without spaces.* Some converters place each word with no space
character and no gap between them, so the page itself reads
"Adieselenginehasafuelinjectionsystem". The repair is built so that it can only
be wrong on something that is NOT glued text, and then rarely:

- A run of letters is split only into words from a list of 126,000, every one
  of them common (a rank limit), and only when the pieces look like a sentence:
  a function word among them (of, the, a, and...), no scrap of one or two
  letters that is not one.
- Evidence comes first. A run of 20-24 letters or more that splits into four
  or five common words, a function word among them, is something no real word
  is - and a page with such a run is a page whose converter dropped its
  spaces. Only on that page, shorter runs (12 letters and up) are split too,
  under stricter rules still: every content word at least four letters and
  common, a function word among the pieces. A page with no such run is left
  exactly as drawn, whatever words on it the list lacks.
- Names, codes, paths, links, hashtags, identifiers and anything with code
  characters are never touched; neither is a run the list itself contains.
- Spaces are added only at the edges of a run that was split: between its
  words, after a comma or full stop that ends it, and around a plain number
  stuck to it. Letter-digit boundaries elsewhere in the token ("23ME4120",
  "H2SO4", "MP3") are left alone.

*Symbol-font bullets.* A bullet drawn in Wingdings or Symbol reaches the text
layer as a private-use code point (U+F0A7, U+F0B7, U+F0D8...): a box glyph that
means nothing to anything downstream. At the start of a line, from a short list
of glyphs that are bullets in practice, it is written as the bullet it is. The
same code points elsewhere are Greek letters, arrows, ticks and crosses, and
stay exactly as drawn.

The word list is wordninja's (MIT, extractors/data/): 126,000 English words
from Wikipedia by frequency. Without the file nothing here changes any text,
and the miss is logged.
"""

import gzip
import logging
import math
import os
import re
import zlib
from functools import lru_cache

# The word list ships with the code (extractors/data/, MIT licence beside it):
# an sdist-only dependency would be built from whatever build backend PyPI had
# on the day of the image build, and a missing file would switch these repairs
# off without a word.
WORD_LIST = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "wordninja_words.txt.gz"
)
logger = logging.getLogger("adi.extraction")

BULLET = chr(0x2022)
# Private-use glyphs that are list bullets and nothing else (Wingdings: small
# and large black squares, an arrow, a diamond-cross; Symbol: the bullet). The
# tick (U+F0FC), the empty box (U+F0A8) and their relatives are checklist
# symbols as often as bullets - "Fee receipt attached" ticked, an option not
# chosen - and stay as drawn, like Greek letters and arrows.
LINE_BULLETS = frozenset(chr(code) for code in (0xF0A7, 0xF0B7, 0xF0D8, 0xF06E, 0xF076))

FUNCTION_WORDS = frozenset(
    "a an the of to in on at by for and or is are was were be been as it its "
    "with from that this has have had not into can will how what which do "
    "does if we you your our their".split()
)
# The function words real glued text cannot do without. The short ones (a, an,
# in, is, it, on, at, as, or, be, by, to, do) are also fragments of longer
# words - "cinema to graphical", "accident a list", "special is at ions" - so a
# split needs at least one of THESE among its pieces.
STRONG_FUNCTION_WORDS = frozenset(
    "the of and that with from this which for are was were has have had not "
    "into will how what does their your our you we".split()
)
# A split that ends in one of these is a derivation, not a sentence:
# "accident a list", "carbon is able", "document at ions", "cinema to graphical".
SUFFIX_PIECES = frozenset(
    "list lists able ably ability abilities ness ions ion ment ments ally "
    "ical ically logic logical graph graphic graphical meter genesis ship ous "
    "ive ish less ful ize ise ized ised ing ings ism ist ists itis ology "
    "tonic tropic rhea cent or is".split()
)
# Words after which a line-end hyphen is left alone: "go-" / "to", "short-" /
# "but", "vis-" / "a-vis". Joining them changes the word or the sense.
HYPHEN_TAILS = FUNCTION_WORDS | frozenset(
    "but versus vs nor than via per yet so no".split()
)

# -- glued words: what counts ------------------------------------------------

# A run longer than this is not a sentence; it also bounds the work.
MAX_RUN = 400

# Evidence: a run this long splitting into this many common pieces (a function
# word among them) can only be words drawn together.
STRONG_LENGTH = 20
STRONG_LONG = 24  # from this length four pieces are enough, below it five
STRONG_PIECES = 5
STRONG_PIECES_LONG = 4
# A long run that splits into listed words is evidence whichever words they
# are, and a long word cannot be a fragment of another: only the short ones
# must be common.
STRONG_RANKS = ((5, 25_000), (99, 100_000))

# On a page with evidence, shorter runs are split too, under stricter rules.
WEAK_LENGTH = 12
WEAK_PIECES = 3
# How common a content word must be depends on its length: a short piece is
# as likely to be a fragment of a longer word, a long one is not.
WEAK_RANKS = ((4, 6_000), (5, 10_000), (7, 15_000), (99, 40_000))
# A split of a run of capitals ("CYCLESANDANALYSIS") needs more: no word list
# can tell a heading from a word set in capitals.
CAPS_LENGTH = 16
CAPS_RANKS = ((5, 6_000), (7, 8_000), (99, 20_000))
CAPS_MIN_PIECE = 5
# Words glued at their capitals, in a list or a sentence with punctuation
# ("Prerequisite:EngineeringMathematics,EngineeringMechanics"): two long words
# split exactly where the capitals mark the boundary.
CAMEL_LENGTH = 16
CAMEL_PIECE = 5
CAMEL_RANK = 15_000

# A word broken at a line end is joined when the joined form is at least this
# common and one half is a prefix-length piece ("in-put", "ana-log").
JOIN_RANK = 40_000

# Tokens with any of these are code, links, paths, hashtags or addresses.
_CODEISH = re.compile(
    r"[@#_=/\\<>{}|]|://|www\.|"
    r"\.(?:com|org|net|edu|gov|ac|co|in|io|py|java|js|php|html?|pdf|docx?|txt)(?![A-Za-z])",
    re.IGNORECASE,
)
_LETTERS = re.compile(r"[A-Za-z]+")
_NUMBERISH = re.compile(r"^\d[\d.,:/%-]*$")
_HYPHEN_WORD = re.compile(r"([A-Za-z]+)-$")
_LOWER_START = re.compile(r"^([a-z]+)")


@lru_cache(maxsize=1)
def _ranks() -> dict:
    """word -> rank (0 = most frequent). Empty, and logged, if the file is
    gone or damaged: a truncated or corrupt file must switch the repairs off,
    not fail every page of every PDF."""
    try:
        with gzip.open(WORD_LIST, "rt", encoding="utf-8") as handle:
            words = handle.read().split()
        if not words:
            raise EOFError("the word list is empty")
    except (OSError, EOFError, UnicodeError, zlib.error) as exc:
        logger.error(
            "extraction.word_list_missing",
            extra={"path": WORD_LIST, "error": repr(exc)},
        )
        return {}
    return {word: rank for rank, word in enumerate(words)}


def _rank(word: str):
    return _ranks().get(word.lower())


# -- bullets -----------------------------------------------------------------


def bullet_line(text: str) -> str:
    """`text` with a symbol-font bullet at its start written as a bullet."""
    if text and text[0] in LINE_BULLETS:
        rest = text[1:]
        return BULLET + (rest if rest[:1] in ("", " ") else " " + rest)
    return text


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


def _hyphen_head(line: str):
    """The word a line ends in when it ends at a hyphen, else None."""
    if not line.endswith("-") or line.endswith("--"):
        return None
    # The last short stretch only: a regex over a long line is quadratic.
    found = _HYPHEN_WORD.search(line[-40:])
    return found.group(1) if found else None


def dehyphenate(lines: list) -> list:
    """Join words broken across consecutive lines by a hyphen.

    The rest of the word moves up to the line it started on, so each line
    still ends where the page's did, give or take that one word. Left exactly
    as drawn, because the join could change what is said:

    - a continuation that is itself hyphenated ("vis-" / "a-vis") or that
      starts with a function word or a word of one or two letters ("go-" /
      "to", "short-" / "but", "to-" / "do");
    - a continuation that is not lower case ("Retrieval-" / "Augmented").
    """
    if not _ranks() or not any(line.endswith("-") for line in lines[:-1]):
        return list(lines)
    out = list(lines)
    index = 0
    while index < len(out) - 1:
        line, following = out[index], out[index + 1]
        head = _hyphen_head(line)
        first, _, rest = following.lstrip().partition(" ")
        start = _LOWER_START.match(first)
        if head is None or start is None:
            index += 1
            continue
        tail = start.group(1)
        after = first[len(tail) :]
        if tail in HYPHEN_TAILS or len(tail) <= 2 or after.startswith("-"):
            index += 1
            continue
        out[index] = line + first if _keeps_hyphen(head, tail) else line[:-1] + first
        if rest.strip():
            out[index + 1] = rest
            index += 1
        else:
            del out[index + 1]
    return out


# -- glued words ---------------------------------------------------------------


def _segment(run: str):
    """The cheapest split of `run` into listed words, as slices of it."""
    pieces = _segmented(run)
    return None if pieces is None else list(pieces)


@lru_cache(maxsize=4096)
def _segmented(run: str):
    """`_segment`, remembered: the strong and the weak test both ask for it."""
    ranks = _ranks()
    lower = run.lower()
    size = len(lower)
    scale = math.log(len(ranks))
    best = [0.0] + [math.inf] * size
    back = [0] * (size + 1)
    for end in range(1, size + 1):
        for begin in range(max(0, end - 24), end):
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
    return tuple(pieces[::-1])


def _case_breaks(run: str) -> set:
    return {i for i in range(1, len(run)) if run[i - 1].islower() and run[i].isupper()}


def _boundaries(pieces: list) -> set:
    at, found = 0, set()
    for piece in pieces[:-1]:
        at += len(piece)
        found.add(at)
    return found


def _scraps(lowered: list) -> bool:
    """A piece that is a one- or two-letter fragment and no function word."""
    for piece in lowered:
        if len(piece) == 1 and piece not in ("a", "i"):
            return True
        if len(piece) == 2 and piece not in FUNCTION_WORDS:
            return True
    return False


def _sentence_like(lowered: list) -> bool:
    """Whether the pieces read like words drawn together, not like a word cut up.

    A strong function word among them (the short ones are fragments of longer
    words), no scrap of one or two letters that is not a function word, and not
    ending in a suffix - which would make the run a derived word.
    """
    if not any(piece in STRONG_FUNCTION_WORDS for piece in lowered):
        return False
    if _scraps(lowered) or lowered[-1] in SUFFIX_PIECES:
        return False
    return True


def _limit(length: int, table) -> int:
    for up_to, rank in table:
        if length <= up_to:
            return rank
    return table[-1][1]


def _candidate(run: str):
    """(pieces, lowered pieces) for a letter run worth looking at, else None."""
    if len(run) < WEAK_LENGTH or len(run) > MAX_RUN or _rank(run) is not None:
        return None
    pieces = _segment(run)
    if not pieces or len(pieces) < 2:
        return None
    lowered = [piece.lower() for piece in pieces]
    # "IndexOutOfBoundsException": every boundary at a capital is an
    # identifier written that way, not words drawn together.
    breaks = _case_breaks(run)
    if breaks and _boundaries(pieces) <= breaks:
        return None
    return pieces, lowered


def _strong(run: str):
    """Pieces of a run that can only be words drawn together, else None."""
    if len(run) < STRONG_LENGTH:
        return None
    found = _candidate(run)
    if found is None:
        return None
    pieces, lowered = found
    needed = STRONG_PIECES_LONG if len(run) >= STRONG_LONG else STRONG_PIECES
    if len(pieces) < needed or not _sentence_like(lowered):
        return None
    # A heading in capitals is read the same way, but a short one is as likely
    # to be a name: capitals need the longer length.
    if run.isupper() and len(run) < STRONG_LONG:
        return None
    for piece in lowered:
        if piece in FUNCTION_WORDS:
            continue
        if len(piece) < 3 or _rank(piece) > _limit(len(piece), STRONG_RANKS):
            return None
    return pieces


def _camel_list(run: str, token: str):
    """Two long words split exactly at the capitals, in a list or sentence."""
    breaks = _case_breaks(run)
    if not breaks or len(run) < CAMEL_LENGTH or not re.search(r"[,;:.]", token):
        return None
    pieces = _segment(run)
    if not pieces or len(pieces) != 2 or not _boundaries(pieces) <= breaks:
        return None
    if all(len(p) >= CAMEL_PIECE and _rank(p) <= CAMEL_RANK for p in pieces):
        return pieces
    return None


def _weak(run: str, token: str):
    """Pieces of a shorter run on a page that has evidence, else None."""
    if WEAK_LENGTH <= len(run) <= MAX_RUN and _rank(run) is None:
        listed = _camel_list(run, token)
        if listed:
            return listed
    found = _candidate(run)
    if found is None:
        return None
    pieces, lowered = found
    capitals = run.isupper()
    if capitals and len(run) < CAPS_LENGTH:
        return None
    if len(pieces) < WEAK_PIECES or not _sentence_like(lowered):
        return None
    table = CAPS_RANKS if capitals else WEAK_RANKS
    floor = CAPS_MIN_PIECE if capitals else 4
    for piece in lowered:
        if piece in FUNCTION_WORDS:
            continue
        if len(piece) < floor or _rank(piece) > _limit(len(piece), table):
            return None
    # A run that uses capitals marks its word boundaries with them; a break
    # between two content words that falls elsewhere cuts a word.
    breaks = _case_breaks(run)
    if breaks:
        at = 0
        for left, right in zip(pieces, pieces[1:], strict=False):
            at += len(left)
            if (
                at not in breaks
                and left.lower() not in FUNCTION_WORDS
                and right.lower() not in FUNCTION_WORDS
            ):
                return None
    return pieces


def _at_parenthesis(token: str, match) -> bool:
    """Whether a letter run is a function name or an argument: "println(" or
    "(numberofstudents". Local to the run: in glued text a parenthesis beside
    some other run ("affected(Tepas,1993).Generallyveryfew...") is a citation."""
    before = token[match.start() - 1] if match.start() > 0 else ""
    after = token[match.end()] if match.end() < len(token) else ""
    return before == "(" or after == "("


def _split_run(run: str, token: str, gated: bool):
    pieces = _strong(run)
    if pieces is None and gated:
        pieces = _weak(run, token)
    return pieces


def _repair_token(token: str, gated: bool) -> str:
    """`token` with its glued runs split, spaces only at the edges of a split."""
    if _CODEISH.search(token):
        return token
    runs = list(_LETTERS.finditer(token))
    if not runs:
        return token
    splits = [
        None
        if _at_parenthesis(token, match)
        else _split_run(match.group(), token, gated)
        for match in runs
    ]
    if not any(splits):
        return token

    out = []
    position = 0
    for index, (match, pieces) in enumerate(zip(runs, splits, strict=True)):
        previous_split = index > 0 and splits[index - 1] is not None
        out.append(
            _edge(
                token[position : match.start()], previous_split, pieces, match.group()
            )
        )
        out.append(" ".join(pieces) if pieces else match.group())
        position = match.end()
    out.append(_edge(token[position:], splits[-1] is not None, None, ""))
    return "".join(out)


def _edge(between: str, left_split: bool, right_pieces, right_run: str) -> str:
    """The non-letters between two runs, spaced where a split run touches them.

    Only a plain number, or punctuation that ends a split run: never inside a
    code-like stretch ("23ME4120", "H2SO4", "MP3" are left as drawn).
    """
    if not between:
        return between
    right_split = right_pieces is not None
    if _NUMBERISH.match(between):
        # "delivers1000bar": a number stuck to a split run, and a short unit
        # word stuck to the number, are separate words.
        unit = bool(right_run) and len(right_run) <= 4 and right_run.islower()
        before = " " if left_split else ""
        after = " " if right_split or (left_split and unit) else ""
        return before + between + after
    if (left_split or right_split) and right_run and between[-1] in ",;:.":
        # The comma or full stop between a split run and the next word, in
        # either direction. A full stop only before a capital: a new sentence,
        # not "forms.gle".
        if between[-1] != "." or right_run[0].isupper():
            return between + " "
    return between


def _repair_texts(text: str, gated: bool) -> str:
    return " ".join(
        _repair_token(token, gated) if len(token) >= WEAK_LENGTH else token
        for token in text.split(" ")
    )


def has_glued_words(texts) -> bool:
    """Whether these texts (one page's words) hold a run that can only be glued."""
    if not _ranks():
        return False
    for text in texts:
        if not text or len(text) < STRONG_LENGTH:
            continue
        for token in text.split(" "):
            if len(token) < STRONG_LENGTH or _CODEISH.search(token):
                continue
            if any(
                _strong(match.group())
                for match in _LETTERS.finditer(token)
                if not _at_parenthesis(token, match)
            ):
                return True
    return False


def split_glued(text: str, gated: bool = True) -> str:
    """`text` with words drawn without spaces separated.

    `gated` says the page has evidence (`has_glued_words`); without it only
    runs that are evidence in themselves are split.
    """
    if not text or len(text) < WEAK_LENGTH or not _ranks():
        return text
    return _repair_texts(text, gated)


def repair_lines(text: str, gated: bool = True) -> str:
    """`split_glued` on each line of a multi-line string."""
    return "\n".join(split_glued(line, gated) for line in text.split("\n"))
