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

This stage runs in the CONTENT phase, not the analysis phase, because
structure is part of the normalized representation of the document rather
than a conclusion drawn from it. It therefore has no database access: it
fills `context.tables` and `context.figures`, and the pipeline writes them
in the same transaction as the pages they point into. Pages without their
structure is not a state anything downstream should have to handle.

The two halves are independent. Either detector can be unconfigured or
unavailable without costing the document the other, and the stage is only
SKIPPED when neither ran.
"""

from documents.processing.context import StageResult
from documents.processing.figures.exceptions import (
    FigureDetectionError,
    FigureDetectorNotConfigured,
    FigureDetectorUnavailable,
)
from documents.processing.figures.service import get_detector as get_figure_detector
from documents.processing.normalize import strip_nul
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
    try:
        detector = get_table_detector()
    except TableDetectorNotConfigured as exc:
        return str(exc), {"skipped": True}
    except TableDetectorUnavailable as exc:
        # Transient by taxonomy, but a missing detector must not cost the
        # document its text. Recorded with a reason so the run is visible
        # in ProcessingEvent rather than silently empty.
        return f"Table detection unavailable: {exc}", {
            "skipped": True,
            "tables_unavailable": True,
        }

    detected = []
    rejected = 0

    for page in context.pages:
        # The SAME normalization the pipeline applies before storing, so
        # the offsets recorded here index the text that actually lands in
        # PageContent. See processing/normalize.py.
        text = strip_nul(page.text)
        if not text.strip():
            continue

        for table in detector.detect(page.page_number, text):
            # Verified per table, on every run. A detector that returns a
            # row the page does not contain gets that row dropped here -
            # fabrication is refused at the boundary rather than trusted
            # not to happen.
            if not detector.verify(table, text):
                rejected += 1
                continue
            detected.append(table)

    # Re-numbered across the document in reading order. The detector
    # numbers within a page, and two pages would otherwise both claim
    # table 0 - which the uniqueness constraint on (content, table_index)
    # would reject at the very end of an otherwise successful pipeline.
    context.tables = [_renumber(table, index) for index, table in enumerate(detected)]

    metadata = {
        "tables": len(context.tables),
        "tables_rejected": rejected,
        "table_detector": detector.name,
        "table_detector_version": detector.version,
    }

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
                f"No tables stored: rejected {rejected} that did not verify "
                f"against the page."
            )
        return note, metadata

    pages_with_tables = len({table.page_number for table in context.tables})
    note = f"Found {len(context.tables)} table(s) across {pages_with_tables} page(s)."
    if rejected:
        note += f" Rejected {rejected} that did not verify against the page."

    metadata["pages_with_tables"] = pages_with_tables
    return note, metadata


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


def _renumber(table, index: int):
    from dataclasses import replace

    return replace(table, table_index=index)
