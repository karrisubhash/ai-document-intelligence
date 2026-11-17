"""PDF text extraction.

Text comes from pdfplumber, not pypdf, and the difference is the reason this
module exists. pypdf returns words in the order the file *draws* them, with
single spaces. For prose that is fine. For a fee table it is not: a blank
cell leaves no trace, so "B.Tech ECE  [blank]  2500" comes back as
"B.Tech ECE 2500" and the number silently moves into the wrong column; and a
two-column page comes back one line of the left column, one line of the
right, forever. pdfplumber returns every character with its rectangle, which
is what makes it possible to say which words share a line, which lines share
a column, and where a table's cells are.

Per page, in this order:

1. *Ruled tables* - grids drawn with lines - are read cell by cell with
   pdfplumber's own detector. A blank cell stays blank and a merged cell
   stays where its text is.
2. Every other word goes through `geometry.layout_page`: lines, page
   columns, and borderless tables found from aligned whitespace.
3. A page whose text layer is garbage (unmapped glyphs - `(cid:12)`,
   replacement characters, private-use code points) is not stored; it is
   deferred to OCR with the reason recorded, because garbage that looks like
   text would poison every search and answer built on it.
4. A large picture with no text over it - a pasted screenshot of a table, a
   scanned insert - is OCR'd through `region_ocr` when one is supplied, and
   its words join the page's own so one layout pass reads them together.

Any failure inside pdfplumber on a page falls back to pypdf for THAT page,
recorded in its metadata. Extraction is never worse than it used to be, and
one malformed page does not cost the document the rest.

pypdf stays for what it is good at: counting pages for validation and
reading the image dictionary for figure detection.
"""

import dataclasses
import math
import re
from collections import Counter
from dataclasses import dataclass, field

import pdfplumber
import pypdf
from celery.exceptions import SoftTimeLimitExceeded
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import compose, geometry, rulings, wording

EXTRACTOR = "pdfplumber"
VERSION = pdfplumber.__version__
PYPDF_VERSION = pypdf.__version__

# Recorded as DocumentTable.detector, so a ruled grid and one inferred from
# alignment can be told apart - and compared - after the fact.
SOURCE_RULED = "pdfplumber:ruled"
SOURCE_LAYOUT = "pdfplumber:layout"

# Word-splitting tolerance as a fraction of the font size, not a fixed 3
# points. A fixed 3pt is wider than a space in 8pt text, and in a PDF that
# positions words instead of writing space characters it glues them together.
X_TOLERANCE_RATIO = 0.2

RULED_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
}

# A page drawn with more rulings than this (a map, a CAD sheet) is not a
# table page, and finding tables in it is slow.
MAX_RULING_OBJECTS = 3000

# A ruled table whose rows hold this many lines in their key cell and in most
# of their other cells, in this share of its body, has rows the rulings did
# not separate - a report shaded or ruled in bands of five rows has a rule
# every fifth row, so every cell holds five rows' worth of text. Its columns
# may be right and its rows are not; it is not stored as a table, and its
# words are read by the layout pass, which finds the rows from the text.
MULTILINE_CELL_LINES = 3
# A table is distrusted when this share of the cells of its body are tall
# cells of banded rows. Counted in cells, not rows: a monthly report ruled
# every third month has a band whose every cell is a stack of three, a
# question paper with a few questions in parts has bands whose marks and
# outcomes are stacked beside a question number that is not.
MULTILINE_SHARE = 0.3
BANDED_CELL_SHARE = 0.6
# A tall cell whose lines are this share figures is a stack of separate values.
FIGURE_STACK_SHARE = 0.8
# A tall cell whose lines are items - each starting a new one - has at most
# this share of lines that start in lower case, as a wrapped sentence's do.
ITEM_CONTINUATION_SHARE = 0.2

