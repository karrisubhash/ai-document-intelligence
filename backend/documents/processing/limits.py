"""How long a document may take to process, worked out from how long it is.

With no ceiling on pages, one fixed task time limit cannot be right for
every PDF. Measured on one CPU with the real embedder (2 GiB, the worker's
`mem_limit`): a 600-page text PDF took 993 s, of which 827 s was embedding
its 1,540 chunks, so the 900 s limit that was set for "50 MB, 500 pages"
would end a 600-page PDF with TIMEOUT after fifteen minutes of work.
A 60-page scan took 488 s, 396 s of it OCR.

So the limit follows the page count: the base limit every document gets
(`CELERY_TASK_SOFT_TIME_LIMIT`) plus `INGEST_SECONDS_PER_PAGE` for each page,
up to `INGEST_MAX_TASK_SECONDS`. The limit is kept - and not simply made
huge - because it is what frees a worker from a Tesseract or poppler call
that hangs on one pathological page; none of those calls has a timeout of
its own. A limit in proportion to the document still frees it in a time in
proportion to the document.

Only a PDF's page count is known before the file is read in full, so only
PDFs are scaled; every other format keeps the base limit, as before.
"""

import logging

from django.conf import settings

logger = logging.getLogger("adi.limits")

# Between the soft limit, which raises inside the task so the pipeline can
# record TIMEOUT, and the hard limit, which kills a task that ignored it.
HARD_LIMIT_GRACE_SECONDS = 60


def pdf_page_count(file_path: str) -> int | None:
    """Pages in a PDF, or None for anything else or anything unreadable.

    None means "no scaling": the document keeps the base limit and the
    pipeline's own VALIDATION reports what is wrong with it.
    """
    try:
        with open(file_path, "rb") as handle:
            if not handle.read(5).startswith(b"%PDF"):
                return None
        from documents.processing.extractors import pdf

        return pdf.page_count(file_path)
    except Exception as exc:  # noqa: BLE001 - never block a job on a hint
        logger.info("limits.page_count_unavailable", extra={"error": str(exc)})
        return None


def soft_limit_for(pages: int | None) -> int:
    """Soft time limit, in seconds, for a document of `pages` pages."""
    base = int(settings.CELERY_TASK_SOFT_TIME_LIMIT)
    if not pages:
        return base
    ceiling = int(settings.INGEST_MAX_TASK_SECONDS) - HARD_LIMIT_GRACE_SECONDS
    scaled = base + int(pages * float(settings.INGEST_SECONDS_PER_PAGE))
    # Never below the base, whatever the ceiling is set to.
    return max(base, min(scaled, ceiling))


def hard_limit_for(soft: int) -> int:
    """Hard time limit for a given soft one: the same distance above it as
    the base limits are (900 and 960), so a task that honours the soft limit
    never meets the hard one."""
    gap = int(settings.CELERY_TASK_TIME_LIMIT) - int(
        settings.CELERY_TASK_SOFT_TIME_LIMIT
    )
    return soft + (gap if gap > 0 else HARD_LIMIT_GRACE_SECONDS)
