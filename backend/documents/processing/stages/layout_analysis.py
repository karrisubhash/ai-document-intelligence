"""LAYOUT_ANALYSIS: the structure plain text loses (§11.3, §25 phase 12).

Flattening a document to text throws away the one thing that makes a fee
table or a grade scale answerable: which number belongs to which label.
"85000" on its own is not an answer to "what is the tuition for CSE" - the
row and the column heading are what make it one.

It also throws away everything that was never text in the first place. A
bar chart with no axis labels survives extraction as nothing at all, and
the document quietly loses whatever it was showing. Figures are recorded
here for that reason - not to read them, which needs a vision model this
project does not ship, but so that an answer can point at one.

A table is not kept as a grid. Once its cells are verified against the page,
its span is rewritten as labelled text rows - one line per row, every value
beside its column's name, opening with the table's number in the document
and the row's number in it ("Table 2, row 1: Course Code: 23CS101; ...").
The text is the table: a chunk of it, a quote from it and a citation of it
each carry their own labels and reference (see tables/text_rows.py).

This stage runs in the CONTENT phase, not the analysis phase, because
structure is part of the normalized representation of the document rather
than a conclusion drawn from it. It therefore has no database access: it
rewrites the pages' text and fills `context.figures`, and the pipeline writes
them in one transaction.

The two halves are independent. Either detector can be unconfigured or
unavailable without costing the document the other, and the stage is only
SKIPPED when neither ran.
"""

from dataclasses import dataclass, replace

from documents.processing.context import StageResult
from documents.processing.figures.exceptions import (
    FigureDetectionError,
    FigureDetectorNotConfigured,
    FigureDetectorUnavailable,
)
from documents.processing.figures.service import get_detector as get_figure_detector
from documents.processing.normalize import strip_nul
from documents.processing.tables import text_rows
from documents.processing.tables.detector import DetectedTable
from documents.processing.tables.detector import verify as verify_table
from documents.processing.tables.exceptions import (
    TableDetectorNotConfigured,
    TableDetectorUnavailable,
)
from documents.processing.tables.service import get_detector as get_table_detector


def run(context) -> StageResult:
    tables_note, table_metadata = _run_tables(context)
    figures_note, figure_metadata = _run_figures(context)

    metadata = {**table_metadata, **figure_metadata}

    if not context.tables and not context.figures:
        # Nothing found is not the same as nothing attempted. When both
        # detectors are switched off the stage is SKIPPED; when they ran
        # and the document simply has no structure, it succeeded.
        skipped = table_metadata.get("skipped") and figure_metadata.get("skipped")
        return StageResult(
            message="; ".join(note for note in (tables_note, figures_note) if note),
            skipped=bool(skipped),
            metadata=metadata,
        )

    return StageResult(
        message="; ".join(note for note in (tables_note, figures_note) if note),
        metadata=metadata,
    )


