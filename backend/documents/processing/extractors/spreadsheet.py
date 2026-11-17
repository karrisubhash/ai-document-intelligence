"""Spreadsheets - XLSX and CSV/TSV - read as the tables they are.

A spreadsheet is the one input whose structure is not inferred: every value
sits in a cell with an address. So there is nothing to detect here; the job is
to write the grid down faithfully and to bound the work, because a sheet is
also the one input whose size is not visible from the file's.

*One page per sheet.* A workbook has no pages, but its sheets are its own
natural unit - a fee sheet, a timetable sheet - and a citation that says
"Sheet: Fees" means something. A sheet over `ROWS_PER_PAGE` rows is split, with
the header row repeated on each part, so a row quoted from page three still
carries its column names.

*Blank rows separate tables.* People put a title, then a table, then a gap,
then another table on one sheet. Each run of non-blank rows is written as its
own table; a run with one row or one column is just text.

*Values are written the way a reader sees them, not the way they are stored.*
Excel stores a date as a number and 0.05 as the percentage 5%; floating point
turns 0.1 + 0.2 into 0.30000000000000004. Dates come out as ISO dates,
percent formats as percentages, and floats lose the binary noise.

*Work is bounded three ways*, because a "used range" can be declared as the
whole million-row grid of a sheet that holds ten cells: a blank-row run ends
the sheet, columns are capped, and the total of filled cells across the file
is capped. Hitting the cap is recorded in metadata AND in the last page's
text - a reader of an answer should know the file had more than was read.

Formulas are read as the value Excel last calculated and stored. A workbook
written by a library that never calculated them has no stored value, and its
formula cells read blank.
"""

import codecs
import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import compose, ooxml
from documents.processing.tables import detector

ROWS_PER_PAGE = 500

# Filled cells read across the whole file before it is cut off.
MAX_CELLS = 200_000

# A sheet is over after this many blank rows in a row: trailing formatting.
MAX_BLANK_RUN = 1000

# Wider than this is not a table a person laid out.
MAX_COLUMNS = 200

SAMPLE_BYTES = 64 * 1024
SAMPLE_LINES = 25
DELIMITERS = (",", ";", "\t", "|")

SOURCE_XLSX = "openpyxl:sheet"
SOURCE_CSV = "csv:sheet"

# One CSV field this large is already a corrupt file; the default (128 KB)
# rejects a legitimate cell of pasted prose.
csv.field_size_limit(10 * 1024 * 1024)


@dataclass
class SheetPage:
    text: str = ""
    tables: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class _Budget:
    def __init__(self):
        self.cells = 0
        self.truncated = False

    def spend(self, filled: int) -> bool:
        """Count filled cells; False once the file is over its cap."""
        self.cells += filled
        if self.cells > MAX_CELLS:
            self.truncated = True
        return not self.truncated


# -- values -----------------------------------------------------------------


def _number(value: float) -> str:
    if value != value or value in (float("inf"), float("-inf")):
        return str(value)
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    text = repr(round(value, 10))
    if "e" in text or "E" in text:
        text = format(Decimal(text), "f").rstrip("0").rstrip(".") or "0"
    return text


def _percent(value, number_format: str) -> str:
    match = re.search(r"\.(0+)", number_format)
    places = len(match.group(1)) if match else 0
    return f"{value * 100:.{places}f}%"


