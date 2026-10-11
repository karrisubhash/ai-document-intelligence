"""General-knowledge replies for chat, when the documents have nothing to say.

A deliberate, opt-in departure from spec 12.4 / FR-16 (abstain rather than
answer), chosen by the deploying institution: with CHAT_GENERAL_KNOWLEDGE on,
a chat question the documents cannot answer - and small talk such as "hi" or
"thanks", which retrieval can never answer - gets a conversational reply from
the model's own knowledge instead of a refusal.

What keeps that honest:

- Only CHAT uses this. `rag.ask`, search and the evaluation runner still
  abstain, so the grounding benchmarks keep measuring grounded answers.
- Only after a clean abstention (FALLBACK_REASONS): nothing relevant was
  retrieved, or the model found no answer in what was. Never after an
  answer was produced and FAILED grounding - that is the hallucination
  signal, and replacing it with an unchecked answer would hide it - and
  never when the answer is probably in a figure the abstention points at.
- The reply carries no citations and is labelled `source: "general"` in the
  API, the stored message and the UI, so nobody mistakes it for a quote
  from their documents.
- The prompt forbids institution-specific facts (fees, dates, policies,
  people): general knowledge is the wrong source for those, and the
  documents already said they do not have them.
- No document text is sent with these requests, only the question and the
  recent conversation.
"""

import logging
import time

from django.conf import settings

from documents.models import Message
from documents.observability import telemetry

from . import streaming
from .engine import Abstention, get_answerer
from .llm import LLMResponse

logger = logging.getLogger("adi.llm")

FALLBACK_REASONS = frozenset(
    {
        Abstention.NO_DOCUMENTS,
        Abstention.NO_RELEVANT_PASSAGES,
        Abstention.MODEL_ABSTAINED,
    }
)

# Earlier turns sent along, so "and what about the second one?" works. Each
# message is clipped: this is conversational context, not a transcript.
HISTORY_TURNS = 6
HISTORY_CLIP = 1500

SYSTEM = """You are the assistant inside a document intelligence platform. \
The user's documents had nothing relevant to this message, so you answer \
from general knowledge. The interface already labels your reply as general \
knowledge; you do not need to say so.

- Answer directly, the way a knowledgeable colleague would: the answer \
first, then only the detail that helps. Keep it concise. Use Markdown - \
short paragraphs, "-" bullet lists, numbered lists, **bold** - when it makes \
the answer easier to read. No tables.
- Greet only if the user greeted you. Greetings, thanks and small talk get \
a sentence or two; you may mention once that you can answer questions about \
their documents.
- In answers to ordinary questions, do not mention the user's documents, \
uploading or searching them. Never claim an answer comes from their \
documents, and never write citation markers like [1].
- If the question is about the user's own organisation - its rules, fees, \
deadlines, dates, people or figures - say plainly that their documents do \
not cover it and that you cannot know their organisation's specifics. Add \
general context only when it is useful and clearly general.
- If you do not know, say so."""


def enabled() -> bool:
    if not getattr(settings, "CHAT_GENERAL_KNOWLEDGE", False):
        return False
    # Needs a generative backend that can take a free-form conversation;
    # the extractive baseline has no knowledge to answer from.
    return hasattr(get_answerer(), "complete")


def should_answer(answer) -> bool:
    return bool(
        answer.abstained and answer.abstention_reason in FALLBACK_REASONS and enabled()
    )


def _history(messages) -> list:
    turns = []
    for message in list(messages)[-HISTORY_TURNS:]:
        role = "assistant" if message.role == Message.Role.ASSISTANT else "user"
        content = (message.content or "").strip()[:HISTORY_CLIP]
        if content:
            turns.append({"role": role, "content": content})
    return turns


def answer(question: str, history=()) -> LLMResponse | None:
    """A general-knowledge reply, or None if the model could not give one.

    None is the caller's cue to keep the abstention it already has: a quota
    error on a free tier must not turn into a 500 for a question that
    already has a perfectly good "not in your documents" answer.
    """
    answerer = get_answerer()
    messages = [
        {"role": "system", "content": SYSTEM},
        *_history(history),
        {"role": "user", "content": question},
    ]
    request = {
        "model": answerer.model,
        "messages": messages,
        # Conversational, so a little variety reads better than greedy
        # decoding; still low, because "general knowledge" should be stable.
        "temperature": getattr(settings, "CHAT_GENERAL_TEMPERATURE", 0.3),
        "max_tokens": getattr(settings, "CHAT_GENERAL_MAX_TOKENS", 4096),
        # Same preset extras as grounded answers (e.g. thinking off on
        # Gemini, whose hidden reasoning otherwise eats max_tokens).
        **getattr(answerer, "request_extras", {}),
    }
    started = time.monotonic()
    # Whatever the grounded attempt streamed (often the abstention
    # sentence) is withdrawn before the new answer starts.
    streaming.reset(source="general")
    try:
        result = answerer.complete(request)
    except Exception as exc:
        logger.warning(
            "llm.general_failed",
            extra={
                "provider": getattr(answerer, "provider", ""),
                "error": type(exc).__name__,
            },
        )
        streaming.reset()
        return None

    text = result.text.strip()
    usage = result.usage
    response = LLMResponse(
        text=text,
        model_name=answerer.name,
        model_version=answerer.version,
        generative=True,
        prompt_characters=sum(len(m["content"]) for m in messages),
        completion_characters=len(text),
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        metadata={
            "provider": getattr(answerer, "provider", ""),
            "served_by": result.served_by,
            "finish_reason": result.finish_reason,
            "source": "general",
        },
    )
    # Counted like every other model call, so cost reporting includes it.
    telemetry.llm_called(response, time.monotonic() - started)
    telemetry.llm_usage(response)
    if not text or result.finish_reason == "content_filter":
        streaming.reset()
        return None
    if result.finish_reason == "length":
        # Unlike a grounded answer, nothing is verified here, so a long
        # reply cut at the limit is still worth showing - but never as if
        # it were complete.
        cut = "\n\n(This answer was cut short.)"
        response.text += cut
        streaming.emit(cut)
    return response
