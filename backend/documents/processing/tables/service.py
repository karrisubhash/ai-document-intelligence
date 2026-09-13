"""Loading the configured table detector.

Same shape as the classifier, field extractor, scanner and embedding
provider loaders: cached per dotted path, validated at the boundary, and
three distinguishable failure modes.
"""

import functools

from django.conf import settings
from django.utils.module_loading import import_string

from .exceptions import TableDetectorNotConfigured, TableDetectorUnavailable
from .layout import TableDetector


@functools.cache
def _load_detector(path: str) -> object:
    return import_string(path)()


def reset_detector_cache() -> None:
    _load_detector.cache_clear()


def get_detector():
    path = getattr(settings, "DOCUMENT_TABLE_DETECTOR", "")

    if not path:
        raise TableDetectorNotConfigured(
            "No DOCUMENT_TABLE_DETECTOR configured. Page text is still "
            "extracted, chunked and searchable; no table structure is "
            "recorded."
        )

    try:
        instance = _load_detector(path)
    except ImportError as exc:
        raise TableDetectorUnavailable(
            f"Could not load table detector {path!r}: {exc}"
        ) from exc

    # Checked at the boundary rather than trusted, for the same reason
    # every other capability here is: a detector missing `verify` would
    # otherwise fail deep inside the stage, after some tables had already
    # been accepted.
    if not isinstance(instance, TableDetector):
        raise TableDetectorUnavailable(
            f"{path!r} does not implement the TableDetector protocol "
            f"(needs name, version, detect() and verify())."
        )

    return instance
