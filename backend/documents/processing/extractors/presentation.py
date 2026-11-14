"""PowerPoint decks: a page per slide, tables and charts as tables.

A slide is a page in the one sense a citation needs: "slide 7" points at
something the reader can open. Unlike a DOCX, a deck has real page boundaries
- the slides - so every slide is one page, in presentation order.

What a slide holds that plain text extraction loses:

* **Reading order.** Shapes are stored in the order they were added, not the
  order they read. They are sorted top to bottom and then left to right, in
  bands - two boxes at nearly the same height are side by side, not stacked -
  with the title always first and groups kept together.
* **Tables**, with merged cells, written as structure.
* **Charts.** A chart's numbers live in an embedded workbook, not on the
  slide, so they appear nowhere in its text. Categories and series are written
  as a table under the chart's title, which is what a reader of the chart
  actually takes from it.
* **Speaker notes**, which routinely carry the explanation the slide only
  gestures at.
* **Pictures**, when one reads like a document (see `pictures`).

Bullets are written only where the slide's own markup asks for one. A bullet
inherited from the slide master is not in the slide's XML, and guessing at it
would put a marker on text that visibly has none.

Footers, dates and slide numbers are skipped: they repeat on every slide and
are decoration, and a hundred copies of "Confidential" in an index is noise.
"""

import re
import zipfile
from dataclasses import dataclass, field

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import compose, ooxml
from documents.processing.extractors.listmarks import format_auto_number
from documents.processing.extractors.pictures import PictureReader

SOURCE_TABLE = "python-pptx:table"
SOURCE_CHART = "python-pptx:chart"

PRESENTATION_PART = "ppt/presentation.xml"

A = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Two shapes whose tops are within this (0.3 inch in EMU) read as one row.
BAND = 274320

_SLIDE_PART = re.compile(r"^ppt/slides/slide\d+\.xml$")


@dataclass
class SlidePage:
    text: str = ""
    tables: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def is_pptx(file_path: str) -> bool:
    return ooxml.has_part(file_path, PRESENTATION_PART)


