"""Sentences and words, with offsets that point back into the page text.

Institutional documents are not prose. A PDF's text layer wraps lines at
the page width, headings sit on their own lines without a full stop,
clauses start with "4.2" or "(a)", and "Rs." or "No." end in a period
without ending a sentence. A splitter that only looks for ". " produces
sentences that start mid-heading and end mid-amount; this one first cuts
the page into blocks at blank lines, headings and list items, and only then
looks for sentence ends inside a block.

Every Segment carries (page_number, start, end) into the page text exactly
as stored, trimmed of surrounding whitespace, so `page_text[start:end]` is
the segment's text - character for character, internal line breaks
included.
"""

import re
from dataclasses import dataclass

from documents.processing.tables.text_rows import ROW_PREFIX

STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are aren't as at
    be because been before being below between both but by can cannot could
    did do does doing done down during each either etc even ever every few for
    from further had has have having he her here hers herself him himself his
    how however i if in into is it its itself just least less let like made
    make many may me might more most much must my myself neither no nor not
    now of off often on once one only or other others otherwise our ours
    ourselves out over own per please same shall she should since so some
    such than that the their theirs them themselves then there these they
    this those though through thus till to too under unless until up upon us
    use used very via was we were what when where whereas whether which while
    who whom whose why will with within without would yet you your yours
    yourself yourselves th st nd rd i.e e.g viz ie eg
    """.split()
)

# Words that end in a period without ending the sentence.
ABBREVIATIONS = frozenset(
    """
    rs no nos dr mr mrs ms prof sr jr st vs viz etc approx dept govt ltd co inc
    fig figs sec cl art vol pp p jan feb mar apr jun jul aug sep sept oct nov
    dec mon tue wed thu fri sat sun e.g i.e ref memo lr dt b.tech m.tech b.e
    m.e b.sc m.sc b.a m.a ph.d smt shri sri hon asst assoc
    """.split()
)

LINE = re.compile(r"[^\n]*\n?")
LIST_ITEM = re.compile(
    r"^\s*(?:"
    r"(?:clause|section|rule|regulation|article|unit|chapter|part)\s+[A-Z]?\d+(?:\.\d+)*"
    r"|\d{1,2}(?:\.\d{1,2})+\.?(?=\s)"
    r"|\d{1,2}[.)](?=\s)"
    r"|\(?[a-hivx]{1,4}\)(?=\s)"
    r"|[•●▪‣⁃\-\*–](?=\s)"
    r")",
    re.IGNORECASE,
)
SENTENCE_END = re.compile(r"[.!?](?:[\"'’”)\]]*)(?=\s+|$)")
WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)
HEADING_MAX = 80


@dataclass(frozen=True)
class Segment:
    page_number: int
    start: int
    end: int
    text: str
    # Index in reading order across the whole document.
    position: int
    # A line on its own with no sentence ending: a title or a heading.
    is_heading: bool = False

    @property
    def words(self) -> list:
        return WORD.findall(self.text)


def _trim(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _is_heading_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > HEADING_MAX:
        return False
    if stripped[-1] in ".;,":
        return False
    # A table row ("B.Tech CSE<tab>85000<tab>2500") is not a heading, nor is
    # a line of a table written as text ("Table 2, row 3: Branch: CSE").
    if "\t" in stripped or "|" in stripped or "  " in stripped:
        return False
    if ROW_PREFIX.match(stripped):
        return False
    visible = [c for c in stripped if not c.isspace()]
    if sum(1 for c in visible if c.isdigit()) > 0.3 * len(visible):
        return False
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) < 3:
        return False
    words = stripped.split()
    if len(words) > 12:
        return False
    if stripped.isupper():
        return True
    # Title Case or a numbered heading ("4.2 Attendance").
    capitalised = sum(1 for word in words if word[:1].isupper() or word[:1].isdigit())
    return capitalised >= max(1, int(len(words) * 0.6))


def _blocks(text):
    """(start, end, is_heading) runs of lines that belong together."""
    blocks = []
    current_start = None
    offset = 0
    lines = []
    for match in LINE.finditer(text):
        if not match.group():
            break
        lines.append((match.start(), match.end(), match.group()))
    for index, (start, end, line) in enumerate(lines):
        next_line = lines[index + 1][2] if index + 1 < len(lines) else ""
        stripped = line.strip()
        if not stripped:
            if current_start is not None:
                blocks.append((current_start, start, False))
                current_start = None
            continue
        heading = _is_heading_line(line) and (
            not next_line.strip()
            or LIST_ITEM.match(next_line)
            or next_line.strip()[:1].isupper()
            or next_line.strip()[:1].isdigit()
        )
        if heading:
            if current_start is not None:
                blocks.append((current_start, start, False))
                current_start = None
            blocks.append((start, end, True))
            continue
        if current_start is not None and LIST_ITEM.match(line):
            blocks.append((current_start, start, False))
            current_start = None
        if current_start is None:
            current_start = start
        offset = end
    if current_start is not None:
        blocks.append(
            (current_start, offset if offset > current_start else len(text), False)
        )
    return blocks


def _sentence_spans(text, start, end):
    """Sentence boundaries inside one block."""
    spans = []
    cursor = start
    for match in SENTENCE_END.finditer(text, start, end):
        stop = match.end()
        before = text[cursor : match.start()]
        token = before.rsplit(None, 1)[-1].lower() if before.split() else ""
        token = token.strip("([\"'")
        if token in ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
            continue
        # "4.5" or "12.03.2024" - a period between digits is not an end.
        if match.group()[0] == "." and stop < end and text[stop : stop + 1].isdigit():
            continue
        following = text[stop:end].lstrip()
        if following and not (
            following[0].isupper() or following[0].isdigit() or following[0] in "\"'(“‘"
        ):
            continue
        spans.append((cursor, stop))
        cursor = stop
    if cursor < end:
        spans.append((cursor, end))
    return spans


def segment(pages) -> list:
    """Every sentence and heading of the document, in reading order."""
    segments = []
    for page_number, text in pages:
        text = text or ""
        for block_start, block_end, is_heading in _blocks(text):
            spans = (
                [(block_start, block_end)]
                if is_heading
                else _sentence_spans(text, block_start, block_end)
            )
            for raw_start, raw_end in spans:
                start, end = _trim(text, raw_start, raw_end)
                if end <= start:
                    continue
                segments.append(
                    Segment(
                        page_number=page_number,
                        start=start,
                        end=end,
                        text=text[start:end],
                        position=len(segments),
                        is_heading=is_heading,
                    )
                )
    return segments


def normalize_term(word: str) -> str:
    """A crude, deterministic stem for counting: lower case, plural folded."""
    word = word.lower().strip("'’-")
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def content_terms(text: str) -> list:
    """Terms worth counting: words of 3+ letters that are not stopwords."""
    terms = []
    for word in WORD.findall(text):
        lowered = word.lower()
        if len(lowered) < 3 or lowered in STOPWORDS:
            continue
        terms.append(normalize_term(lowered))
    return terms


def collapse(text: str) -> str:
    return " ".join(text.split())
