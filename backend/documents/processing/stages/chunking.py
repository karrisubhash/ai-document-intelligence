from documents.processing.chunking import chunk_content
from documents.processing.chunking.exceptions import (
    ChunkingError,
    EmbeddingProviderUnavailable,
)
from documents.observability import telemetry
from documents.processing.context import StageResult
from documents.processing.errors import ErrorCode, StageError, TransientError


def run(context) -> StageResult:
    """Chunk the normalized content and index it for search.

    Reads DocumentContent, never the original upload. Unlike the other
    analysis stages this one is not all-or-nothing: chunks are written even
    when no embedding provider is configured, because a lexically
    searchable document is more useful than an unsearchable one and the
    vectors can be backfilled later.
    """
    try:
        summary = chunk_content(
            context.content.id,
            processing_job=context.processing_job,
        )
    except EmbeddingProviderUnavailable as exc:
        raise TransientError(str(exc)) from exc
    except ChunkingError as exc:
        raise StageError(str(exc), code=ErrorCode.CHUNKING_FAILED) from exc

    if not summary["chunks"]:
        return StageResult(
            message=summary.get("skipped_reason", "Nothing to chunk."),
            skipped=True,
            metadata=summary,
        )

    telemetry.document_indexed(
        summary["chunks"],
        bool(summary["embedded"]),
        document_id=context.document_version.document_id,
    )

    if summary["embedded"]:
        message = (
            f"{summary['chunks']} chunk(s) indexed, {summary['embedded']} "
            f"embedded via {summary['embedding_model']}"
            f"{'' if summary['semantic'] else ' (lexical baseline)'}"
        )
    else:
        message = (
            f"{summary['chunks']} chunk(s) indexed for lexical search; "
            f"no embedding provider configured"
        )

    return StageResult(message=message, metadata=summary)