def slide_count(file_path: str) -> int:
    """Slides in the deck, from the archive's part names - no parsing."""
    try:
        with zipfile.ZipFile(file_path) as archive:
            return sum(1 for name in archive.namelist() if _SLIDE_PART.match(name))
    except (zipfile.BadZipFile, OSError) as exc:
        raise StageError(
            f"Could not read PPTX archive: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc


def _number(value) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return repr(round(float(value), 10))


class _Slides:
    def __init__(self, pictures: PictureReader):
        from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

        self.shape_types = MSO_SHAPE_TYPE
        self.placeholders = PP_PLACEHOLDER
        self.pictures = pictures
        self.tables = 0
        self.charts = 0
        self.notes = 0

    # -- ordering ------------------------------------------------------

    def _is_title(self, shape) -> bool:
        return shape.is_placeholder and shape.placeholder_format.type in (
            self.placeholders.TITLE,
            self.placeholders.CENTER_TITLE,
            self.placeholders.VERTICAL_TITLE,
        )

    def _is_furniture(self, shape) -> bool:
        return shape.is_placeholder and shape.placeholder_format.type in (
            self.placeholders.DATE,
            self.placeholders.FOOTER,
            self.placeholders.SLIDE_NUMBER,
            self.placeholders.HEADER,
        )

    @staticmethod
    def _top(shape) -> int:
        try:
            return shape.top or 0
        except Exception:
            return 0

    @staticmethod
    def _left(shape) -> int:
        try:
            return shape.left or 0
        except Exception:
            return 0

    def _rows(self, shapes) -> list:
        """Shapes grouped into rows: those starting within a band of the row's
        first shape. Measured from that shape, not by cutting the slide into
        fixed bands - a box at 2.0 inches and one at 2.1 are one row wherever
        a fixed boundary happens to fall between them."""
        rows: list = []
        for shape in sorted(shapes, key=self._top):
            if rows and self._top(shape) - self._top(rows[-1][0]) <= BAND:
                rows[-1].append(shape)
            else:
                rows.append([shape])
        return rows

    def _flatten(self, shapes) -> list:
        """Shapes in reading order, groups expanded in place."""
        out: list = []
        for row in self._rows(shapes):
            for shape in sorted(row, key=self._left):
                if shape.shape_type == self.shape_types.GROUP:
                    out += self._flatten(shape.shapes)
                else:
                    out.append(shape)
        return out

    # -- text ----------------------------------------------------------

    @staticmethod
    def _marker(properties, level: int, counters: dict) -> str:
        auto = None if properties is None else properties.find(f"{{{A}}}buAutoNum")
        if auto is not None:
            for deeper in [index for index in counters if index > level]:
                del counters[deeper]
            start = int(auto.get("startAt", "1") or 1)
            counters[level] = counters.get(level, start - 1) + 1
            return format_auto_number(counters[level], auto.get("type", "arabicPeriod"))

        # Anything that is not a numbered paragraph ends the run of numbers at
        # its level and below.
        for index in [index for index in counters if index >= level]:
            del counters[index]

        bullet = None if properties is None else properties.find(f"{{{A}}}buChar")
        if bullet is None:
            return ""
        char = bullet.get("char", "")
        # Bullets are glyphs from a symbol font; outside it they are letters.
        if char in ("-", "*", "+", chr(0x2013), chr(0x2014)):
            return char
        return chr(0x2022)

    def _lines(self, text_frame) -> list:
        lines: list = []
        counters: dict = {}
        for paragraph in text_frame.paragraphs:
            level = paragraph.level or 0
            marker = self._marker(paragraph._p.pPr, level, counters)
            text = paragraph.text.replace("\v", "\n").strip()
            if not text:
                continue
            prefix = "  " * level + (f"{marker} " if marker else "")
            lines.append(prefix + text)
        return lines

    # -- one slide -----------------------------------------------------

    def _chart_rows(self, chart) -> list:
        try:
            plot = chart.plots[0]
            categories = [str(category) for category in plot.categories]
            series = list(plot.series)
        except Exception:
            return []  # scatter and bubble charts have no categories

        if not categories or not series:
            return []

        rows = [[""] + [str(item.name or "") for item in series]]
        for index, category in enumerate(categories):
            row = [category]
            for item in series:
                values = list(item.values)
                row.append(_number(values[index]) if index < len(values) else "")
            rows.append(row)
        return rows

    def _shape(self, shape, builder) -> None:
        if getattr(shape, "has_table", False) and shape.has_table:
            self.tables += 1
            builder.add_table(
                [[cell.text for cell in row.cells] for row in shape.table.rows],
                source=SOURCE_TABLE,
            )
            return

        if getattr(shape, "has_chart", False) and shape.has_chart:
            self.charts += 1
            chart = shape.chart
            try:
                if chart.has_title and chart.chart_title.has_text_frame:
                    title = chart.chart_title.text_frame.text.strip()
                    if title:
                        builder.add_text(title)
            except Exception:
                pass
            rows = self._chart_rows(chart)
            if rows:
                builder.add_table(rows, header=True, source=SOURCE_CHART)
            return

        if shape.has_text_frame:
            for line in self._lines(shape.text_frame):
                builder.add_text(line)
            return

        # A picture on the slide: read it only if it reads like a document.
        try:
            blob = shape.image.blob
        except Exception:
            return
        text = self.pictures.text(blob)
        if text:
            builder.add_text(text)

    def slide(self, slide, version: str) -> SlidePage:
        builder = compose.PageBuilder(source=SOURCE_TABLE, source_version=version)

        shapes = [s for s in self._flatten(slide.shapes) if not self._is_furniture(s)]
        shapes.sort(key=lambda shape: 0 if self._is_title(shape) else 1)

        for shape in shapes:
            self._shape(shape, builder)

        metadata = {"extractor": "python-pptx"}
        if slide._element.get("show") == "0":
            metadata["hidden"] = True

        try:
            has_notes = slide.has_notes_slide
            frame = slide.notes_slide.notes_text_frame if has_notes else None
        except Exception:
            frame = None
        notes = "\n".join(self._lines(frame)).strip() if frame is not None else ""
        if notes:
            self.notes += 1
            builder.add_text(f"Notes: {notes}")

        text, tables = builder.build()
        return SlidePage(text=text, tables=tables, metadata=metadata)


def extract_slides(file_path: str, *, image_ocr=None, max_images: int = 0) -> tuple:
    """(pages, metadata): one `SlidePage` per slide, in order.

    `image_ocr(PIL.Image) -> OcrPage` reads a picture on a slide;
    `max_images` caps how many one deck may spend on it.
    """
    expansion = ooxml.check_expansion(file_path, "PPTX")

    try:
        import pptx

        deck = pptx.Presentation(file_path)
    except Exception as exc:
        raise StageError(
            f"Could not read PPTX: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc

    version = getattr(pptx, "__version__", "")
    pictures = PictureReader(image_ocr, max_images)
    reader = _Slides(pictures)

    try:
        pages = [reader.slide(slide, version) for slide in deck.slides]
    except StageError:
        raise
    except Exception as exc:
        raise StageError(
            f"PPTX text extraction failed: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc

    for number, page in enumerate(pages, start=1):
        page.metadata["slide"] = number
        page.metadata["extractor_version"] = version

    return pages, {
        "extractor": "python-pptx",
        "extractor_version": version,
        "slides": len(pages),
        "tables": reader.tables,
        "charts": reader.charts,
        "notes": reader.notes,
        "pictures_ocred": pictures.read,
        **expansion,
    }