def _run_tables(context):
    # Tables an extractor read as structure come first. They are verified
    # exactly as a detected table is - the span must re-slice out of the
    # page and every cell must be inside it - but they do not depend on the
    # text detector being configured, and they keep their empty cells.
    found, rejected = _structured_tables(context)

    detector = None
    unavailable = None
    try:
        detector = get_table_detector()
    except TableDetectorNotConfigured as exc:
        unavailable = (str(exc), {"skipped": True})
    except TableDetectorUnavailable as exc:
        # Transient by taxonomy, but a missing detector must not cost the
        # document its text. Recorded with a reason so the run is visible
        # in ProcessingEvent rather than silently empty.
        unavailable = (
            f"Table detection unavailable: {exc}",
            {"skipped": True, "tables_unavailable": True},
        )

    if detector is None and not found and not rejected:
        # Nothing ran and nothing was found: the stage may be SKIPPED.
        return unavailable

    metadata = {}

    if detector is not None:
        taken = {}
        for table in found:
            taken.setdefault(table.page_number, []).append(
                (table.character_start, table.character_end)
            )

        for page in context.pages:
            # The SAME normalization the pipeline applies before storing, so
            # the offsets recorded here index the text that actually lands in
            # PageContent. See processing/normalize.py.
            text = strip_nul(page.text)
            if not text.strip():
                continue

            for table in detector.detect(page.page_number, text):
                # The extractor already read this region as cells. Its
                # reading wins: it saw the document's own structure, where
                # this saw only whitespace.
                if _overlaps(table, taken.get(page.page_number, ())):
                    continue

                # Verified per table, on every run. A detector that returns a
                # row the page does not contain gets that row dropped here -
                # fabrication is refused at the boundary rather than trusted
                # not to happen.
                if not detector.verify(table, text):
                    rejected += 1
                    continue
                found.append(table)

        metadata["table_detector"] = detector.name
        metadata["table_detector_version"] = detector.version
    else:
        metadata.update(unavailable[1])
        metadata.pop("skipped", None)

    # Numbered across the document in reading order, then written into the
    # page text as labelled rows. The cells are the verified ones; the grid
    # itself is not kept.
    found.sort(key=lambda table: (table.page_number, table.character_start))
    context.tables = _write_as_text(context, found)

    metadata["tables"] = len({table.number for table in context.tables})
    metadata["tables_rejected"] = rejected
    if context.tables:
        metadata["table_rows"] = sum(table.rows for table in context.tables)
        unlabelled = {t.number for t in context.tables if not t.labelled}
        if unlabelled:
            metadata["tables_without_labels"] = len(unlabelled)
    sources = sorted({table.source for table in found if table.source})
    if sources:
        metadata["table_sources"] = sources

    # The rejection count is reported whether or not anything survived.
    # An earlier version returned a bare "No tables found." when every
    # detected table failed verification, which reads as "this document has
    # no tables" when what actually happened is "a detector produced rows
    # that are not on the page and they were refused" - the one event here
    # worth escalating.
    if not context.tables:
        note = "No tables found."
        if rejected:
            note = (
                f"No tables written: rejected {rejected} that did not verify "
                f"against the page."
            )
        return note, metadata

    pages_with_tables = len({table.page_number for table in context.tables})
    note = (
        f"Wrote {metadata['tables']} table(s) across {pages_with_tables} page(s) "
        f"as {metadata['table_rows']} labelled text row(s)."
    )
    if rejected:
        note += f" Rejected {rejected} that did not verify against the page."

    metadata["pages_with_tables"] = pages_with_tables
    return note, metadata


def _structured_tables(context):
    """The tables extractors recorded, verified, as DetectedTables."""
    found = []
    rejected = 0

    for page in context.pages:
        if not page.tables:
            continue

        text = strip_nul(page.text)
        for recorded in page.tables:
            table = DetectedTable(
                page_number=page.page_number,
                table_index=0,
                character_start=recorded.character_start,
                character_end=recorded.character_end,
                rows=[list(row) for row in recorded.rows],
                # Written as " | " rows by PageBuilder, whatever the source.
                separator="PIPE",
                header=list(recorded.header),
                source=recorded.source,
                source_version=recorded.source_version,
                header_declared=getattr(recorded, "header_declared", False),
                continues=getattr(recorded, "continues", False),
            )
            if not verify_table(table, text):
                rejected += 1
                continue
            found.append(table)

    return found, rejected


READ_BY_MODEL = "Read by a vision model; the reading is part of the page text."


def _mark_described(context, figures) -> list:
    """Figures on a page a vision model read are recorded as read.

    The reading itself is in the page text, where search finds it; the row
    only says so, which is what keeps chat from telling a reader there is a
    figure here that nobody has read. A figure on any other page stays
    undescribed, exactly as before.
    """
    describer = context.figure_describer
    sources = sorted(describer.sources) if describer is not None else []
    if not sources:
        return figures

    read_pages = {
        page.page_number
        for page in context.pages
        if page.metadata.get("figures_described")
        or page.metadata.get("figure_described")
    }
    # An Office file has no pages a figure could be pinned to; its pictures
    # are not recorded at all (see the figure detector).
    source = ", ".join(sources)[:100]
    return [
        replace(figure, description=READ_BY_MODEL, description_source=source)
        if figure.page_number in read_pages
        else figure
        for figure in figures
    ]


