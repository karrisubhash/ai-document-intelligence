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

PROGRESS. `status(stage)` says which step is running (retrieving, ranking,
writing, verifying) and `sources(passages)` names the passages the answer
will be written from, before a word of it exists. Both are previews too: the
cited sources in `done` are the authoritative ones.

STOPPING. The sink may raise `Cancelled` - the stream view does when the
reader has gone away - and every emit, reset, status and sources call is a
point where a turn notices it. It is a BaseException on purpose: the
gateway's "try the next model" and the answerer's "fall back to quoting"
both catch Exception, and a reader who pressed Stop wants neither.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_sink: ContextVar = ContextVar("rag_stream_sink", default=None)

RETRIEVING = "retrieving"
RANKING = "ranking"
WRITING = "writing"
VERIFYING = "verifying"


class Cancelled(BaseException):  # noqa: N818 - a signal, like GeneratorExit
    """The reader stopped the answer; nothing more should be generated."""


@contextmanager
def capture(sink):
    """Install `sink(event, payload)` for the duration of the block."""
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


def reset(source: str = "") -> None:
    """Withdraw what was streamed; `source` labels the answer that follows."""
    sink = _sink.get()
    if sink is not None:
        sink("reset", source)


def status(stage: str) -> None:
    """Which step of the answer is running now."""
    sink = _sink.get()
    if sink is not None:
        sink("status", {"stage": stage})


def sources(passages) -> None:
    """The passages the answer will be written from, before it is written.

    Titles and pages only - never passage text: the authoritative citations
    arrive with `done`, and these are what a reader sees while waiting.
    """
    sink = _sink.get()
    if sink is None:
        return
    sink(
        "sources",
        {
            "sources": [
                {
                    "marker": passage.marker,
                    "document_id": passage.chunk.document_version.document_id,
                    "document_title": passage.chunk.document_version.document.title,
                    "page_number": getattr(passage.chunk, "page_number", None),
                }
                for passage in passages
            ]
        },
    )
