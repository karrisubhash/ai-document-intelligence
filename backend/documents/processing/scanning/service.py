import functools

from django.conf import settings
from django.utils.module_loading import import_string

from .base import Verdict
from .exceptions import ScannerNotConfigured, ScannerUnavailable


@functools.cache
def _load_scanner(path: str, ruleset: str):
    """Instantiate once per (dotted path, ruleset) pair.

    The ruleset is in the key for the same reason the classifier's artifact
    is: a scanner holding a stale signature set would keep clearing files
    against rules the operator has already replaced.
    """
    return import_string(path)()


def reset_scanner_cache() -> None:
    _load_scanner.cache_clear()


def get_scanner():
    path = getattr(settings, "DOCUMENT_SCANNER", "")

    if not path:
        raise ScannerNotConfigured(
            "No DOCUMENT_SCANNER configured. Uploads are not scanned, and "
            "the stage records that rather than implying they were."
        )

    try:
        return _load_scanner(
            path, getattr(settings, "DOCUMENT_SCANNER_RULESET", "")
        )
    except ImportError as exc:
        raise ScannerUnavailable(
            f"Could not load scanner {path!r}: {exc}"
        ) from exc


def scan_file(file_path: str):
    """Scan one stored file. Raises ScannerNotConfigured when none is set."""
    scanner = get_scanner()
    result = scanner.scan(file_path)

    if result.verdict not in (Verdict.CLEAN, Verdict.INFECTED, Verdict.SUSPICIOUS):
        # A scanner we did not write returning something we do not
        # understand must not be read as "fine".
        raise ScannerUnavailable(
            f"Scanner {scanner.name} returned unknown verdict "
            f"{result.verdict!r}; refusing to interpret it as clean."
        )

    return result
