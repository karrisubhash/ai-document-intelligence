"""Where a streaming answerer sends its text as it is generated.

The chat stream endpoint runs the ordinary chat pipeline - follow-up
resolution, planning, retrieval, rerank, generation, grounding - unchanged,
in a worker thread, with a sink installed here. An answerer that can stream
checks `active()` and, if a sink is installed, emits each piece of text
through `emit()` as the provider sends it. Nothing between the view and the
answerer has to know streaming exists, and with no sink installed every
call is a no-op, so the JSON endpoint, the evaluation runner and the tests
behave exactly as before.

What is streamed is PROVISIONAL. Grounding is checked after generation, and
a grounded answer that fails it is withdrawn; the endpoint's final `done`
event always carries the authoritative answer, and a client must render
that, not the concatenated deltas. `reset()` tells a client to discard what
it has shown so far - sent when one answer is withdrawn and another starts.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_sink: ContextVar = ContextVar("rag_stream_sink", default=None)


@contextmanager
def capture(sink):
    """Install `sink(event, text)` for the duration of the block."""
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def active() -> bool:
    return _sink.get() is not None


def emit(text: str) -> None:
    sink = _sink.get()
    if sink is not None and text:
        sink("delta", text)


def reset() -> None:
    sink = _sink.get()
    if sink is not None:
        sink("reset", "")
