"""The bundled figure detector, and loading a configured one.

Same shape as every other pluggable capability here: a Protocol, a cached
loader keyed on the dotted path, validation at the boundary, and an empty
setting meaning SKIPPED rather than silently doing nothing.
"""

import functools
from typing import Protocol, runtime_checkable

from django.conf import settings
from django.utils.module_loading import import_string

from documents.processing.context import FileType

from . import detector
from .exceptions import FigureDetectorNotConfigured, FigureDetectorUnavailable


@runtime_checkable
class FigureDetector(Protocol):
    """What the LAYOUT_ANALYSIS stage requires of a figure detector."""

    name: str
    version: str

    def detect(self, file_path: str, file_type: str, pages: list) -> list: ...


class EmbeddedImageDetector:
    """Counts the images a file embeds. Does not look at them.

    Describing an image needs a vision model, and this project has one
    training path that is deliberately still untrained. Rather than ship a
    describer that guesses, every figure is recorded with an empty
    description - which is what lets the RAG engine say "there is an image
    here I cannot read" instead of either inventing a caption or saying
    nothing at all.
    """

    name = detector.DETECTOR_NAME
    version = detector.DETECTOR_VERSION

    def detect(self, file_path: str, file_type: str, pages: list) -> list:
        if file_type == FileType.PDF:
            from documents.processing.extractors.pdf import open_reader

            return detector.detect_pdf_figures(file_path, open_reader)

        if file_type == FileType.IMAGE:
            # The file IS the figure. Recorded so that a question about a
            # scanned chart whose OCR produced nothing can still be told
            # where to look.
            return [
                detector.DetectedFigure(
                    page_number=1,
                    figure_index=0,
                    metadata={"whole_page": True},
                )
            ]

        # DOCX embeds images too, but a DOCX has no page numbers - see the
        # docx extractor - so there is nowhere truthful to put them. A
        # figure recorded against a page the format does not have is worse
        # than one not recorded.
        return []


@functools.cache
def _load_detector(path: str) -> object:
    return import_string(path)()


def reset_detector_cache() -> None:
    _load_detector.cache_clear()


def get_detector():
    path = getattr(settings, "DOCUMENT_FIGURE_DETECTOR", "")

    if not path:
        raise FigureDetectorNotConfigured(
            "No DOCUMENT_FIGURE_DETECTOR configured. Figures are not "
            "recorded, and an answer cannot point at one."
        )

    try:
        instance = _load_detector(path)
    except ImportError as exc:
        raise FigureDetectorUnavailable(
            f"Could not load figure detector {path!r}: {exc}"
        ) from exc

    if not isinstance(instance, FigureDetector):
        raise FigureDetectorUnavailable(
            f"{path!r} does not implement the FigureDetector protocol "
            f"(needs name, version and detect())."
        )

    return instance
