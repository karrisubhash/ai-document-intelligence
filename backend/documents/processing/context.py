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
    # Always rows[0] when set - the Tables tab drops the first row from the
    # body when a header is present.
    header: list = field(default_factory=list)
    # Which extractor found it, recorded as DocumentTable.detector (<= 50).
    source: str = ""
    source_version: str = ""


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
    # Tables found by LAYOUT_ANALYSIS, with offsets into the page text as
    # it will be stored. Written by the pipeline in the same transaction as
    # the pages, so structure and text can never disagree.
    tables: list = field(default_factory=list)
    # Images the document embeds, recorded so an answer can point at
    # content this pipeline cannot read rather than pretend it is absent.
    figures: list = field(default_factory=list)
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
