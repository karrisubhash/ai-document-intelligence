from dataclasses import dataclass, field

from documents.processing.normalize import strip_nul


class FileType:
    PDF = "PDF"
    TXT = "TXT"
    IMAGE = "IMAGE"
    DOCX = "DOCX"
    XLSX = "XLSX"
    CSV = "CSV"
    PPTX = "PPTX"
    # Web pages, Rich Text, OpenDocument and data files: see the extractors of
    # the same names. Each reads into the same page-and-table shape as the
    # formats above, so nothing after text extraction knows the difference.
    HTML = "HTML"
    RTF = "RTF"
    ODT = "ODT"
    ODS = "ODS"
    ODP = "ODP"
    JSON = "JSON"
    XML = "XML"
    # Office's binary formats, converted to the modern ones and read as those.
    DOC = "DOC"
    XLS = "XLS"
    PPT = "PPT"


@dataclass(frozen=True)
class PageTable:
    """A table an extractor read as structure, and where it sits in the text.

    Extractors that can see real cells (a Word table, a ruled PDF grid, a
    spreadsheet) hand them over here instead of flattening them to text for
    the layout detector to rediscover - which loses every empty cell and
    every column that happens to line up by accident. The span still
    indexes the page text, so LAYOUT_ANALYSIS verifies it exactly as it
    verifies a detected table: every cell must re-slice out of the page.
    `PageBuilder` (extractors/compose.py) is what produces these, so the
    offsets are measured, never estimated.
    """

    character_start: int
    character_end: int
    rows: list
    # Always rows[0] when set.
    header: list = field(default_factory=list)
    # Which extractor found it, reported in LAYOUT_ANALYSIS's metadata.
    source: str = ""
    source_version: str = ""
    # The source said this is the header (an HTML <th>, a chart's series, a
    # sheet's column names) rather than it being guessed from the cells. A
    # guessed header is used as labels only if it reads as one; see
    # tables/text_rows.py.
    header_declared: bool = False
    # The rest of the previous page's table (a long sheet split into pages):
    # it keeps that table's number and goes on counting its rows.
    continues: bool = False
    # Where each of `rows`' columns was in what the extractor read, before
    # columns empty in this block were dropped: how a continuation's values
    # are matched to the labels of the page before.
    column_ids: tuple = ()


@dataclass
class ExtractedPage:
    page_number: int
    text: str = ""
    # None means "no text yet" - the OCR stage claims these pages.
    extraction_method: str | None = None
    metadata: dict = field(default_factory=dict)
    # Tables the extractor read as structure; spans index `text` as stored.
    tables: list = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        # Judged on the text as it will be stored. A page of nothing but NUL
        # bytes counted as text, and was then written out blank once the
        # pipeline stripped them.
        return bool(strip_nul(self.text).strip())


@dataclass
class IngestionContext:
    document_version: object
    file_path: str
    processing_job: object | None = None
    file_type: str | None = None
    pages: list[ExtractedPage] = field(default_factory=list)
    # Tables LAYOUT_ANALYSIS wrote into the page text as labelled rows
    # (tables/text_rows.Region, spans into the text as it will be stored).
    # Not stored on their own: the text is the table.
    tables: list = field(default_factory=list)
    # Images the document embeds, recorded so an answer can point at
    # content this pipeline cannot read rather than pretend it is absent.
    figures: list = field(default_factory=list)
    # Reads the figures of THIS document within its budget (figures/vision.py),
    # made once by TEXT_EXTRACTION and shared by the stages after it. None when
    # no vision model is configured.
    figure_describer: object | None = None
    metadata: dict = field(default_factory=dict)
    # The persisted DocumentContent, set once the content stages finish.
    # Analysis stages read this instead of reopening the original file.
    content: object | None = None

    def pages_needing_ocr(self) -> list[ExtractedPage]:
        return [page for page in self.pages if page.extraction_method is None]


@dataclass
class StageResult:
    """What a stage reports back for its ProcessingEvent."""

    message: str = ""
    metadata: dict = field(default_factory=dict)
    # A stage that legitimately had nothing to do - recorded as SKIPPED
    # rather than dressed up as success.
    skipped: bool = False
