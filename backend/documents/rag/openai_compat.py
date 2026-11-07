"""One answerer for every backend that speaks the OpenAI chat API.

`ClaudeAnswerer` is the generative path this project was designed around,
and it is one vendor deep. That is a reasonable bet for a production
deployment and a bad one for everything around it: a reviewer with no
Anthropic key cannot run the benchmark, a privacy review that forbids
sending institutional documents to a third party has nowhere to go, and
"swap the provider" - which section 26 promises is one setting - turns out
to mean writing a second answerer first.

This module is that second answerer, and it is deliberately only one class.
The OpenAI chat-completions request has become the shape almost every other
provider also accepts, so the entire provider dimension collapses into
three strings: a base_url, a model id, and the NAME of the environment
variable holding the key. Gemini, Groq, OpenRouter, Mistral, GitHub Models, a
local Ollama and Cerebras all answer the same
`client.chat.completions.create(...)` call; what differs between them is
where it is sent. Those strings live in `documents.rag.providers` as presets,
because they are unforgiving - see the Gemini preset for the URL typo that
reports itself as a missing model - and `RAG_ANSWERER_PROVIDER=groq` is then
the whole configuration. An explicit RAG_ANSWERER_BASE_URL /
RAG_ANSWERER_MODEL still overrides a preset, because vLLM, LM Studio and an
internal gateway all speak this API too and none of them can be enumerated.

WHERE THE CALL GOES is the gateway's decision, not this class's
(`documents.rag.gateway`): a chain of provider:model links, a shared budget
that every process reads, cooldowns after a 429, and a reserve that keeps the
last calls of the day for chat. This class builds the grounded request and
judges the reply; the gateway finds a model to send it to.

THE GUARDRAILS DO NOT MOVE, and they are not restated here. The system
prompt is IMPORTED from `claude.py` rather than rewritten, because it is
half of a contract whose other half is `grounding.validate`: a marker on
every claim, every figure appearing verbatim in a cited passage, passages
treated as data rather than as instructions. A second copy of that prompt
drifts from the checker within one edit, and the way that drift shows up is
not an error - it is a slow rise in answers downgraded to abstentions,
which looks like a weak model rather than a stale prompt.

COMPATIBLE IS NOT IDENTICAL, and the differences that matter are the quiet
ones. Gemini silently ignores OpenAI parameters it does not implement, so a
200 and a plausible answer is not evidence that a parameter took effect.
Groq converts `temperature=0` to 1e-8, so grounded output there is not
reproducible run to run. OpenRouter can return HTTP 200 whose body is an
`error` object with NO `choices` array, which the SDK does not raise on and
which turns `choices[0]` into an IndexError several frames from the cause.
Ollama ignores the API key but the SDK refuses to build a client without
one. Each of those is handled where it bites, and named in a comment there.

WHICH BACKEND TO POINT IT AT. A grounded prompt is the system rules plus at
most RAG_MAX_PASSAGES passages inside RAG_CONTEXT_BUDGET characters - 8,000
by default, so about two to three thousand tokens. Every preset takes that
in one request; what binds on a free tier is how many requests a model will
take a minute and a day (Gemini: 5 and 20), and on Groq's free gpt-oss tier
its 8K tokens a minute, two or three questions. The default is Gemini Flash
because this is an instruction-following task rather than a knowledge one: a
weaker model here does not produce confidently wrong answers so much as
answers that miss markers and get downgraded, so the pipeline stays safe and
becomes useless. Other providers are best added BEHIND it in
RAG_ANSWERER_CHAIN, where they turn "AI writing is at capacity" into more
answers a day.

DATA USE IS A CONFIGURATION FACT, SO IT IS RECORDED LIKE ONE. Google's
terms say that on unpaid tiers submitted content and the responses to it
are used to improve their products, and OpenRouter's free models may be
trained on by whichever provider served them. For institutional records
that may be disqualifying, and it is not something anyone discovers by
reading the code. So the presets carry the flag, the client logs it once on
construction, every generated answer's metadata says which way it was set,
and the System status screen shows it per link. The privacy-clean options
both work here unchanged: attach billing to the Gemini key, or point at
`ollama` and let nothing leave the machine.

FAILURE FALLS BACK RATHER THAN 500s, exactly as in `claude.py`. A missing
package, an unset key, an exhausted free-tier quota, a timeout and a
malformed 200 all mean the same thing to someone waiting for an answer, and
all of them degrade to the extractive answerer with `fell_back_from` set. A
quietly degraded answer that does not say so is worse than a slow one - and
on a free tier the 429 is not the exceptional case, it is Tuesday.

RUN LIVE through Gemini: the public deployment answers with this class, and
the measurements recorded in the Gemini preset (per-minute and per-day
quotas, which models answer, time to first token) were taken against it.
The other presets have NOT been run against their live backends from this
repository; their request shapes are the ones each provider documents, the
logic is driven by stubbed clients in the tests, and the first real call to
each is still the test.
"""