# A page is deferred to OCR when this share of its text is unreadable.
GARBLED_SHARE = 0.2
PRIVATE_USE_SHARE = 0.3
# Below this many glyphs a share means nothing (one bad glyph in five is not a
# bad page); only "nothing readable at all" condemns a page that short.
MIN_GLYPHS_FOR_SHARE = 8
_CID = re.compile(r"\(cid:\d+\)")

# A picture is worth OCR when it covers this much of the page and almost no
# text-layer word sits on it. A scanned page that already carries an OCR text
# layer has many words over its image and is left alone.
MIN_IMAGE_PAGE_SHARE = 0.10

# A picture covering this much of the page IS the page: a scan. It is not read
# here as an insert but deferred to page-level OCR, which also turns a sideways
# page upright and records the page as OCR - not as a text layer.
SCANNED_PAGE_SHARE = 0.80
MAX_WORDS_OVER_IMAGE = 3
MIN_IMAGE_PIXELS = 60


@dataclass
class PdfPage:
    """One page, as extracted."""

    text: str = ""
    tables: list = field(default_factory=list)
    extractor: str = EXTRACTOR
    extractor_version: str = VERSION
    # True when the text layer is absent or unusable and OCR should read it.
    needs_ocr: bool = False
    metadata: dict = field(default_factory=dict)