def _overlaps(table, spans) -> bool:
    return any(
        table.character_start < end and start < table.character_end
        for start, end in spans
    )


def _run_figures(context):
    try:
        detector = get_figure_detector()
    except FigureDetectorNotConfigured as exc:
        return str(exc), {"skipped": True}
    except FigureDetectorUnavailable as exc:
        return f"Figure detection unavailable: {exc}", {
            "skipped": True,
            "figures_unavailable": True,
        }

    try:
        figures = detector.detect(context.file_path, context.file_type, context.pages)
    except FigureDetectionError as exc:
        # A figure is a nice-to-have; the text is the document. Failing the
        # whole job here would lose a perfectly good extraction over an
        # image dictionary nobody asked about.
        return f"Figure detection failed: {exc}", {"figures_failed": True}

    figures = _mark_described(context, figures)
    context.figures = figures

    metadata = {
        "figures": len(figures),
        "figure_detector": detector.name,
        "figure_detector_version": detector.version,
    }

    if not figures:
        return "No figures found.", metadata

    pages_with_figures = sorted({figure.page_number for figure in figures})
    metadata["pages_with_figures"] = pages_with_figures
    return (
        f"Found {len(figures)} figure(s) on page(s) "
        f"{', '.join(str(page) for page in pages_with_figures)}."
    ), metadata


@dataclass(frozen=True)
class WrittenTable:
    """Where one table's rows landed in its page's text, as stored."""

    page_number: int
    number: int
    character_start: int
    character_end: int
    first_row: int
    last_row: int
    labelled: bool
    source: str = ""

    @property
    def rows(self) -> int:
        return self.last_row - self.first_row + 1


def _from_layout(source: str) -> bool:
    """A table found by where its words sit rather than by ruling or markup:
    the text detector's (no source), a borderless PDF table, an OCR'd one."""
    return not source or source.endswith(":layout")


def _write_as_text(context, tables) -> list:
    """Replace every table's span in its page with its labelled rows.

    `tables` are verified and in reading order. Table numbers run across the
    document; a table that continues the previous page's (a long sheet) keeps
    its number and goes on counting rows. Each page's text is rebuilt once,
    so the spans returned index the text as it will be stored, and the page
    no longer carries cells of its own.
    """
    by_page = {}
    for table in tables:
        by_page.setdefault(table.page_number, []).append(table)

    written_tables = []
    number = 0
    rows_in = {}

    for page in context.pages:
        mine = by_page.get(page.page_number)
        page.tables = []
        if not mine:
            continue

        text = strip_nul(page.text)
        pieces = []
        length = 0
        cursor = 0
        for table in mine:
            if table.character_start < cursor:
                # Overlaps one already written; verification makes this
                # unreachable, and the text is left as it was if it happens.
                continue
            if table.continues and number:
                table_number, first_row = number, rows_in[number] + 1
            else:
                table_number, first_row = number + 1, 1

            written = text_rows.write(
                table_number,
                table.rows,
                table.header,
                header_declared=table.header_declared,
                first_row=first_row,
                # Read from where words sit on the page, with no ruling or
                # markup to say where the cells are: a guessed header must
                # name every column before its words become labels.
                strict=_from_layout(table.source),
            )
            if not written.lines:
                continue
            number = table_number
            rows_in[number] = first_row - 1 + written.rows

            before = text[cursor : table.character_start]
            pieces.append(before)
            length += len(before)
            pieces.append(written.text)
            written_tables.append(
                WrittenTable(
                    page_number=page.page_number,
                    number=number,
                    character_start=length,
                    character_end=length + len(written.text),
                    first_row=first_row,
                    last_row=first_row + written.rows - 1,
                    labelled=written.labelled,
                    source=table.source,
                )
            )
            length += len(written.text)
            cursor = table.character_end

        pieces.append(text[cursor:])
        page.text = "".join(pieces)

    return written_tables
