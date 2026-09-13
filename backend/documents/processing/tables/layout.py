"""The bundled table detector, behind the capability contract (§26).

The detection itself lives in `detector` as pure functions. This wraps them
in the shape every other pluggable capability in the pipeline has - a name,
a version, and a boundary that validates what came back - so that a better
detector (a real PDF layout model, a vision model) can replace it through a
setting without any caller changing.
"""

from typing import Protocol, runtime_checkable

from . import detector
from .exceptions import TableDetectionError


@runtime_checkable
class TableDetector(Protocol):
    """What the LAYOUT_ANALYSIS stage requires of a detector.

    `verify` is part of the contract, not a helper the stage happens to
    call. A detector that cannot demonstrate its output is present in the
    page has not earned the right to store it, and the stage enforces that
    on every table rather than trusting the implementation.
    """

    name: str
    version: str

    def detect(self, page_number: int, text: str) -> list: ...

    def verify(self, table, page_text: str) -> bool: ...


class LayoutTableDetector:
    """Whitespace-layout table detection over extracted page text.

    Deliberately not a machine-learned layout model. It needs no artifact,
    no GPU and no download, it runs in microseconds per page, and it is
    fully explainable - every table it finds can be pointed at on the page.
    A model would find more tables; it would also need the entire
    artifact-governance path the classifier has, and this one is honest
    about what it misses.
    """

    name = detector.DETECTOR_NAME
    version = detector.DETECTOR_VERSION

    def detect(self, page_number: int, text: str) -> list:
        try:
            return detector.detect_tables(page_number, text)
        except Exception as exc:  # pragma: no cover - defensive
            raise TableDetectionError(
                f"Table detection failed on page {page_number}: {exc}"
            ) from exc

    def verify(self, table, page_text: str) -> bool:
        return detector.verify(table, page_text)
