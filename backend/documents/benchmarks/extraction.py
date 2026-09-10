"""Harness for comparing document extraction approaches.

Deliberately measures rather than assumes: pypdf/pdf2image/tesseract were
a baseline wiring choice, not a technology decision. Point this at a real
evaluation set and let the numbers decide what belongs in the pipeline.

Expected evaluation-set layout::

    <dataset>/
        born_digital/invoice.pdf
        born_digital/invoice.txt        <- optional ground truth
        scanned/report.pdf
        image/receipt.png

Any `.txt` sitting next to a document with the same stem is treated as
that document's ground truth. Categories are just directory names, so
the set can mirror whatever document classes matter.
"""

import difflib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from django.conf import settings

from documents.processing.context import FileType
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import ocr, pdf, plain_text
from documents.processing.stages.ingestion import detect_file_type

try:
    import resource
except ImportError:  # Windows
    resource = None

DOCUMENT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}


@dataclass
class PageResult:
    page_number: int
    text: str
    extraction_method: str


@dataclass
class DocumentResult:
    path: str
    category: str
    file_type: str | None
    approach: str
    succeeded: bool = True
    error_code: str = ""
    error_message: str = ""
    page_count: int = 0
    character_count: int = 0
    duration_seconds: float = 0.0
    peak_child_memory_mb: float = 0.0
    similarity: float | None = None
    word_accuracy: float | None = None
    methods: dict = field(default_factory=dict)


def _child_memory_mb() -> float:
    """Peak RSS of child processes - where poppler and tesseract live."""
    if resource is None:
        return 0.0
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024


# --- Extraction approaches under comparison ---------------------------------


def approach_pdf_text(path: str, file_type: str) -> list[PageResult]:
    """Text layer only. Fast, exact where a text layer exists, blank where not."""
    if file_type == FileType.TXT:
        return [PageResult(1, plain_text.read_text(path), "PLAIN_TEXT")]

    if file_type != FileType.PDF:
        raise StageError(
            "Approach only handles PDF/TXT.", code=ErrorCode.UNSUPPORTED_FILE_TYPE
        )

    return [
        PageResult(index, text, "PDF_TEXT")
        for index, text in enumerate(pdf.extract_pages(path), start=1)
    ]


def approach_ocr(path: str, file_type: str) -> list[PageResult]:
    """OCR every page, ignoring any text layer. The slow upper bound."""
    if file_type == FileType.TXT:
        return [PageResult(1, plain_text.read_text(path), "PLAIN_TEXT")]

    if file_type == FileType.IMAGE:
        return [PageResult(1, ocr.ocr_image_file(path), "OCR")]

    return [
        PageResult(number, ocr.ocr_pdf_page(path, number, settings.OCR_DPI), "OCR")
        for number in range(1, pdf.page_count(path) + 1)
    ]


def approach_auto(path: str, file_type: str) -> list[PageResult]:
    """What the production pipeline does: text layer, OCR only where blank."""
    if file_type == FileType.TXT:
        return [PageResult(1, plain_text.read_text(path), "PLAIN_TEXT")]

    if file_type == FileType.IMAGE:
        return [PageResult(1, ocr.ocr_image_file(path), "OCR")]

    pages = []
    for number, text in enumerate(pdf.extract_pages(path), start=1):
        if text.strip():
            pages.append(PageResult(number, text, "PDF_TEXT"))
        else:
            pages.append(
                PageResult(
                    number, ocr.ocr_pdf_page(path, number, settings.OCR_DPI), "OCR"
                )
            )
    return pages


APPROACHES = {
    "pdf_text": approach_pdf_text,
    "ocr": approach_ocr,
    "auto": approach_auto,
}


