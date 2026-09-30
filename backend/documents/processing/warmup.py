"""Load the configured local models before the first request needs them.

A process that serves search holds its own copy of the embedding model and,
when configured, the cross-encoder. Loading them lazily meant the first
search after every deploy, restart or worker recycle paid for it - measured
at 6.7 s for bge-small from a warm cache, on top of whatever the network
cost. Warming at worker start moves that cost to the moment nobody is
waiting.

Only models that load IN PROCESS are warmed. Hosted providers (Bedrock, an
OpenAI-compatible API) have nothing to load, and "warming" them would spend
a paid API call on every worker boot.
"""

import logging
import time

from django.conf import settings

logger = logging.getLogger("adi.models")


def enabled() -> bool:
    return bool(getattr(settings, "AI_WARMUP", True)) and not getattr(
        settings, "RUNNING_TESTS", False
    )


ALL = ("embedding", "reranker")


def warm(*, reason: str = "startup", components=ALL) -> dict:
    """Load every configured in-process model. Never raises.

    Returns {component: seconds or error class}, for the command to print.
    A failure is logged and left for the request path to handle, which it
    already does by degrading - a worker that refused to start because a
    model was missing would turn a slower search into no search at all.
    """
    from documents.processing.chunking import service as chunking_service
    from documents.rag import engine

    loaded = {}

    def attempt(component, load):
        started = time.monotonic()
        try:
            load()
        except Exception as exc:  # degrade, never crash the worker
            loaded[component] = type(exc).__name__
            logger.warning(
                "model.warmup_failed",
                extra={"component": component, "error": type(exc).__name__},
            )
            return
        loaded[component] = round(time.monotonic() - started, 2)

    try:
        provider = chunking_service.get_provider()
    except Exception:
        provider = None
    if "embedding" in components and provider is not None:
        if hasattr(provider, "_get_model"):
            attempt("embedding", provider._get_model)

    # Web processes rerank; Celery workers never do, and warming a 1 GB
    # cross-encoder in a worker that only indexes is memory spent on nothing.
    if "reranker" in components:
        reranker = engine.get_reranker()
        if hasattr(reranker, "_get_model"):
            attempt("reranker", reranker._get_model)

    logger.info("model.warmup", extra={"reason": reason, "loaded": loaded})
    return loaded
