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
variable holding the key. Gemini, Groq, OpenRouter, a local Ollama and
Cerebras all answer the same `client.chat.completions.create(...)` call;
what differs between them is where it is sent.

WHY PRESETS RATHER THAN THREE FREEFORM SETTINGS. Because those strings are
unforgiving, and they take their revenge in another component's error
message. Gemini's compatibility endpoint is served only at
`/v1beta/openai/` - trailing slash included - and answers 404 on anything
else, while the SDK reports a 404 as a model problem. So the URL typo gets
debugged as a model-access problem, in a different console, by someone with
no reason to suspect the setting they typed. `PRESETS` holds the strings
that are known to be right, and `RAG_ANSWERER_PROVIDER=groq` is then the
whole configuration. An explicit RAG_ANSWERER_BASE_URL / RAG_ANSWERER_MODEL
still overrides a preset, because vLLM, LM Studio and an internal gateway
all speak this API too and none of them can be enumerated here.

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

WHICH BACKEND TO POINT IT AT. The binding constraint for this workload is
prompt SIZE, not request rate: a grounded-citation prompt is the system
rules plus five to twenty retrieved passages, routinely five to twenty
thousand tokens. That is what rules out the fast open-weight tiers - Groq's
free tier caps its gpt-oss models at 8K tokens per minute, so one long RAG
prompt is the entire minute - and it is why the default is Gemini Flash,
which takes the prompt in a single request. The second consideration is
that this is an instruction-following task rather than a knowledge one: a
weaker model here does not produce confidently wrong answers so much as
answers that miss markers and get downgraded, so the pipeline stays safe
and becomes useless.

DATA USE IS A CONFIGURATION FACT, SO IT IS RECORDED LIKE ONE. Google's
terms say that on unpaid tiers submitted content and the responses to it
are used to improve their products, and OpenRouter's free models may be
trained on by whichever provider served them. For institutional records
that may be disqualifying, and it is not something anyone discovers by
reading the code. So the presets carry the flag, the client logs it once on
construction, and every generated answer's metadata says which way it was
set. The privacy-clean options both work here unchanged: attach billing to
the Gemini key, or point at `ollama` and let nothing leave the machine.

FAILURE FALLS BACK RATHER THAN 500s, exactly as in `claude.py`. A missing
package, an unset key, an exhausted free-tier quota, a timeout and a
malformed 200 all mean the same thing to someone waiting for an answer, and
all of them degrade to the extractive answerer with `fell_back_from` set. A
quietly degraded answer that does not say so is worse than a slow one - and
on a free tier the 429 is not the exceptional case, it is Tuesday.