import logging

from django.conf import settings

from . import budget, streaming
from . import gateway as gateway_module

# Shared with the Claude answerer on purpose: this prompt encodes the
# citation discipline and the figure rule that documents.rag.grounding
# enforces afterwards, and two copies of a contract is one copy of a
# contract plus a bug waiting for the next edit. If it ever has to be
# copied instead of imported, the two must be changed together.
from .claude import SYSTEM
from .gateway import (
    TOKEN_LIMIT_FIELD,
    Completion,
    Gateway,
    NoChoicesReturned,
    OpenAICompatibleNotConfigured,
    Trace,
    links_from_settings,
)
from .llm import ABSTENTION_TEXT, ExtractiveAnswerer, LLMResponse, is_abstention

# Re-exported: where these lived before the gateway existed, and where
# callers and tests still import them from.
from .providers import (  # noqa: F401
    CEREBRAS,
    DEFAULT_KEY_ENV,
    DEFAULT_PROVIDER,
    GEMINI,
    GITHUB,
    GROQ,
    MISTRAL,
    OLLAMA,
    OPENAI,
    OPENROUTER,
    PRESETS,
    Preset,
)

logger = logging.getLogger("adi.llm")

__all__ = [
    "Completion",
    "NoChoicesReturned",
    "OpenAICompatibleAnswerer",
    "OpenAICompatibleNotConfigured",
    "PRESETS",
    "Preset",
    "reset_cooldowns",
]

# This is an extraction task. The answer has to repeat figures from the
# passages exactly and cite them, and sampling variety buys nothing any
# reader wants. Greedy wherever the provider honours it - Groq converts 0
# to 1e-8 rather than honouring it, so runs there are not identical and a
# regression test that assumes they are will flap.
DEFAULT_TEMPERATURE = 0.0

# A cap, not a target: its only job is that a long answer is never cut
# mid-sentence. Lower than the Claude answerer's 16000 because these
# backends are not one vendor - the thinnest free models top out at 8,192
# output tokens, and a request above a server's own ceiling is a 400,
# which would fall back to extractive for a reason that has nothing to do
# with the question asked. A preset with a lower ceiling (GitHub Models:
# 4,000) is clamped to it per link.
MAX_TOKENS = 4096

GEMINI_HOST = gateway_module.GEMINI_HOST
GEMINI_PATH = gateway_module.GEMINI_PATH

# The bench lengths and the 429 parsing live with the shared budget now.
DEFAULT_COOLDOWN_SECONDS = budget.DEFAULT_COOLDOWN_SECONDS
DAILY_COOLDOWN_SECONDS = budget.DAILY_COOLDOWN_SECONDS
OVERLOAD_COOLDOWN_SECONDS = budget.OVERLOAD_COOLDOWN_SECONDS
_cooldown_seconds = budget._cooldown_seconds
_retry_after = budget._retry_after
_without_credentials = gateway_module.without_credentials


def reset_cooldowns() -> None:
    """For tests: forget every remembered 429, exhaustion mark and count."""
    budget.reset()


