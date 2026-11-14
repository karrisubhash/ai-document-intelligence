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

import re
from dataclasses import dataclass, field

import pdfplumber
import pypdf
from celery.exceptions import SoftTimeLimitExceeded
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import compose, geometry

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
    dpi: int = 300,
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

    ruled = _ruled_tables(page, metadata)
    boxes = [bbox for bbox, _ in ruled]
    upright, rotated = _words(page, boxes)

    unreadable = _garbled(upright, ruled)
    if unreadable:
        return PdfPage(needs_ocr=True, metadata={"ocr_reason": unreadable})

    if _is_scan(page, upright):
        return PdfPage(
            needs_ocr=True,
            metadata={"ocr_reason": "scanned page: a picture with no text layer"},
        )

    if region_ocr is not None:
        upright += _ocr_pictures(page, upright, region_ocr, budget, dpi, metadata)

    extras = [
        geometry.Block(
            kind="table",
            top=bbox[1],
            bottom=bbox[3],
            rows=rows,
            source=SOURCE_RULED,
        )
        for bbox, rows in ruled
    ]
    if rotated:
        extras.append(
            geometry.Block(
                kind="text",
                top=min(word["top"] for word in rotated),
                bottom=max(word["bottom"] for word in rotated),
                text=" ".join(word["text"] for word in rotated),
            )
        )

    builder = compose.PageBuilder(source=SOURCE_LAYOUT, source_version=VERSION)
    for block in geometry.layout_page(upright, extra_blocks=extras):
        if block.kind == "table":
            builder.add_table(block.rows, source=block.source or SOURCE_LAYOUT)
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


def _ruled_tables(page, metadata) -> list:
    """[(bbox, rows)] for each grid drawn with lines on this page."""
    rulings = len(page.lines) + len(page.rects) + len(page.curves)
    if rulings > MAX_RULING_OBJECTS:
        metadata["ruled_tables_skipped"] = f"{rulings} drawn lines"
        return []

    found = []
    for table in page.find_tables(table_settings=RULED_SETTINGS):
        rows = [
            ["" if cell is None else cell for cell in row]
            for row in table.extract(x_tolerance_ratio=X_TOLERANCE_RATIO)
        ]
        # A box around a paragraph, an underline, a page border: one row or
        # one column is not a table, and claiming its words would take them
        # out of the normal reading flow.
        if len(rows) < 2 or max((len(row) for row in rows), default=0) < 2:
            continue
        if not any(cell.strip() for row in rows for cell in row):
            continue
        found.append((table.bbox, rows))

    return _outermost(found)


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


def _words(page, table_boxes) -> tuple:
    """(upright geometry words, rotated word dicts), outside ruled tables."""
    raw = page.extract_words(
        x_tolerance_ratio=X_TOLERANCE_RATIO,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=False,
    )

    upright = []
    rotated = []
    for word in raw:
        if any(_inside(word, box) for box in table_boxes):
            continue
        if not word.get("upright", True):
            rotated.append(word)
            continue
        upright.append(
            geometry.Word(
                text=word["text"],
                x0=word["x0"],
                x1=word["x1"],
                top=word["top"],
                bottom=word["bottom"],
            )
        )
    return upright, rotated


def _garbled(words, ruled) -> str:
    """Why this text layer cannot be trusted, or "" when it can."""
    text = " ".join(word.text for word in words)
    text += " ".join(cell for _, rows in ruled for row in rows for cell in row)

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