NOT RUN AGAINST ANY LIVE BACKEND from this repository - there were no keys
on the machine this was written on, and the `openai` package is not even
installed in its virtualenv. The request shape is the one every provider's
own quickstart shows, and the logic is written to be driven by a stubbed
client the way `tests_claude_answerer.py` drives its sibling, but treat the
first real call as the test.
"""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass

from django.conf import settings

from . import streaming

# Shared with the Claude answerer on purpose: this prompt encodes the
# citation discipline and the figure rule that documents.rag.grounding
# enforces afterwards, and two copies of a contract is one copy of a
# contract plus a bug waiting for the next edit. If it ever has to be
# copied instead of imported, the two must be changed together.
from .claude import SYSTEM
from .llm import ABSTENTION_TEXT, ExtractiveAnswerer, LLMResponse, is_abstention

logger = logging.getLogger("adi.llm")

GEMINI = "gemini"
GROQ = "groq"
OPENROUTER = "openrouter"
OLLAMA = "ollama"
CEREBRAS = "cerebras"


@dataclass(frozen=True)
class Preset:
    """The three strings that identify a backend, plus its caveats."""

    base_url: str
    model: str
    key_env: str

    # For a server that requires a key it does not use. The SDK refuses to
    # construct a client with an empty one, so "no auth" still needs a
    # value to send.
    placeholder_key: str = ""

    # Whether this backend's free tier may use what we send it to train on.
    # A property of the service rather than of any request, and it belongs
    # in the answer's metadata either way.
    trains_on_free_tier: bool = False

    # Tried in order when the configured model is rate-limited, overloaded
    # or retired. Free tiers meter each model separately, so the next model
    # usually has its whole quota left.
    fallback_models: tuple = ()

    # Optional request parameters this backend is KNOWN to accept. Only
    # per preset, never globally - see _request_kwargs for why.
    request_extras: tuple = ()


@dataclass
class Completion:
    """What one chat completion produced, streamed or not."""

    text: str
    finish_reason: str
    usage: object = None
    served_by: str = ""
    streamed: bool = False


PRESETS = {
    # The default. Flash takes a twenty-thousand-token grounded prompt in
    # one request, which the fast open-weight free tiers cannot, and it is
    # a frontier-lab instruction-tuned model - which is what the marker
    # rule actually asks for.
    #
    # 3.5-flash rather than the newest Flash on purpose: Google stopped
    # publishing per-model free quotas, and the newest line is reported at
    # a daily request count that an evaluation run exhausts in seconds.
    # Confirm the real number in AI Studio before sizing a batch job, and
    # move this with RAG_ANSWERER_MODEL rather than with a code change.
    #
    # The trailing "/v1beta/openai/" is load-bearing - see the module
    # docstring. Do not tidy it to "/v1/".
    GEMINI: Preset(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-3.5-flash",
        key_env="GEMINI_API_KEY",
        trains_on_free_tier=True,
        # Measured 2026-10-11 on a new free-tier key: 5 requests per minute
        # PER MODEL. gemini-2.5-flash and -flash-lite answer 404 ("no longer
        # available to new users"); the -lite 3.x models reject
        # reasoning_effort "none" with a 400. These two accepted it, first
        # streamed token 1.5 s and 5.8 s. Re-check before relying on them:
        # Google rotates this catalogue.
        # The free tier also caps each model at 20 requests PER DAY (measured
        # 2026-10-11: GenerateRequestsPerDayPerProjectPerModel-FreeTier), so
        # every model in the chain adds a day's allowance of its own.
        # gemini-3.1-flash-lite and -3.8-flash answered 503 at times that day;
        # a 503 benches a model for 30 s rather than removing it.
        fallback_models=(
            "gemini-3-flash-preview",
            "gemini-3.7-flash",
            "gemini-3.1-flash-lite",
            "gemini-3.8-flash",
        ),
        # Gemini 3 Flash "thinks" by default, and its hidden reasoning is
        # billed against max_tokens: at 200 tokens even "say hello" came
        # back cut off (finish_reason "length"). "none" also halved the
        # time to the first streamed word (3.4 s -> 1.6 s). This is
        # extraction over supplied passages, which needs no deliberation.
        request_extras=(("reasoning_effort", "none"),),
    ),
    # Very fast, and the wrong shape for this workload on the free tier:
    # gpt-oss is capped at 8K tokens per minute there, which a single long
    # retrieval prompt spends in one request. Useful for short questions
    # over a small context, or with a paid tier behind it.
    #
    # The llama-3.x ids are deliberately not the default: they are absent
    # from the free-tier table and one of them is already deprecated in
    # favour of this model.
    GROQ: Preset(
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-120b",
        key_env="GROQ_API_KEY",
    ),
    # A portability check rather than a production path: the same call,
    # many upstreams. The default is the free-model ROUTER rather than a
    # pinned ":free" id, because that catalogue rotates every few months
    # and a hard-coded id becomes a 404 without anyone touching this file.
    # The router picks per request, so `served_by` in the response
    # metadata is the only record of what actually answered.
    OPENROUTER: Preset(
        base_url="https://openrouter.ai/api/v1",
        model="openrouter/free",
        key_env="OPENROUTER_API_KEY",
        trains_on_free_tier=True,
    ),
    # The privacy answer: nothing leaves the machine, no key, no quota, at
    # the cost of local hardware and a weaker model than Gemini Flash. The
    # ":20b" is part of the model id, not a tier marker - it looks like
    # OpenRouter's ":free" suffix and has nothing to do with it, so do not
    # write suffix-parsing that spans the two.
    #
    # The hostname has no dot in it, which some HTTP middleware treats as
    # a typo and some upgrades to HTTPS. Neither is correct here.
    OLLAMA: Preset(
        base_url="http://localhost:11434/v1/",
        model="gpt-oss:20b",
        key_env="OLLAMA_API_KEY",
        placeholder_key="ollama",
    ),
    # A roomy per-minute token budget and very few requests per minute.
    # Set RAG_ANSWERER_RETRIES low here: at a 60 second timeout, the SDK's
    # own retries are enough to spend the minute's whole request allowance
    # on one question.
    CEREBRAS: Preset(
        base_url="https://api.cerebras.ai/v1",
        model="gpt-oss-120b",
        key_env="CEREBRAS_API_KEY",
    ),
}

DEFAULT_PROVIDER = GEMINI

# Where the key comes from when a custom base_url is configured with no
# preset behind it. It names an environment variable; it never holds one.
DEFAULT_KEY_ENV = "RAG_ANSWERER_API_KEY"

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
# with the question asked.
MAX_TOKENS = 4096

# `max_completion_tokens` is the newer first-party spelling, but these
# compatibility layers were built against `max_tokens` and not all of them
# accept the new one. Kept deliberately.
TOKEN_LIMIT_FIELD = "max_tokens"

GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_PATH = "/v1beta/openai/"


class OpenAICompatibleNotConfigured(RuntimeError):
    """The SDK is missing, or there is no usable base_url, model or key."""


class NoChoicesReturned(RuntimeError):
    """A 200 with no `choices` - a provider failure wearing a success.

    OpenRouter answers some upstream failures with HTTP 200 and a body
    containing only an `error` object. The SDK does not raise on that, so
    without this the first symptom is an IndexError inside an answerer,
    which sends whoever reads the traceback hunting for a bug in our code.
    """


class OpenAICompatibleAnswerer:
    """Answers with any OpenAI-compatible model, over the passages
    retrieval already authorized."""

    name = "openai-compatible"
    generative = True

    def __init__(self):
        named = (getattr(settings, "RAG_ANSWERER_PROVIDER", "") or "").strip().lower()
        base_url = (getattr(settings, "RAG_ANSWERER_BASE_URL", "") or "").strip()

        # A base URL with no provider name is an endpoint with no preset.
        # Defaulting the name to Gemini there handed the custom endpoint
        # Gemini's key variable (so GEMINI_API_KEY, if exported, was sent to
        # it as the bearer token), Gemini's "trains on free-tier input" flag
        # and a "gemini/..." version string.
        if base_url and not named:
            self.provider = "custom"
            preset = None
        else:
            self.provider = named or DEFAULT_PROVIDER
            preset = PRESETS.get(self.provider)

        model = (getattr(settings, "RAG_ANSWERER_MODEL", "") or "").strip()
        key_env = (getattr(settings, "RAG_ANSWERER_KEY_ENV", "") or "").strip()

        if preset is None and not base_url:
            # Refused rather than guessed at. A typo'd provider name
            # falling back to the default would send institutional
            # documents to a service nobody chose, and would do it
            # silently.
            raise OpenAICompatibleNotConfigured(
                f"Unknown RAG_ANSWERER_PROVIDER {self.provider!r}. Use one of "
                f"{', '.join(sorted(PRESETS))}, or set RAG_ANSWERER_BASE_URL "
                "and RAG_ANSWERER_MODEL for a server not listed here."
            )

        self.base_url = base_url or preset.base_url
        self.model = model or (preset.model if preset else "")

        if not self.model:
            raise OpenAICompatibleNotConfigured(
                "RAG_ANSWERER_BASE_URL is set but RAG_ANSWERER_MODEL is not. "
                "A custom endpoint has no default model to fall back to."
            )

        # The NAME of the variable, never its contents. See _api_key.
        self.key_env = key_env or (preset.key_env if preset else DEFAULT_KEY_ENV)
        self._placeholder_key = preset.placeholder_key if preset else ""

        # A custom endpoint is assumed not to train on what it is sent -
        # it is usually someone's own server - but an operator pointing at
        # a third party can say otherwise rather than having the answer
        # metadata claim something untrue.
        self.trains_on_free_tier = bool(
            getattr(settings, "RAG_ANSWERER_PROVIDER_TRAINS_ON_INPUT", False)
        ) or bool(preset and preset.trains_on_free_tier)

        temperature = getattr(settings, "RAG_ANSWERER_TEMPERATURE", None)
        self.temperature = (
            DEFAULT_TEMPERATURE if temperature in (None, "") else float(temperature)
        )

        # The provider is part of the version, not decoration: the same
        # model id is served by more than one of these backends, at
        # different quantisations and different context limits, and two
        # answers recorded under one version string are not comparable.
        self.version = f"{self.provider}/{self.model}"

        # RAG_ANSWERER_FALLBACK_MODELS overrides the preset's chain; "none"
        # turns it off. A chain only applies to the preset's own default
        # model: an operator who pinned RAG_ANSWERER_MODEL chose that model.
        configured = (
            getattr(settings, "RAG_ANSWERER_FALLBACK_MODELS", "") or ""
        ).strip()
        if configured.lower() == "none":
            chain = ()
        elif configured:
            chain = tuple(m.strip() for m in configured.split(",") if m.strip())
        else:
            chain = preset.fallback_models if (preset and not model) else ()
        self.fallback_models = tuple(m for m in chain if m != self.model)

        self.request_extras = dict(preset.request_extras) if preset else {}
        effort = (getattr(settings, "RAG_ANSWERER_REASONING_EFFORT", "") or "").strip()
        if effort.lower() == "default":
            self.request_extras.pop("reasoning_effort", None)
        elif effort:
            self.request_extras["reasoning_effort"] = effort

        _warn_if_base_url_looks_wrong(self.base_url)

        # The extractive answerer is the fallback path, not a second
        # configuration to maintain: it needs nothing to work.
        self._fallback = ExtractiveAnswerer()
        self._client = None

    # --- client ----------------------------------------------------------

    def _api_key(self) -> str:
        """The key, from the environment and nowhere else.

        Never read from Django settings, never logged, and never placed in
        response metadata. Settings get dumped whole by `diffsettings` and
        by more than one debug page; metadata is stored on the answer and
        rendered back over the API. A key that lives only in os.environ
        cannot leak through either of those.
        """
        key = os.environ.get(self.key_env, "").strip()
        if key:
            return key

        if self._placeholder_key:
            # Ollama documents its key as "required but ignored", and the
            # SDK refuses to construct a client without one. The literal
            # placeholder is the documented way through, not a workaround.
            return self._placeholder_key

        raise OpenAICompatibleNotConfigured(
            f"No API key for provider {self.provider!r}. Set {self.key_env} in "
            "the environment - it is read from there only, never from Django "
            "settings."
        )

    def _get_client(self):
        if self._client is not None:
            return self._client

        # Our own configuration is checked BEFORE the dependency, for the
        # reason the Claude answerer gives: both can be missing at once,
        # and "install the SDK" is the less useful of the two messages
        # when the real problem is a key nobody exported.
        api_key = self._api_key()

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise OpenAICompatibleNotConfigured(
                "The openai SDK is not installed. "
                "pip install -r backend/requirements-llm.txt"
            ) from exc

        timeout = float(getattr(settings, "RAG_ANSWERER_TIMEOUT", 60.0) or 60.0)
        retries = int(getattr(settings, "RAG_ANSWERER_RETRIES", 2) or 0)

        if self.trains_on_free_tier:
            # Once per client, at warning level, because the people who
            # need to see this read logs rather than this module - and
            # because it is a property of the deployment, not of any one
            # question. Every answer carries the same fact in its metadata.
            logger.warning(
                "llm.provider_may_train_on_content",
                extra={
                    "provider": self.provider,
                    "model": self.model,
                    "reason": "this backend's free tier may use submitted "
                    "content and responses to improve its products; use a "
                    "paid tier, or the local ollama preset, for documents "
                    "that must not be used that way",
                },
            )

        # base_url and api_key are ALWAYS passed explicitly. Omitted, the
        # SDK reads OPENAI_BASE_URL and OPENAI_API_KEY from the
        # environment, so one stale line in a compose file would redirect
        # every question to a different company with no error anywhere.
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=retries,
        )
        return self._client

    # --- answering -------------------------------------------------------

    def answer(self, *, question: str, context, prompt: str) -> LLMResponse:
        if context.is_empty:
            # Nothing retrieved. Asking a model to answer from no passages
            # is asking it to answer from memory, which is the one thing
            # this system must never do.
            return self._fallback.answer(
                question=question, context=context, prompt=prompt
            )

        try:
            return self._generate(question=question, context=context, prompt=prompt)
        except Exception as exc:
            # Deliberately broad at the gateway boundary: a missing
            # package, a revoked key, an exhausted free-tier quota, a
            # network blip and a malformed 200 all mean the same thing to
            # a caller waiting for an answer, and none of them should
            # surface as a 500 when a grounded extractive answer is
            # available.
            logger.warning(
                "llm.fallback",
                extra={
                    "provider": self.provider,
                    "model": self.model,
                    "error": type(exc).__name__,
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
            }
            return response

    def _request_kwargs(self, prompt: str) -> dict:
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
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                # Passage markers live in the rendered prompt text, never
                # in a message `name` field: Groq rejects `name` outright,
                # and the marker has to be in the content for the model to
                # cite it anyway.
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            TOKEN_LIMIT_FIELD: MAX_TOKENS,
            # Preset-specific parameters known to be accepted (Preset).
            **self.request_extras,
        }

    def _generate(self, *, question: str, context, prompt: str) -> LLMResponse:
        # No `from openai import OpenAI` here: _get_client owns that, so a
        # caller that injected its own client - which the tests do - never
        # needs the package installed at all.
        result = self.complete(self._request_kwargs(prompt))
        finish_reason = result.finish_reason

        # Read defensively. Every backend in PRESETS reports usage on a
        # non-streaming completion, so a missing block means something was
        # already wrong - and guessing a token count would put an invented
        # number into the cost reporting, which is worse than a zero that
        # is visibly a zero. A streamed completion reports it only if the
        # server volunteers it in the last chunk.
        usage = result.usage

        if finish_reason == "content_filter":
            # Institutional documents are dull, but a disciplinary
            # circular or a safety incident report is exactly the kind of
            # legitimate content a classifier declines on. An abstention
            # is the honest reply to that; it is not an error.
            logger.warning(
                "llm.refused",
                extra={"provider": self.provider, "model": self.model},
            )
            return self._abstained(
                prompt, reason="the model declined to answer this question", usage=usage
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
                    "provider": self.provider,
                    "model": self.model,
                    "max_tokens": MAX_TOKENS,
                },
            )
            return self._abstained(
                prompt,
                reason="the model's answer was cut off at the output token limit",
                usage=usage,
            )

        text = result.text.strip()

        if not text:
            return self._abstained(
                prompt, reason="the model returned no text", usage=usage
            )

        metadata = {
            "provider": self.provider,
            # Credentials stripped: this dict is stored on the answer and
            # served back over the API, and an internal gateway's base_url
            # can carry `user:password@` in it.
            "base_url": _without_credentials(self.base_url),
            "model": self.model,
            "temperature": self.temperature,
            "finish_reason": finish_reason,
            # Which model actually served it, which is not always the one
            # requested: a free-model router picks per request, and some
            # gateways substitute without saying so anywhere else.
            "served_by": result.served_by,
            "streamed": result.streamed,
            "provider_may_train_on_input": self.trains_on_free_tier,
        }

        # OpenRouter prices every response and reports it on the usage
        # block. Recorded when present rather than assumed, because no
        # other backend here sends it.
        cost = getattr(usage, "cost", None)
        if cost is not None:
            metadata["cost"] = cost

        return LLMResponse(
            text=text,
            abstained=is_abstention(text),
            cited_markers=_markers(text),
            model_name=self.name,
            model_version=self.version,
            generative=True,
            prompt_characters=len(prompt),
            completion_characters=len(text),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            metadata=metadata,
        )

    def complete(self, request: dict) -> "Completion":
        """One chat completion, streamed through documents.rag.streaming
        when a sink is installed and returned whole either way.

        `request` is a full chat.completions.create() payload. Shared by the
        grounded answer and the general-knowledge reply (documents.rag.general)
        so both get the same client, key handling and quirk handling.
        """
        # No `from openai import OpenAI` here: _get_client owns that, so a
        # caller that injected its own client - which the tests do - never
        # needs the package installed at all.
        client = self._get_client()
        models = [request["model"]]
        if request["model"] == self.model:
            models += list(self.fallback_models)
            # A model that answered 429 a moment ago will answer 429 again:
            # try it last, not first. Never skipped outright - if every model
            # is cooling, the original order stands.
            models.sort(
                key=lambda m: _cooling_until(self.base_url, m) > time.monotonic()
            )

        for index, model in enumerate(models):
            last = index == len(models) - 1
            try:
                # The SDK's own retries honour Retry-After - 14 s on a
                # free-tier 429 - so they are kept only for the LAST model.
                # Before that, the next model is the faster retry.
                attempt_client = client if last else _without_retries(client)
                return self._complete_once(attempt_client, {**request, "model": model})
            except Exception as exc:
                if _status(exc) in (429, 503):
                    _cool(self.base_url, model, exc)
                if last or _status(exc) not in MODEL_SPECIFIC_FAILURES:
                    raise
                logger.warning(
                    "llm.model_unavailable",
                    extra={
                        "provider": self.provider,
                        "model": model,
                        "status": _status(exc),
                        "next_model": models[index + 1],
                        "retry_after": _retry_after(exc),
                    },
                )

    def _complete_once(self, client, request: dict) -> "Completion":
        if not streaming.active():
            completion = client.chat.completions.create(**request)
            choices = getattr(completion, "choices", None)
            if not choices:
                self._no_choices(completion)
            choice = choices[0]
            message = getattr(choice, "message", None)
            return Completion(
                # Typed `str | None`, and genuinely None when the model
                # emitted only tool calls or was cut off by a filter.
                text=getattr(message, "content", None) or "",
                finish_reason=getattr(choice, "finish_reason", "") or "",
                usage=getattr(completion, "usage", None),
                served_by=getattr(completion, "model", "") or request["model"],
            )

        # Streamed. No `stream_options`: some of these servers 400 on a
        # parameter they do not implement (see _request_kwargs), and usage
        # is worth less than an answer that arrives at all.
        parts, finish_reason, usage, served_by, last = [], "", None, "", None
        for chunk in client.chat.completions.create(**request, stream=True):
            last = chunk
            usage = getattr(chunk, "usage", None) or usage
            served_by = getattr(chunk, "model", "") or served_by
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            piece = getattr(delta, "content", None) or ""
            if piece:
                parts.append(piece)
                streaming.emit(piece)
            finish_reason = getattr(choices[0], "finish_reason", None) or finish_reason
        if not parts and not finish_reason:
            # The streamed form of OpenRouter's HTTP 200 with an `error`
            # body and no choices.
            self._no_choices(last)
        return Completion(
            text="".join(parts),
            finish_reason=finish_reason,
            usage=usage,
            served_by=served_by or request["model"],
            streamed=True,
        )

    def _no_choices(self, completion):
        error = _embedded_error(completion)
        logger.warning(
            "llm.no_choices",
            extra={"provider": self.provider, "error": error},
        )
        raise NoChoicesReturned(
            f"{self.provider} returned a success with no choices ({error})"
        )

    def _abstained(self, prompt: str, *, reason: str, usage=None) -> LLMResponse:
        return LLMResponse(
            text=ABSTENTION_TEXT,
            abstained=True,
            cited_markers=(),
            model_name=self.name,
            model_version=self.version,
            generative=True,
            prompt_characters=len(prompt),
            completion_characters=len(ABSTENTION_TEXT),
            # The call was made and billed even though its reply is unusable;
            # the engine records usage for every response so cost is not
            # counted only on success, and a zero here would undo that.
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            metadata={
                "reason": reason,
                "provider": self.provider,
                "model": self.model,
            },
        )


# Failures of one MODEL rather than of the account or the request: rate
# limited (429), retired (404), overloaded (503) or erroring (500/502/504).
# A bad key (401/403) or a bad request (400) fails every model alike.
MODEL_SPECIFIC_FAILURES = frozenset({404, 429, 500, 502, 503, 504})


# Rate-limited models, remembered across calls and threads: (base_url,
# model) -> time.monotonic() deadline. Without it every question paid a
# doomed 429 on the throttled model before switching (seen live: 3 of 7
# calls in one session).
_cooling = {}
_cooling_lock = threading.Lock()
DEFAULT_COOLDOWN_SECONDS = 60.0
DAILY_COOLDOWN_SECONDS = 3600.0
OVERLOAD_COOLDOWN_SECONDS = 30.0
_RETRY_IN = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)\s*s", re.I)


def _cooldown_seconds(exc) -> float:
    """How long a 429 should bench its model. Never raises.

    Gemini sends no Retry-After header; it puts "Please retry in 14.5s" and
    a quota id in the body. A per-DAY quota will not lift in a minute.
    """
    try:
        if _status(exc) == 503:
            # Overloaded, not rate-limited: usually seconds, so a short bench.
            return OVERLOAD_COOLDOWN_SECONDS
        text = str(exc)
        if "PerDay" in text:
            return DAILY_COOLDOWN_SECONDS
        match = _RETRY_IN.search(text)
        if match:
            return max(1.0, float(match.group(1)))
        header = _retry_after(exc)
        if header:
            # Seconds, or an HTTP date (RFC 9110) - which float() refuses.
            return max(1.0, float(header))
    except (TypeError, ValueError):
        pass
    return DEFAULT_COOLDOWN_SECONDS


def _cool(base_url, model, exc) -> None:
    with _cooling_lock:
        _cooling[(base_url, model)] = time.monotonic() + _cooldown_seconds(exc)


def _cooling_until(base_url, model) -> float:
    with _cooling_lock:
        return _cooling.get((base_url, model), 0.0)


def reset_cooldowns() -> None:
    """For tests: forget every remembered 429."""
    with _cooling_lock:
        _cooling.clear()


def _status(exc) -> int | None:
    return getattr(exc, "status_code", None)


def _without_retries(client):
    """The same client with SDK retries off; stubs without with_options pass through."""
    with_options = getattr(client, "with_options", None)
    return with_options(max_retries=0) if with_options else client


def _warn_if_base_url_looks_wrong(base_url: str) -> None:
    """Name the one base_url mistake that reports itself as another fault.

    Gemini's compatibility endpoint is served only at `/v1beta/openai/`,
    trailing slash included. Every other path on that host answers 404,
    and the SDK surfaces a 404 as a model that does not exist - so the URL
    typo gets investigated as a model-access problem, in a console that
    will say the access is fine.
    """
    if GEMINI_HOST not in base_url or base_url.endswith(GEMINI_PATH):
        return

    logger.warning(
        "llm.suspect_base_url",
        extra={
            # Stripped for the reason the answer metadata strips it: an
            # internal gateway's URL can carry `user:password@`, and the
            # formatter does not redact a field with this name.
            "base_url": _without_credentials(base_url),
            "expected_suffix": GEMINI_PATH,
            "reason": "Gemini's OpenAI-compatible API answers 404 on any "
            "other path, and a 404 reads as a model that does not exist",
        },
    )


def _without_credentials(url: str) -> str:
    """The URL with any `user:password@` userinfo removed."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit(parts._replace(netloc=host))


def _embedded_error(completion) -> str:
    """The error type from a 200 that carried an error instead of an answer.

    The normalized type or code only, never the upstream's free-text
    message: that message is written by whichever provider a router
    happened to pick, differs between them for the same fault, and is
    attacker-adjacent text to be logging verbatim. The type is the part
    that is actionable.
    """
    extra = getattr(completion, "model_extra", None) or {}
    error = extra.get("error") if isinstance(extra, dict) else None
    if not isinstance(error, dict):
        return "no error object in the body either"

    metadata = error.get("metadata")
    kind = metadata.get("error_type") if isinstance(metadata, dict) else None
    return str(kind or error.get("code") or "unspecified")


def _retry_after(exc) -> str:
    """The provider's Retry-After header, when the failure carried one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    return str(getter("retry-after", "") or "")


def _markers(text: str) -> tuple:
    from .grounding import extract_markers

    return extract_markers(text)
