from dataclasses import dataclass, field


class FileType:
    PDF = "PDF"
    TXT = "TXT"
    IMAGE = "IMAGE"
    DOCX = "DOCX"


@dataclass
class ExtractedPage:
    page_number: int
    text: str = ""
    # None means "no text yet" - the OCR stage claims these pages.
    extraction_method: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


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
