"""Re-indexing existing documents (spec §11.4, §22).

Changing the embedding model invalidates every stored vector, and §11.4
calls for "a controlled re-indexing strategy" rather than a cutover. This is
that strategy: one task per document, on its own queue, so a bulk re-index
of the whole corpus does not sit in front of the uploads people are waiting
on.

That queue separation is the concrete form of NFR-02's "independently
scalable". Re-indexing is embedding-bound and OCR is CPU-bound, so they want
different worker counts on different machines - and with one shared queue,
the only way to make a re-index finish sooner is to add workers that also
pick up ingestion, which is the opposite of control.
"""

import logging

from celery import shared_task

from documents.models import DocumentContent
from documents.observability import telemetry
from documents.processing.chunking import chunk_content, get_provider
from documents.processing.chunking.exceptions import (
    ChunkingError,
    EmbeddingProviderNotConfigured,
    EmbeddingProviderUnavailable,
)

logger = logging.getLogger("adi.reindex")

MAX_RETRIES = 3


@shared_task(bind=True, max_retries=MAX_RETRIES)
def reindex_content_task(self, document_content_id: int):
    """Re-chunk and re-embed one DocumentContent.

    Deliberately per-content rather than one task for the whole corpus: a
    single long task cannot be parallelised, loses all its progress when a
    worker restarts, and makes queue depth meaningless because the work is
    one item deep no matter how much is left to do.
    """
    # Checked BEFORE chunking, not by catching an exception from it.
    #
    # chunk_content handles a missing provider internally and returns a
    # summary rather than raising - which is right for first-time indexing,
    # where chunks without vectors are still lexically searchable. It is
    # wrong for a RE-index: chunking deletes the existing chunks first, and
    # the embeddings cascade with them. Re-indexing with no provider
    # therefore destroyed working vectors and wrote none back, leaving the
    # document worse off than before the task ran.
    try:
        get_provider()
    except EmbeddingProviderNotConfigured:
        logger.info(
            "reindex.skipped", extra={"document_content_id": document_content_id}
        )
        return {"content_id": document_content_id, "skipped": True}
    except EmbeddingProviderUnavailable as exc:
        raise self.retry(exc=exc, countdown=2**self.request.retries) from exc

    try:
        summary = chunk_content(document_content_id)
    except EmbeddingProviderUnavailable as exc:
        # A provider that is briefly down should not abandon the re-index.
        raise self.retry(exc=exc, countdown=2**self.request.retries) from exc
    except DocumentContent.DoesNotExist:
        # The document was deleted while the re-index was queued. Nothing to
        # do, and retrying will not bring it back.
        logger.info(
            "reindex.content_gone", extra={"document_content_id": document_content_id}
        )
        return {"content_id": document_content_id, "missing": True}
    except ChunkingError:
        # Permanent: the same content chunked again fails the same way.
        raise

    telemetry.document_indexed(
        summary["chunks"],
        bool(summary["embedded"]),
        document_id=None,
    )

    logger.info(
        "reindex.completed",
        extra={
            "document_content_id": document_content_id,
            "chunks": summary["chunks"],
            "embedded": summary["embedded"],
        },
    )
    return summary
