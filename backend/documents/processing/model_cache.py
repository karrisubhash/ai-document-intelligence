"""Loading local models without phoning home.

sentence-transformers resolves a model id against the Hugging Face Hub on
EVERY load, even when the weights are already in the local cache: it sends
HEAD requests for each config file to see whether a newer revision exists.
Measured on the first search after an API restart, with bge-small already
cached: 21 requests to huggingface.co, and a 30.5-second response where the
model load itself took 6.7 seconds. In an air-gapped deployment the same
requests wait out their network timeouts instead.

So a model is loaded from the cache first, with `local_files_only=True`,
and the Hub is contacted only when the weights are genuinely not there yet -
the first run on a fresh volume. `manage.py warm_models` is the way to make
that first run happen at deploy time rather than on somebody's question.
"""

import logging
import os

logger = logging.getLogger("adi.models")


def offline() -> bool:
    return os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in ("1", "true", "yes")


def load_cached_first(build, *, model_id: str, refuse=()):
    """Call `build(local_files_only=...)`, preferring the local cache.

    `refuse` names exception types that mean "this will fail however it is
    loaded" - a configuration error, not a cache miss - and are re-raised
    without trying the network.
    """
    try:
        return build(local_files_only=True)
    except refuse:
        raise
    except Exception as exc:
        if offline():
            # HF_HUB_OFFLINE was set on purpose; reaching for the network
            # anyway would defeat the setting and hang where it is enforced
            # by a firewall.
            raise
        logger.info(
            "model.not_cached_downloading",
            extra={"model": model_id, "reason": type(exc).__name__},
        )
        return build(local_files_only=False)