class OpenAICompatibleAnswerer:
    """Answers with any OpenAI-compatible model, over the passages
    retrieval already authorized."""

    name = gateway_module.NAME
    generative = True

    def __init__(self):
        # Refuses an unknown provider or a custom endpoint with no model
        # (OpenAICompatibleNotConfigured), exactly as before chains: a typo
        # must not send documents to a service nobody chose.
        self.gateway = Gateway(links_from_settings())
        primary = self.gateway.primary

        # The first link, under the names this class has always had.
        self.provider = primary.provider
        self.base_url = primary.base_url
        self.model = primary.model
        self.key_env = primary.key_env
        self._placeholder_key = primary.placeholder_key
        self.trains_on_free_tier = primary.trains_on_input
        self.request_extras = dict(primary.request_extras)
        # The provider is part of the version, not decoration: the same
        # model id is served by more than one of these backends, at
        # different quantisations and different context limits, and two
        # answers recorded under one version string are not comparable.
        # An answer records version_for() its completion instead, which names
        # the link that actually answered.
        self.version = primary.version
        # Every link after the first, in the order they are tried.
        self.fallback_models = tuple(link.model for link in self.gateway.links[1:])

        temperature = getattr(settings, "RAG_ANSWERER_TEMPERATURE", None)
        self.temperature = (
            DEFAULT_TEMPERATURE if temperature in (None, "") else float(temperature)
        )

        # The extractive answerer is the fallback path, not a second
        # configuration to maintain: it needs nothing to work.
        self._fallback = ExtractiveAnswerer()

    # --- client ----------------------------------------------------------

    @property
    def _client(self):
        """A client injected for every link - how tests drive this class."""
        return self.gateway.client_override

    @_client.setter
    def _client(self, client):
        self.gateway.client_override = client

    def _api_key(self) -> str:
        """The first link's key, from the environment and nowhere else
        (see gateway.Link.api_key)."""
        return self.gateway.primary.api_key()

    def _get_client(self):
        """Build every configured link's client; return the first.

        Called at worker start-up (documents.processing.warmup) so the SDK
        import and client construction do not land on the first question.
        Building a client sends no request.
        """
        first, error = None, None
        for link in self.gateway.links:
            if not self.gateway.configured(link):
                continue
            try:
                client = self.gateway.client_for(link)
            except OpenAICompatibleNotConfigured as exc:
                error = error or exc
                continue
            first = first or client
        if first is None:
            raise error or self.gateway._not_configured()
        return first

    # --- answering -------------------------------------------------------

    def answer(self, *, question: str, context, prompt: str) -> LLMResponse:
        if context.is_empty:
            # Nothing retrieved. Asking a model to answer from no passages
            # is asking it to answer from memory, which is the one thing
            # this system must never do.
            return self._fallback.answer(
                question=question, context=context, prompt=prompt
            )

        trace = Trace()
        try:
            return self._generate(
                question=question, context=context, prompt=prompt, trace=trace
            )
        except Exception as exc:
            # Deliberately broad at the gateway boundary: a missing
            # package, a revoked key, an exhausted free-tier quota, a
            # network blip and a malformed 200 all mean the same thing to
            # a caller waiting for an answer, and none of them should
            # surface as a 500 when a grounded extractive answer is
            # available.
            # A stream that broke part-way is withdrawn: what it showed was
            # never checked, and the quoted answer replaces it.
            streaming.reset()
            capacity = gateway_module.is_capacity(exc)
            retry_at = getattr(exc, "retry_at", None)
            logger.warning(
                "llm.fallback",
                extra={
                    "provider": self.provider,
                    "model": self.model,
                    "error": type(exc).__name__,
                    "capacity": capacity,
                    "tried": trace.tried,
                    # The provider's own "come back in N seconds", when it
                    # sent one. On a free tier the 429 is the likeliest
                    # single cause of a fallback, and this is what
                    # separates a quota problem from an outage.
                    "retry_after": _retry_after(exc),
                },
            )
            response = self._fallback.answer(
                question=question, context=context, prompt=prompt
            )
            response.metadata = {
                **response.metadata,
                "fell_back_from": self.name,
                "fallback_provider": self.provider,
                "fallback_reason": type(exc).__name__,
                # "capacity" comes back by itself (a cooldown ends, the
                # quota day rolls over); "error" needs someone to look.
                "fallback_kind": "capacity" if capacity else "error",
                "fallback_retry_at": retry_at.isoformat() if retry_at else None,
                "attempts": list(trace.attempts),
            }
            response.fell_back_from = self.version
            # The question and its passages reached every link tried, even
            # the ones that then failed.
            response.may_train_on_input = any(
                link.trains_on_input
                for link in self.gateway.links
                if link.version in trace.tried
            )
            return response

    def _request_kwargs(self, prompt: str) -> dict:
        """The request for the first link; see _request_for."""
        return self._request_for(self.gateway.primary, prompt)

    def _request_for(self, link, prompt: str) -> dict:
        """The smallest request every one of these backends accepts.

        Nothing optional is sent. RAG_ANSWERER_EFFORT is not forwarded,
        and neither are seed, logit_bias or top_logprobs: some of these
        servers reject a parameter they do not implement with a 400, which
        would fall back to extractive on every single question, and others
        accept it with a 200 and ignore it, which makes a setting that
        does nothing look like a setting that works. Both are worse than
        not offering the knob.
        """
        return {
            "model": link.model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                # Passage markers live in the rendered prompt text, never
                # in a message `name` field: Groq rejects `name` outright,
                # and the marker has to be in the content for the model to
                # cite it anyway.
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            TOKEN_LIMIT_FIELD: link.token_cap(MAX_TOKENS),
            # Preset-specific parameters known to be accepted (Preset), for
            # THIS link's provider only.
            **dict(link.request_extras),
        }

    def _generate(
        self, *, question: str, context, prompt: str, trace: Trace | None = None
    ) -> LLMResponse:
        # No `from openai import OpenAI` here: the gateway owns that, so a
        # caller that injected its own client - which the tests do - never
        # needs the package installed at all.
        result = self.gateway.run(
            lambda link: self._request_for(link, prompt),
            purpose=gateway_module.current_purpose(),
            on_delta=streaming.emit if streaming.active() else None,
            on_reset=streaming.reset,
            trace=trace,
        )
        link = result.link
        finish_reason = result.finish_reason

        # Read defensively. Every backend in PRESETS reports usage on a
        # non-streaming completion, so a missing block means something was
        # already wrong - and guessing a token count would put an invented
        # number into the cost reporting, which is worse than a zero that
        # is visibly a zero. A streamed completion reports it only if the
        # server volunteers it in the last chunk.
        if finish_reason == "content_filter":
            # Institutional documents are dull, but a disciplinary
            # circular or a safety incident report is exactly the kind of
            # legitimate content a classifier declines on. An abstention
            # is the honest reply to that; it is not an error.
            logger.warning(
                "llm.refused",
                extra={"provider": link.provider, "model": link.model},
            )
            return self._abstained(
                prompt,
                reason="the model declined to answer this question",
                completion=result,
            )

        if finish_reason == "length":
            # Truncation, and not left to grounding.validate to catch: it
            # checks that the markers resolve and the figures are in the
            # cited passages, not that the last sentence was finished. An
            # answer that cites as it goes - which SYSTEM asks for - keeps
            # its earlier markers, so a cut that drops a condition passes.
            # Checked before the text because a reasoning model's thinking
            # counts toward the cap, and one that spent all of it thinking
            # returns no content: without this line that reads as a model
            # which would not answer rather than one not given room to.
            logger.warning(
                "llm.truncated",
                extra={
                    "provider": link.provider,
                    "model": link.model,
                    "max_tokens": link.token_cap(MAX_TOKENS),
                },
            )
            return self._abstained(
                prompt,
                reason="the model's answer was cut off at the output token limit",
                completion=result,
            )

        text = result.text.strip()

        if not text:
            return self._abstained(
                prompt, reason="the model returned no text", completion=result
            )

        response = gateway_module.response_from(
            result,
            text=text,
            name=self.name,
            prompt_characters=len(prompt),
            metadata={
                # Credentials stripped: this dict is stored on the answer and
                # served back over the API, and an internal gateway's base_url
                # can carry `user:password@` in it.
                "base_url": _without_credentials(link.base_url),
                "temperature": self.temperature,
                "purpose": gateway_module.current_purpose(),
            },
        )
        response.abstained = is_abstention(text)
        response.cited_markers = _markers(text)
        return response

    def version_for(self, completion: Completion) -> str:
        """The version to record for an answer this completion produced."""
        if completion.link is not None:
            return completion.link.version
        return f"{self.provider}/{completion.model or self.model}"

    def complete(self, request: dict) -> Completion:
        """One chat completion, streamed through documents.rag.streaming
        when a sink is installed and returned whole either way.

        `request` is a full chat.completions.create() payload. Naming the
        first link's model sends it down the whole chain, each link getting
        its own model and request extras; naming any other model sends it
        to that model alone, on the first link's endpoint. New code calls
        `self.gateway.complete(messages, purpose=...)` instead, which builds
        the request per link itself.
        """
        options = {
            "purpose": gateway_module.current_purpose(),
            "on_delta": streaming.emit if streaming.active() else None,
            "on_reset": streaming.reset,
        }
        if request.get("model") == self.model:
            return self.gateway.run(
                lambda link: self.gateway.adapt(request, link), **options
            )
        pinned = gateway_module.pinned(self.gateway.primary, request["model"])
        return self.gateway.run(lambda link: dict(request), pin=pinned, **options)

    def _abstained(
        self, prompt: str, *, reason: str, completion: Completion
    ) -> LLMResponse:
        usage = completion.usage
        link = completion.link
        return LLMResponse(
            text=ABSTENTION_TEXT,
            abstained=True,
            cited_markers=(),
            model_name=self.name,
            model_version=self.version_for(completion),
            generative=True,
            prompt_characters=len(prompt),
            completion_characters=len(ABSTENTION_TEXT),
            # The call was made and billed even though its reply is unusable;
            # the engine records usage for every response so cost is not
            # counted only on success, and a zero here would undo that.
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=completion.model,
            provider=link.provider if link else self.provider,
            finish_reason=completion.finish_reason,
            fell_back_from=completion.fell_back_from,
            may_train_on_input=bool(link and link.trains_on_input),
            metadata={
                "reason": reason,
                "provider": link.provider if link else self.provider,
                "model": completion.model or self.model,
            },
        )


def _markers(text: str) -> tuple:
    from .grounding import extract_markers

    return extract_markers(text)