def open_reader(file_path: str) -> PdfReader:
    try:
        return PdfReader(file_path)
    except Exception as exc:
        raise StageError(
            f"Could not read PDF: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc


def page_count(file_path: str) -> int:
    reader = open_reader(file_path)

    # pypdf reads the page tree on first use, not when the reader is built,
    # so a password-protected PDF or a broken /Pages entry fails HERE - and
    # unwrapped, pypdf's own exception reached the job as UNEXPECTED_ERROR,
    # a bug report for what is a property of the file.
    try:
        return len(reader.pages)
    except FileNotDecryptedError as exc:
        raise StageError(
            "PDF is password-protected; upload a copy without a password.",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc
    except Exception as exc:
        raise StageError(
            f"Could not read PDF: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc


def extract_pages(file_path: str) -> list[str]:
    """Text layer per page, index 0 = page 1. Blank where there is none."""
    return [page.text for page in extract_page_results(file_path)]


def extract_pages_pypdf(file_path: str) -> list[str]:
    """The previous engine, kept as a named baseline for the benchmark.

    Not used by the pipeline: it is what `extract_pages` returned before
    pdfplumber, so a comparison against it measures what changed.
    """
    reader = open_reader(file_path)
    try:
        return [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise StageError(
            f"PDF text extraction failed: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc


def extract_page_results(
    file_path: str,
    *,
    region_ocr=None,
    max_regions: int = 0,
    dpi: int = 250,
) -> list:
    """Every page as a `PdfPage`: text, tables, which extractor read it.

    `region_ocr(image) -> list[geometry.Word]` reads a picture; the words it
    returns are in the picture's own pixel space and are mapped onto the page
    here. `max_regions` caps how many pictures one document may spend on it.
    """
    try:
        document = pdfplumber.open(file_path)
    except Exception as exc:
        return _pypdf_document(file_path, f"pdfplumber could not open it: {exc}")

    results: list = []
    budget = _Budget(max_regions if region_ocr else 0)
    fallback = None

    with document:
        for index, page in enumerate(document.pages):
            try:
                results.append(_extract_page(page, region_ocr, budget, dpi))
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                if fallback is None:
                    fallback = open_reader(file_path)
                results.append(
                    _pypdf_page(fallback, index, f"pdfplumber failed: {exc}")
                )
            finally:
                # Page objects cache every character; across a 500-page file
                # that is the difference between megabytes and gigabytes.
                page.flush_cache()

    return results


# -- one page ---------------------------------------------------------------


@dataclass
class _Budget:
    remaining: int
    skipped: int = 0

    def take(self) -> bool:
        if self.remaining <= 0:
            self.skipped += 1
            return False
        self.remaining -= 1
        return True


def _extract_page(page, region_ocr, budget, dpi) -> PdfPage:
    metadata: dict = {}

    # A landscape table drawn sideways has its text running down or up the
    # sheet in the file's own coordinates. Its words are turned into the
    # text's own frame, in which it reads left to right; the rulings and
    # pictures are in the other frame and are left alone.
    votes = _directions(page)
    direction = _text_direction(votes)
    turned = direction != "right"
    if turned:
        metadata["text_direction"] = direction

    ruled = [] if turned else _ruled_tables(page, metadata)
    boxes = [bbox for bbox, _ in ruled]
    upright, odd = _words(page, boxes, direction, votes)

    unreadable = _garbled(upright, ruled)
    if unreadable:
        return PdfPage(needs_ocr=True, metadata={"ocr_reason": unreadable})

    if not turned and _is_scan(page, upright):
        return PdfPage(
            needs_ocr=True,
            metadata={"ocr_reason": "scanned page: a picture with no text layer"},
        )

    if _repair_glued(upright, ruled):
        upright = [
            dataclasses.replace(word, text=wording.split_glued(word.text))
            for word in upright
        ]
        metadata["glued_words_split"] = True

    if region_ocr is not None and not turned:
        upright += _ocr_pictures(page, upright, region_ocr, budget, dpi, metadata)

    # A title drawn as the table's first, full-width row stays its first row:
    # it is what tells two tables with the same columns apart (Semester I,
    # Semester II), so it stays inside the table's span, beside its own
    # table, and - being the first row - it is the table's header, as it was
    # before this reading of ruled tables.
    extras = []
    for bbox, grid in ruled:
        block = geometry.Block(
            kind="table",
            top=bbox[1],
            bottom=bbox[3],
            rows=grid.rows,
            source=SOURCE_RULED,
            header=True if grid.header else "auto",
        )
        extras.append(block)
    for group in odd:
        # On a turned page the odd ones out are in the other frame, where
        # this page has no place for them: they close it.
        top = float("inf") if turned else min(w["top"] for w in group)
        extras.append(
            geometry.Block(
                kind="text",
                top=top,
                bottom=top,
                text=" ".join(word["text"] for word in group),
            )
        )

    builder = compose.PageBuilder(source=SOURCE_LAYOUT, source_version=VERSION)
    for block in geometry.layout_page(upright, extra_blocks=extras):
        if block.kind == "table":
            builder.add_table(
                block.rows,
                source=block.source or SOURCE_LAYOUT,
                header=block.header,
            )
        else:
            builder.add_text(block.text)

    text, tables = builder.build()

    if budget.skipped:
        metadata["pictures_skipped"] = budget.skipped

    if not text.strip():
        return PdfPage(needs_ocr=True, metadata={"ocr_reason": "no text layer"})

    if tables:
        metadata["tables"] = len(tables)
    return PdfPage(text=text, tables=tables, metadata=metadata)


def _repair_glued(words, ruled) -> bool:
    """Whether this page draws words without spaces, and if so, split its cells.

    Decided on the page's own text, words and cells together: a page that
    shows the fault (`wording.has_glued_words`) has its long unlisted runs
    split; any other page is left exactly as drawn. The cells are repaired
    here, the caller repairs the words.
    """
    texts = [word.text for word in words]
    texts += [cell for _, grid in ruled for row in grid.rows for cell in row]
    if not wording.has_glued_words(texts):
        return False
    for _, grid in ruled:
        grid.rows = [[wording.repair_lines(cell) for cell in row] for row in grid.rows]
    return True


def _ruled_tables(page, metadata) -> list:
    """[(bbox, rulings.Grid)] for each grid drawn with lines on this page.

    Found on the page as a reader sees it (`rulings.visible_page`): shapes
    painted in the colour already behind them are not rulings.
    """
    drawn = len(page.lines) + len(page.rects) + len(page.curves)
    if drawn > MAX_RULING_OBJECTS:
        metadata["ruled_tables_skipped"] = f"{drawn} drawn lines"
        return []

    found = []
    source = rulings.visible_page(page)
    for table in source.find_tables(table_settings=RULED_SETTINGS):
        for grid in rulings.read(table, text_of=_cell_text):
            rows = grid.rows
            if not _is_table(grid):
                continue
            if not _rows_are_whole(rows):
                metadata["ruled_tables_distrusted"] = (
                    metadata.get("ruled_tables_distrusted", 0) + 1
                )
                continue
            found.append((grid.bbox, grid))

    return _outermost(found)


def _is_table(grid) -> bool:
    """Whether a ruled grid is a table rather than a box drawn around text.

    A box around a paragraph, an underline, a page border: one row or one
    column is not a table, and claiming its words would take them out of the
    normal reading flow. A titled box with one row of several cells is two
    rows, the title and that row, and is kept.
    """
    rows = grid.rows
    if len(rows) < 2 or max((len(row) for row in rows), default=0) < 2:
        return False
    return any(cell.strip() for row in rows for cell in row)


def _cell_text(chars) -> str:
    """One ruled cell's text, read in the direction its characters run.

    A row label printed sideways down the first column ("Mécanisme",
    "Kaliémie") comes back reversed when read as upright text.
    """
    votes = Counter(_char_direction(char) for char in chars)
    direction = _text_direction(votes)
    return pdfplumber.utils.extract_text(
        chars,
        x_tolerance_ratio=X_TOLERANCE_RATIO,
        y_tolerance=3,
        **_WORD_OPTIONS[direction],
    )


def _rows_are_whole(rows: list) -> bool:
    """Whether each row of this ruled table is one row of the page.

    Tall cells alone do not say. A table of wrapped descriptions has them -
    "GRANT" beside four lines of explanation and four of syntax - and is a
    table. A band of rows the rulings never separated has them too, and
    something a wrapped description never has: a cell holding a stack of
    separate figures, one per line, beside other tall cells. A table whose
    body is enough such cells is not stored as a table.
    """
    banded = counted = 0
    for row in rows[1:]:
        filled = [cell.strip() for cell in row if cell.strip()]
        counted += len(filled)
        if len(filled) >= 2 and _is_banded(filled):
            banded += sum(
                1 for cell in filled if cell.count("\n") + 1 >= MULTILINE_CELL_LINES
            )
    return not counted or banded < MULTILINE_SHARE * counted


def _is_item_stack(lines: list) -> bool:
    """Whether the lines of a cell are separate items, not a wrapped sentence.

    A list of names, designations or sections has a capital (or a digit) at
    the start of every line. A description wrapped over four lines starts
    most of them in lower case.
    """
    starts = [line.lstrip()[:1] for line in lines[1:] if line.strip()]
    if not starts:
        return False
    lower = sum(1 for start in starts if start.islower())
    return lower <= ITEM_CONTINUATION_SHARE * len(starts)


def _is_banded(filled: list) -> bool:
    """Whether one ruled row holds several rows of the page.

    Two or more of its filled cells are tall (MULTILINE_CELL_LINES lines or
    more), they are most of its filled cells, and they are stacks of separate
    things: one is a stack of figures, or all of them are lists of items about
    as long as one another (a faculty list ruled only above and below). The
    tall cells need not be equally tall: in a band one name wraps or one
    amount is blank, and the stacks no longer line up. A wrapped description
    beside a short key is not a stack: it is a row.
    """
    stacks = [cell.split("\n") for cell in filled]
    tall = [lines for lines in stacks if len(lines) >= MULTILINE_CELL_LINES]
    if len(tall) < 2 or len(tall) < BANDED_CELL_SHARE * len(filled):
        return False
    if any(
        sum(1 for line in lines if geometry.is_number(line))
        >= FIGURE_STACK_SHARE * len(lines)
        for lines in tall
    ):
        return True
    heights = [len(lines) for lines in tall]
    return max(heights) - min(heights) <= 1 and all(map(_is_item_stack, tall))


def _outermost(tables: list) -> list:
    """Drop a table whose box lies inside, or overlaps, an earlier one."""
    kept: list = []
    for bbox, rows in sorted(tables, key=lambda item: (item[0][1], item[0][0])):
        if any(_overlap(bbox, other) for other, _ in kept):
            continue
        kept.append((bbox, rows))
    return kept


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _inside(word: dict, box) -> bool:
    x = (word["x0"] + word["x1"]) / 2
    y = (word["top"] + word["bottom"]) / 2
    return box[0] - 1 <= x <= box[2] + 1 and box[1] - 1 <= y <= box[3] + 1


# How far from an exact quarter turn a character may be drawn and still count as
# running that way; between the margins it is a diagonal annotation.
ANGLE_MARGIN = 20

# How pdfplumber should assemble the characters of text drawn in each direction
# into words. Without it, a vertical label comes out one letter at a time.
_WORD_OPTIONS = {
    "right": {},
    "down": {"char_dir_rotated": "ttb", "line_dir_rotated": "rtl"},
    "up": {"char_dir_rotated": "btt", "line_dir_rotated": "ltr"},
    # Upside down. pdfplumber counts it as upright (nothing is turned a
    # quarter), so it is the plain direction options that apply.
    "left": {"char_dir": "rtl", "line_dir": "btt"},
}


def _char_direction(char: dict) -> str:
    """The direction a character was drawn in.

    right, up, down or left (upside down: text that reads right to left),
    each within a margin of the exact angle; anything between is angled - a
    diagonal annotation. Read from its text matrix, which carries the
    rotation it was drawn with whatever /Rotate says.
    """
    a, b = char["matrix"][0], char["matrix"][1]
    angle = math.degrees(math.atan2(b, a))
    if abs(angle) <= ANGLE_MARGIN:
        return "right"
    if abs(angle - 90) <= ANGLE_MARGIN:
        return "up"
    if abs(angle + 90) <= ANGLE_MARGIN:
        return "down"
    if abs(angle) >= 180 - ANGLE_MARGIN:
        return "left"
    return "angled"


def _directions(page) -> Counter:
    return Counter(_char_direction(char) for char in page.chars)


def _text_direction(votes: Counter) -> str:
    """Which way most of the page's text runs, given each direction's count.

    A page of mostly diagonal text has no direction to turn it by, so it is
    read as it stands.
    """
    if not votes:
        return "right"
    direction, count = votes.most_common(1)[0]
    if direction == "angled" or count <= 0.5 * sum(votes.values()):
        return "right"
    return direction


def _turn(word: dict, direction: str, width: float, height: float) -> tuple:
    """A word's (x0, x1, top, bottom) in the frame its text reads left to right in.

    Text running down the sheet reads left to right once the sheet is turned
    a quarter anticlockwise, which also makes its lines advance down the
    page; text running up needs a quarter turn clockwise, and text upside
    down a half turn. Applied to the word's box, not its characters - the
    words were already formed along the text's own direction.
    """
    if direction == "left":
        return (
            width - word["x1"],
            width - word["x0"],
            height - word["bottom"],
            height - word["top"],
        )
    if direction == "down":
        return (
            word["top"],
            word["bottom"],
            width - word["x1"],
            width - word["x0"],
        )
    return (
        height - word["bottom"],
        height - word["top"],
        word["x0"],
        word["x1"],
    )


def _angled_words(page) -> list:
    """Diagonal text, read in the order it was written.

    No axis runs along it, so it cannot be assembled by position - but the
    PDF stores characters in the order the author typed them. A run ends at
    a space, a change of angle, or a jump of more than a few character
    widths.
    """
    words: list = []
    run: list = []

    def close():
        if run:
            words.append(
                {
                    "text": "".join(c["text"] for c in run),
                    "x0": min(c["x0"] for c in run),
                    "x1": max(c["x1"] for c in run),
                    "top": min(c["top"] for c in run),
                    "bottom": max(c["bottom"] for c in run),
                }
            )
            run.clear()

    previous = None
    for char in page.chars:
        if _char_direction(char) != "angled" or char["text"].isspace():
            close()
            previous = None
            continue
        if previous is not None:
            same = char["matrix"][:2] == previous["matrix"][:2]
            near = abs(char["x0"] - previous["x0"]) <= 2.5 * char["size"]
            if not (same and near):
                close()
        run.append(char)
        previous = char
    close()
    return words


def _drawn(page, which: str, votes: Counter) -> list:
    """The words made of the characters drawn in direction `which`."""
    if which == "angled":
        return _angled_words(page)
    options = {
        "x_tolerance_ratio": X_TOLERANCE_RATIO,
        "y_tolerance": 3,
        "keep_blank_chars": False,
        "use_text_flow": False,
        **_WORD_OPTIONS[which],
    }
    source = page
    if len(votes) > 1:
        source = page.filter(
            lambda obj: (
                obj.get("object_type") == "char" and _char_direction(obj) == which
            )
        )
    return source.extract_words(**options)


def _words(page, table_boxes, direction: str, votes: Counter) -> tuple:
    """(geometry words, [groups of odd-one-out word dicts]), outside ruled tables.

    The odd ones out are words drawn in a different direction from the rest of
    the page - a vertical margin stamp on an upright page, an upright label on
    a page whose text was turned - each group read along its own direction.
    """
    turned = direction != "right"

    def outside(words):
        return [w for w in words if not any(_inside(w, box) for box in table_boxes)]

    upright = []
    for word in outside(_drawn(page, direction, votes)):
        if turned:
            x0, x1, top, bottom = _turn(word, direction, page.width, page.height)
        else:
            x0, x1, top, bottom = word["x0"], word["x1"], word["top"], word["bottom"]
        upright.append(
            geometry.Word(text=word["text"], x0=x0, x1=x1, top=top, bottom=bottom)
        )

    odd = []
    for which in votes:
        if which != direction:
            group = outside(_drawn(page, which, votes))
            if group:
                odd.append(group)
    return upright, odd


def _garbled(words, ruled) -> str:
    """Why this text layer cannot be trusted, or "" when it can."""
    text = " ".join(word.text for word in words)
    text += " ".join(cell for _, grid in ruled for row in grid.rows for cell in row)

    # One unmapped glyph is written "(cid:12)" - seven characters standing
    # for one - so each is counted as the single glyph it is, or a lone bad
    # character in a short line would read as a mostly-garbage page.
    unmapped = len(_CID.findall(text))
    glyphs = [char for char in _CID.sub("?", text) if not char.isspace()]
    size = len(glyphs)
    if not size:
        return ""

    replaced = glyphs.count("\ufffd")
    private = sum(1 for char in glyphs if "\ue000" <= char <= "\uf8ff")
    reasons = (
        (unmapped, "unmapped glyphs in the text layer"),
        (replaced, "replacement characters in the text layer"),
        (private, "private-use glyphs in the text layer"),
    )
    worst, reason = max(reasons, key=lambda item: item[0])

    # Nothing readable left at any size: a page whose only "text" is four
    # (cid:0) is blank, not a four-glyph page.
    if unmapped + replaced + private >= size:
        return reason
    if size < MIN_GLYPHS_FOR_SHARE:
        return ""

    if unmapped > GARBLED_SHARE * size:
        return reasons[0][1]
    if replaced > GARBLED_SHARE * size:
        return reasons[1][1]
    if private > PRIVATE_USE_SHARE * size:
        return reasons[2][1]
    return ""


# -- pictures ---------------------------------------------------------------


def _is_scan(page, words) -> bool:
    """A page that is one picture with (almost) no text over it."""
    area = float(page.width * page.height)
    for picture in _pictures(page, area):
        covered = (picture["x1"] - picture["x0"]) * (picture["bottom"] - picture["top"])
        if covered < SCANNED_PAGE_SHARE * area:
            continue
        box = (picture["x0"], picture["top"], picture["x1"], picture["bottom"])
        if _words_over(words, box) <= MAX_WORDS_OVER_IMAGE:
            return True
    return False


def _words_over(words, box) -> int:
    return sum(
        1
        for word in words
        if box[0] <= (word.x0 + word.x1) / 2 <= box[2]
        and box[1] <= (word.top + word.bottom) / 2 <= box[3]
    )


def _ocr_pictures(page, words, region_ocr, budget, dpi, metadata) -> list:
    """Words read out of large pictures that have no text over them."""
    page_area = float(page.width * page.height)
    read = []
    done = 0

    for picture in _pictures(page, page_area):
        box = (picture["x0"], picture["top"], picture["x1"], picture["bottom"])
        if _words_over(words, box) > MAX_WORDS_OVER_IMAGE:
            continue
        if not budget.take():
            continue

        image = page.crop(_clip(box, page)).to_image(resolution=dpi).original
        scale_x = (box[2] - box[0]) / image.width
        scale_y = (box[3] - box[1]) / image.height

        for word in region_ocr(image):
            read.append(
                geometry.Word(
                    text=word.text,
                    x0=box[0] + word.x0 * scale_x,
                    x1=box[0] + word.x1 * scale_x,
                    top=box[1] + word.top * scale_y,
                    bottom=box[1] + word.bottom * scale_y,
                )
            )
        done += 1

    if done:
        metadata["pictures_ocred"] = done
    return read


def _pictures(page, page_area: float) -> list:
    """Pictures big enough to carry content, largest first, not overlapping."""
    candidates = []
    for picture in page.images:
        width = picture["x1"] - picture["x0"]
        height = picture["bottom"] - picture["top"]
        source = picture.get("srcsize") or (MIN_IMAGE_PIXELS, MIN_IMAGE_PIXELS)
        if min(source) < MIN_IMAGE_PIXELS:
            continue
        if width * height < MIN_IMAGE_PAGE_SHARE * page_area:
            continue
        candidates.append(picture)

    candidates.sort(
        key=lambda p: (p["x1"] - p["x0"]) * (p["bottom"] - p["top"]), reverse=True
    )
    kept: list = []
    for picture in candidates:
        box = (picture["x0"], picture["top"], picture["x1"], picture["bottom"])
        if any(_overlap(box, (k["x0"], k["top"], k["x1"], k["bottom"])) for k in kept):
            continue
        kept.append(picture)
    return kept


def _clip(box, page):
    return (
        max(box[0], page.bbox[0]),
        max(box[1], page.bbox[1]),
        min(box[2], page.bbox[2]),
        min(box[3], page.bbox[3]),
    )


# -- the fallback -----------------------------------------------------------


def _pypdf_page(reader: PdfReader, index: int, reason: str) -> PdfPage:
    try:
        raw = reader.pages[index].extract_text() or ""
    except Exception as exc:
        raise StageError(
            f"PDF text extraction failed: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc

    builder = compose.PageBuilder()
    builder.add_text(raw)
    text, _ = builder.build()
    return PdfPage(
        text=text,
        extractor="pypdf",
        extractor_version=PYPDF_VERSION,
        needs_ocr=not text.strip(),
        metadata={"fallback_reason": reason},
    )


def _pypdf_document(file_path: str, reason: str) -> list:
    reader = open_reader(file_path)
    try:
        count = len(reader.pages)
    except Exception as exc:
        raise StageError(
            f"PDF text extraction failed: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc
    return [_pypdf_page(reader, index, reason) for index in range(count)]