# --- Quality metrics --------------------------------------------------------


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def score_against_truth(extracted: str, truth: str) -> tuple[float, float]:
    """(character similarity, word accuracy) against ground truth."""
    got, expected = _normalize(extracted), _normalize(truth)

    similarity = difflib.SequenceMatcher(None, expected, got).ratio()

    expected_words, got_words = expected.split(), got.split()
    if not expected_words:
        return similarity, 1.0 if not got_words else 0.0

    matcher = difflib.SequenceMatcher(None, expected_words, got_words)
    matched = sum(block.size for block in matcher.get_matching_blocks())

    return similarity, matched / len(expected_words)


# --- Runner -----------------------------------------------------------------


def discover_documents(dataset_dir: Path) -> list[tuple[Path, str]]:
    documents = []
    for path in sorted(dataset_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in DOCUMENT_SUFFIXES:
            continue
        relative = path.relative_to(dataset_dir)
        category = relative.parts[0] if len(relative.parts) > 1 else "uncategorized"
        documents.append((path, category))
    return documents


def ground_truth_for(path: Path) -> str | None:
    candidate = path.with_suffix(".txt")
    if candidate.exists():
        return candidate.read_text(encoding="utf-8", errors="replace")
    return None


def run_one(path: Path, category: str, approach_name: str) -> DocumentResult:
    file_type = detect_file_type(str(path))
    result = DocumentResult(
        path=str(path),
        category=category,
        file_type=file_type,
        approach=approach_name,
    )

    memory_before = _child_memory_mb()
    started = time.monotonic()

    try:
        pages = APPROACHES[approach_name](str(path), file_type)
    except Exception as exc:
        result.succeeded = False
        result.error_code = getattr(exc, "code", ErrorCode.UNEXPECTED_ERROR)
        result.error_message = str(exc)
        result.duration_seconds = round(time.monotonic() - started, 3)
        return result

    result.duration_seconds = round(time.monotonic() - started, 3)
    result.peak_child_memory_mb = round(max(0.0, _child_memory_mb() - memory_before), 1)
    result.page_count = len(pages)
    result.character_count = sum(len(page.text) for page in pages)

    for page in pages:
        result.methods[page.extraction_method] = (
            result.methods.get(page.extraction_method, 0) + 1
        )

    truth = ground_truth_for(path)
    if truth is not None:
        similarity, word_accuracy = score_against_truth(
            "\n".join(page.text for page in pages), truth
        )
        result.similarity = round(similarity, 4)
        result.word_accuracy = round(word_accuracy, 4)

    return result


def run_benchmark(dataset_dir: Path, approach_names: list[str]) -> list[DocumentResult]:
    results = []
    for path, category in discover_documents(dataset_dir):
        for approach_name in approach_names:
            results.append(run_one(path, category, approach_name))
    return results


def summarize(results: list[DocumentResult]) -> dict:
    summary = {}
    for approach_name in sorted({result.approach for result in results}):
        subset = [result for result in results if result.approach == approach_name]
        succeeded = [result for result in subset if result.succeeded]
        scored = [result for result in succeeded if result.similarity is not None]

        summary[approach_name] = {
            "documents": len(subset),
            "succeeded": len(succeeded),
            "failure_rate": round(1 - len(succeeded) / len(subset), 4) if subset else 0,
            "total_seconds": round(sum(r.duration_seconds for r in subset), 3),
            "mean_seconds": (
                round(sum(r.duration_seconds for r in subset) / len(subset), 3)
                if subset
                else 0
            ),
            "total_pages": sum(r.page_count for r in succeeded),
            "peak_child_memory_mb": (
                round(max(r.peak_child_memory_mb for r in subset), 1) if subset else 0
            ),
            "scored_documents": len(scored),
            "mean_similarity": (
                round(sum(r.similarity for r in scored) / len(scored), 4)
                if scored
                else None
            ),
            "mean_word_accuracy": (
                round(sum(r.word_accuracy for r in scored) / len(scored), 4)
                if scored
                else None
            ),
        }
    return summary


def to_report(results: list[DocumentResult]) -> dict:
    return {
        "results": [asdict(result) for result in results],
        "summary": summarize(results),
    }


def write_report(report: dict, path: Path) -> None:
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