def _timedelta(value: dt.timedelta) -> str:
    seconds = int(value.total_seconds())
    hours, remainder = divmod(abs(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    sign = "-" if value.total_seconds() < 0 else ""
    return f"{sign}{hours}:{minutes:02d}:{seconds:02d}"


def cell_text(value, number_format: str = "") -> str:
    """A cell value as a person reads it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0):
            return value.date().isoformat()
        return value.isoformat(
            sep=" ", timespec="seconds" if value.second else "minutes"
        )
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S" if value.second else "%H:%M")
    if isinstance(value, dt.timedelta):
        return _timedelta(value)
    if isinstance(value, int | float):
        if "%" in (number_format or ""):
            return _percent(value, number_format)
        return _number(value) if isinstance(value, float) else str(value)
    return str(value).strip()


# -- grids to pages ---------------------------------------------------------


def _blocks(rows: list) -> list:
    """Runs of non-blank rows, as lists of rows; blank rows (empty or all
    empty cells) separate them."""
    blocks: list = []
    current: list = []
    for row in rows:
        if any(cell for cell in row):
            current.append(row)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _header_choice(block: list):
    """True when the first row is, beyond doubt, the column names.

    "auto" is the narrow rule every other source uses: a non-numeric first row
    over rows that carry numbers. A sheet of text columns has none to find, and
    would be left with no header at all, so a first row of three or more cells,
    every one filled, none a number and no two alike is taken as the header.
    Two columns stay "auto": "Name | Asha Rao" is a label and its value as
    easily as a heading.
    """
    first = block[0]
    if len(block) < 3 or len(first) < 3:
        return "auto"
    if not all(first) or any(detector.NUMERIC.match(cell) for cell in first):
        return "auto"
    if len({cell.casefold() for cell in first}) != len(first):
        return "auto"
    return True


def build_pages(
    rows: list,
    *,
    name: str | None,
    source: str,
    source_version: str,
    metadata: dict,
    header_row: bool = False,
) -> list:
    """One sheet's rows as pages: tables as structure, the sheet named.

    `header_row`: the first row is the column names, known rather than
    guessed - a list of records whose keys they are.
    """
    paged = len(rows) > ROWS_PER_PAGE
    pages: list = []
    # Whether the previous page ended inside a table: its last block was
    # stored as a table and ran to the page's last row.
    open_table = False

    for start in range(0, max(len(rows), 1), ROWS_PER_PAGE):
        window = rows[start : start + ROWS_PER_PAGE]
        builder = compose.PageBuilder(source=source, source_version=source_version)

        label = f"Sheet: {name}" if name else ""
        if paged:
            span = f"sheet rows {start + 1}-{start + len(window)}"
            label = f"{label} ({span})" if label else f"({span})"
        if label:
            builder.add_text(label)

        blocks = _blocks(window)

        # A table that runs on from the previous page keeps its number, its
        # labels and its row count (LAYOUT_ANALYSIS carries them over), so
        # its rows here read like its rows there. It runs on only if the
        # previous page ended inside it AND this page starts with it: a blank
        # first row here, or a note that ended the page before, starts anew.
        starts_here = bool(window) and any(window[0])
        stored = False
        for position, block in enumerate(blocks):
            joined = position == 0 and open_table and starts_here
            if joined:
                # Its first row is data, whatever it looks like.
                stored = builder.add_table(block, header=False, continues=True)
            elif header_row and start == 0 and position == 0:
                stored = builder.add_table(block, header=True, header_declared=True)
            else:
                choice = _header_choice(block)
                # A sheet's first row taken for its header is a guess, and is
                # checked as one.
                stored = builder.add_table(block, header=choice, header_declared=False)

        open_table = bool(blocks) and stored and bool(window) and any(window[-1])
        pages.append(builder)

    out = []
    for builder in pages:
        text, tables = builder.build()
        out.append(SheetPage(text=text, tables=tables, metadata=dict(metadata)))
    return out


# -- XLSX -------------------------------------------------------------------


def _read_sheet(sheet, budget: _Budget) -> list:
    """Rows of cell text, blank rows as [], trailing blanks gone."""
    rows: list = []
    blank_run = 0
    columns = min(sheet.max_column or 0, MAX_COLUMNS) or None

    for cells in sheet.iter_rows(max_col=columns):
        values = [
            cell_text(cell.value, getattr(cell, "number_format", "") or "")
            for cell in cells
        ]
        filled = sum(1 for value in values if value)

        if not filled:
            blank_run += 1
            if blank_run > MAX_BLANK_RUN:
                break
            rows.append([])
            continue

        blank_run = 0
        rows.append(values)
        if not budget.spend(filled):
            break

    while rows and not any(rows[-1]):
        rows.pop()
    while rows and not any(rows[0]):
        rows.pop(0)
    return rows


def extract_xlsx(file_path: str) -> tuple:
    """(pages, metadata) for a workbook: a page per visible sheet."""
    expansion = ooxml.check_expansion(file_path, "XLSX")

    # Opened as a file object, not a path: openpyxl checks a PATH's extension
    # and refuses anything that is not .xlsx before it looks inside, but the
    # type was decided by the bytes. A workbook uploaded as "report.docx" is
    # still a workbook. The handle must outlive the read-only workbook, which
    # reads lazily from it; it is closed with the workbook below.
    handle = None
    try:
        import openpyxl

        handle = open(file_path, "rb")
        workbook = openpyxl.load_workbook(handle, read_only=True, data_only=True)
    except StageError:
        if handle:
            handle.close()
        raise
    except Exception as exc:
        if handle:
            handle.close()
        raise StageError(
            f"Could not read XLSX: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc

    version = getattr(openpyxl, "__version__", "")
    budget = _Budget()
    pages: list = []
    hidden = 0
    sheets = 0
    total_rows = 0

    try:
        for sheet in workbook.worksheets:
            if sheet.sheet_state != "visible":
                hidden += 1
                continue
            if budget.truncated:
                break

            rows = _read_sheet(sheet, budget)
            if not rows:
                continue

            sheets += 1
            total_rows += len(rows)
            pages += build_pages(
                rows,
                name=sheet.title,
                source=SOURCE_XLSX,
                source_version=version,
                metadata={"sheet": sheet.title, "extractor": "openpyxl"},
            )
    except StageError:
        raise
    except Exception as exc:
        raise StageError(
            f"XLSX text extraction failed: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc
    finally:
        workbook.close()
        handle.close()

    if budget.truncated and pages:
        pages[-1] = _with_truncation_note(pages[-1])

    return pages, {
        "extractor": "openpyxl",
        "extractor_version": version,
        "sheets": sheets,
        "sheets_hidden": hidden,
        "rows": total_rows,
        "cells": budget.cells,
        "truncated": budget.truncated,
        **expansion,
    }


def _with_truncation_note(page: SheetPage) -> SheetPage:
    note = (
        f"(Not read: the file has more than {MAX_CELLS} filled cells; "
        f"the rest is omitted.)"
    )
    return SheetPage(
        text=f"{page.text}\n\n{note}" if page.text else note,
        tables=page.tables,
        metadata=page.metadata,
    )


# -- CSV / TSV --------------------------------------------------------------


def _encoding(sample: bytes) -> str:
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"

    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        # final=False: the sample may end half way through a multi-byte
        # character, which is not an encoding error.
        decoder.decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        # Excel's "CSV" on Windows. Every byte decodes; the few it leaves
        # undefined are replaced rather than failing the whole file.
        return "cp1252"


def _delimiter(sample_text: str, preferred: str) -> str:
    """The delimiter that splits the first lines into the same count of cells.

    Chosen by how consistently each candidate splits, not by csv.Sniffer,
    which guesses from character statistics and takes a comma inside a
    sentence for structure. A candidate needs two or more cells on most lines.
    """
    lines = [line for line in sample_text.splitlines() if line.strip()][:SAMPLE_LINES]
    if not lines:
        return preferred

    best, best_score = preferred, 0.0
    for delimiter in (preferred, *[d for d in DELIMITERS if d != preferred]):
        try:
            counts = [
                len(next(csv.reader([line], delimiter=delimiter))) for line in lines
            ]
        except csv.Error:
            continue
        common = max(set(counts), key=counts.count)
        if common < 2:
            continue
        consistency = counts.count(common) / len(counts)
        score = consistency * min(common, 10)
        # The preferred delimiter wins ties: a .tsv is tab-separated unless
        # its content clearly says otherwise.
        if score > best_score + 1e-9:
            best, best_score = delimiter, score
    return best


def extract_delimited(file_path: str, preferred: str = ",") -> tuple:
    """(pages, metadata) for a CSV or TSV: one table, paged when long."""
    try:
        with open(file_path, "rb") as handle:
            sample = handle.read(SAMPLE_BYTES)
    except OSError as exc:
        raise StageError(
            f"Could not read the file: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc

    encoding = _encoding(sample)
    delimiter = _delimiter(sample.decode(encoding, errors="replace"), preferred)
    budget = _Budget()
    rows: list = []
    blank_run = 0

    try:
        with open(file_path, "rb") as raw:
            text = io.TextIOWrapper(
                raw, encoding=encoding, errors="replace", newline=""
            )
            for record in csv.reader(text, delimiter=delimiter):
                values = [cell_text(cell) for cell in record[:MAX_COLUMNS]]
                filled = sum(1 for value in values if value)
                if not filled:
                    blank_run += 1
                    if blank_run > MAX_BLANK_RUN:
                        break
                    rows.append([])
                    continue
                blank_run = 0
                rows.append(values)
                if not budget.spend(filled):
                    break
    except csv.Error as exc:
        raise StageError(
            f"Could not parse the file as delimited text: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc
    except OSError as exc:
        raise StageError(
            f"Could not read the file: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc

    while rows and not any(rows[-1]):
        rows.pop()

    pages = (
        build_pages(
            rows,
            name=None,
            source=SOURCE_CSV,
            source_version="",
            metadata={"extractor": "csv"},
        )
        if rows
        else []
    )
    if budget.truncated and pages:
        pages[-1] = _with_truncation_note(pages[-1])

    return pages, {
        "extractor": "csv",
        "extractor_version": "",
        "rows": len(rows),
        "cells": budget.cells,
        "delimiter": {"\t": "tab"}.get(delimiter, delimiter),
        "encoding": encoding,
        "truncated": budget.truncated,
    }
