"""The LLM gateway: every generative call in the system goes through here.

Generative AI is the scarcest resource this product has. On the free tiers it
runs on, a day is a few hundred requests across every user, and when they are
gone answers drop to quoting passages. So nothing calls a model directly: a
feature asks THIS module for a completion, says what it is for, and the
gateway decides whether and where to spend a request.

    complete(messages, *, purpose, max_tokens, temperature=0.2,
             json_mode=False, on_delta=None) -> LLMResponse
        raises Unavailable(reason, retry_at) when nothing can serve it now
    available(purpose) -> bool      cheap; no network
    status() -> dict                for the admin panel; no keys, no URLs

A CHAIN OF LINKS. A link is one model at one provider ("groq:openai/gpt-oss-
120b"), with that provider's base URL, key variable and request quirks
(documents.rag.providers). RAG_ANSWERER_CHAIN lists them in order of
preference; without it the chain is the single configured provider and its
fallback models, exactly what the answerer used before chains existed. A link
whose key is not set is `unconfigured` and skipped. Clients are built lazily,
one per endpoint.

WALKING IT. Failures belong to different things, and each moves the walk on
differently:

  429, 503      the link is busy: bench it (documents.rag.budget), try the next
  400, 404, 422 this model will not take this request: try the next
  500-504       this attempt failed: try the next
  401, 403      the ACCOUNT is refused: skip every link on that endpoint, but
                another provider's links still get their turn
  no status     the endpoint is unreachable or broke mid-reply: likewise

Only when every link has failed does the caller see an error. The SDK's own
retries - which honour Retry-After, 14 s on a free-tier 429 - are kept only
for the last link tried; before it, the next link is the faster retry.

PURPOSES. `chat` is someone waiting for an answer and may use everything,
including links that are cooling down (a bench is an estimate, and those are
tried after the ready ones rather than not at all). Everything else waits its
turn: `brief` (document summaries on demand), `grid` (multi-document
extraction) and `other` may spend only while the calls left today exceed
LLM_CHAT_RESERVE times 1, 2 and 3 respectively, never on a link that is
cooling down, so chat always keeps the last calls of the day.

STREAMING. With `on_delta`, text is handed over as the provider sends it.
What a link streamed before failing is withdrawn with `on_reset` before the
next link starts; a caller that passes no `on_reset` cannot withdraw it, so
for that caller a mid-stream failure ends the walk instead.
"""

import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from django.conf import settings

from . import budget
from .llm import LLMResponse
from .providers import (
    CUSTOM,
    DEFAULT_KEY_ENV,
    DEFAULT_PROVIDER,
    PRESETS,
    display_name,
)
from .providers import (
    UTC as UTC_ZONE,
)

logger = logging.getLogger("adi.llm")

CHAT = "chat"
BRIEF = "brief"
GRID = "grid"
OTHER = "other"

# How many chat reserves each purpose must leave untouched. Chat leaves
# none: it is the reason the reserve exists.
PURPOSE_RESERVE_MULTIPLIER = {CHAT: 0, BRIEF: 1, GRID: 2, OTHER: 3}
PURPOSES = tuple(PURPOSE_RESERVE_MULTIPLIER)

# Link states, as status() reports them.
READY = "ready"
COOLING = "cooling"
EXHAUSTED = "exhausted_today"
UNCONFIGURED = "unconfigured"

# Unavailable.reason values.
NOT_CONFIGURED = "not_configured"
NO_CAPACITY = "exhausted"
ALL_COOLING = "cooling"
RESERVED = "reserved_for_chat"
FAILED = "failed"

# `max_completion_tokens` is the newer first-party spelling, but these
# compatibility layers were built against `max_tokens` and not all of them
# accept the new one. Kept deliberately.
TOKEN_LIMIT_FIELD = "max_tokens"

# The same chat-completions API under a different name in the metrics: the
# answerer's, so dashboards keyed on it keep counting.
NAME = "openai-compatible"

GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_PATH = "/v1beta/openai/"


class Unavailable(RuntimeError):
    """Nothing can serve this purpose now.

    `reason` is one of the module's constants; `retry_at` is when capacity is
    expected back (a cooldown ending, a quota day rolling over), or None when
    it will not come back by waiting - nothing is configured, or every link
    failed.
    """

    def __init__(self, reason: str, retry_at: datetime | None = None, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.retry_at = retry_at


class OpenAICompatibleNotConfigured(Unavailable):
    """The SDK is missing, or there is no usable base_url, model or key."""

    def __init__(self, detail: str):
        super().__init__(NOT_CONFIGURED, None, detail)


class NoChoicesReturned(RuntimeError):
    """A 200 with no `choices` - a provider failure wearing a success.

    OpenRouter answers some upstream failures with HTTP 200 and a body
    containing only an `error` object. The SDK does not raise on that, so
    without this the first symptom is an IndexError inside an answerer,
    which sends whoever reads the traceback hunting for a bug in our code.
    """


@dataclass(frozen=True)
class Link:
    """One model at one provider."""

    provider: str
    model: str
    base_url: str
    key_env: str
    placeholder_key: str = ""
    trains_on_input: bool = False
    request_extras: tuple = ()
    daily_limit: int = 0
    quota_tz: str = UTC_ZONE
    max_output_tokens: int = 0
    json_object: bool = False

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def version(self) -> str:
        """What an answer records: "provider/model", as model_version always
        has. The provider is part of it because the same model id is served
        by several of these backends, at different quantisations."""
        return f"{self.provider}/{self.model}"

    @property
    def endpoint(self) -> tuple:
        return (self.base_url, self.key_env)

    def has_key(self) -> bool:
        return bool(os.environ.get(self.key_env, "").strip() or self.placeholder_key)

    def api_key(self) -> str:
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

        if self.placeholder_key:
            # Ollama documents its key as "required but ignored", and the
            # SDK refuses to construct a client without one. The literal
            # placeholder is the documented way through, not a workaround.
            return self.placeholder_key

        raise OpenAICompatibleNotConfigured(
            f"No API key for provider {self.provider!r}. Set {self.key_env} in "
            "the environment - it is read from there only, never from Django "
            "settings."
        )

    def token_cap(self, requested: int) -> int:
        if self.max_output_tokens and requested > self.max_output_tokens:
            return self.max_output_tokens
        return requested


@dataclass
class Completion:
    """What one chat completion produced, streamed or not."""

    text: str
    finish_reason: str
    usage: object = None
    served_by: str = ""
    streamed: bool = False
    # The model the request named: in a fallback chain, the one that answered.
    model: str = ""
    # The link that answered, and the first link of the chain when another
    # one answered in its place ("provider/model").
    link: Link | None = None
    fell_back_from: str = ""


@dataclass
class Trace:
    """What a walk tried, for a caller that has to explain a failure."""

    attempts: list = field(default_factory=list)

    def failed(self, link: Link, exc) -> None:
        self.attempts.append(
            {
                "link": link.version,
                "error": type(exc).__name__,
                "status": budget._status(exc),
            }
        )

    @property
    def tried(self) -> list:
        return [attempt["link"] for attempt in self.attempts]


@dataclass
class Plan:
    links: list
    unavailable: Unavailable | None = None


_purpose: ContextVar = ContextVar("llm_purpose", default=CHAT)


@contextmanager
def purpose(name: str):
    """Spend what runs inside the block under `name` rather than chat.

    For a caller that reaches the model through the RAG engine rather than
    through complete() - a batch job asking questions, say - and should not
    spend the calls chat is holding back.
    """
    _check_purpose(name)
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def current_purpose() -> str:
    return _purpose.get()


def _check_purpose(name: str) -> None:
    if name not in PURPOSE_RESERVE_MULTIPLIER:
        raise ValueError(f"Unknown LLM purpose {name!r}; use one of {PURPOSES}")


def _status(exc) -> int | None:
    return budget._status(exc)


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def is_capacity(exc) -> bool:
    """Whether a failure means "at capacity" rather than "broken".

    The difference a reader cares about: capacity comes back by itself
    (a cooldown ends, the quota day rolls over); a fault needs someone.
    """
    if isinstance(exc, Unavailable):
        if exc.reason in (NO_CAPACITY, ALL_COOLING, RESERVED):
            return True
        cause = exc.__cause__
        return cause is not None and _status(cause) in (429, 503)
    return _status(exc) in (429, 503)


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
            "base_url": without_credentials(base_url),
            "expected_suffix": GEMINI_PATH,
            "reason": "Gemini's OpenAI-compatible API answers 404 on any "
            "other path, and a 404 reads as a model that does not exist",
        },
    )


def without_credentials(url: str) -> str:
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


# --- Building the chain from settings -----------------------------------------


def _setting(name: str) -> str:
    return (getattr(settings, name, "") or "").strip()


def _extras(preset, *, explicit_chain: bool) -> tuple:
    """A preset's request extras, with RAG_ANSWERER_REASONING_EFFORT applied.

    In a single-provider configuration the override applies as it always
    has. In an explicit chain it only changes links whose preset already
    sends `reasoning_effort`: providers that do not implement it answer a
    400, and one global knob must not break half the chain.
    """
    extras = dict(preset.request_extras) if preset else {}
    effort = _setting("RAG_ANSWERER_REASONING_EFFORT")
    if effort.lower() == "default":
        extras.pop("reasoning_effort", None)
    elif effort and (not explicit_chain or "reasoning_effort" in extras):
        extras["reasoning_effort"] = effort
    return tuple(extras.items())


def _link(provider, model, base_url, key_env, preset, *, trains, extras) -> Link:
    return Link(
        provider=provider,
        model=model,
        base_url=base_url,
        key_env=key_env,
        placeholder_key=preset.placeholder_key if preset else "",
        trains_on_input=trains,
        request_extras=extras,
        daily_limit=budget.daily_limit_for(
            provider, model, preset.daily_limit if preset else 0
        ),
        quota_tz=preset.quota_tz if preset else UTC_ZONE,
        max_output_tokens=preset.max_output_tokens if preset else 0,
        json_object=preset.json_object if preset else False,
    )


def _single_provider_links() -> tuple:
    """The chain RAG_ANSWERER_PROVIDER has always meant: one provider, its
    configured model, then that provider's fallback models."""
    named = _setting("RAG_ANSWERER_PROVIDER").lower()
    base_url = _setting("RAG_ANSWERER_BASE_URL")

    # A base URL with no provider name is an endpoint with no preset.
    # Defaulting the name to Gemini there handed the custom endpoint
    # Gemini's key variable (so GEMINI_API_KEY, if exported, was sent to
    # it as the bearer token), Gemini's "trains on free-tier input" flag
    # and a "gemini/..." version string.
    if base_url and not named:
        provider = CUSTOM
        preset = None
    else:
        provider = named or DEFAULT_PROVIDER
        preset = PRESETS.get(provider)

    model = _setting("RAG_ANSWERER_MODEL")
    key_env = _setting("RAG_ANSWERER_KEY_ENV")

    if preset is None and not base_url:
        # Refused rather than guessed at. A typo'd provider name
        # falling back to the default would send institutional
        # documents to a service nobody chose, and would do it
        # silently.
        raise OpenAICompatibleNotConfigured(
            f"Unknown RAG_ANSWERER_PROVIDER {provider!r}. Use one of "
            f"{', '.join(sorted(PRESETS))}, or set RAG_ANSWERER_BASE_URL "
            "and RAG_ANSWERER_MODEL for a server not listed here."
        )

    base_url = base_url or preset.base_url
    primary = model or (preset.model if preset else "")
    if not primary:
        raise OpenAICompatibleNotConfigured(
            "RAG_ANSWERER_BASE_URL is set but RAG_ANSWERER_MODEL is not. "
            "A custom endpoint has no default model to fall back to."
        )

    # The NAME of the variable, never its contents. See Link.api_key.
    key_env = key_env or (preset.key_env if preset else DEFAULT_KEY_ENV)

    # A custom endpoint is assumed not to train on what it is sent - it is
    # usually someone's own server - but an operator pointing at a third
    # party can say otherwise rather than having the answer metadata claim
    # something untrue.
    trains = bool(
        getattr(settings, "RAG_ANSWERER_PROVIDER_TRAINS_ON_INPUT", False)
    ) or bool(preset and preset.trains_on_free_tier)

    # RAG_ANSWERER_FALLBACK_MODELS overrides the preset's chain; "none"
    # turns it off. A chain only applies to the preset's own default
    # model: an operator who pinned RAG_ANSWERER_MODEL chose that model.
    configured = _setting("RAG_ANSWERER_FALLBACK_MODELS")
    if configured.lower() == "none":
        fallbacks = ()
    elif configured:
        fallbacks = tuple(m.strip() for m in configured.split(",") if m.strip())
    else:
        fallbacks = preset.fallback_models if (preset and not model) else ()

    extras = _extras(preset, explicit_chain=False)
    _warn_if_base_url_looks_wrong(base_url)
    models = [primary, *(m for m in fallbacks if m != primary)]
    return tuple(
        _link(provider, m, base_url, key_env, preset, trains=trains, extras=extras)
        for m in dict.fromkeys(models)
    )


def _explicit_links(chain: str) -> tuple:
    """RAG_ANSWERER_CHAIN: "gemini, groq:openai/gpt-oss-120b, custom:llama".

    `provider:model` is one link. A bare `provider` is its preset's model
    and fallback models. `custom` is RAG_ANSWERER_BASE_URL. Split at the
    FIRST colon only: "ollama:gpt-oss:20b" is Ollama's "gpt-oss:20b".
    """
    links = []
    for item in chain.split(","):
        item = item.strip()
        if not item:
            continue
        provider, _, model = item.partition(":")
        provider, model = provider.strip().lower(), model.strip()

        if provider == CUSTOM:
            base_url = _setting("RAG_ANSWERER_BASE_URL")
            model = model or _setting("RAG_ANSWERER_MODEL")
            if not base_url or not model:
                raise OpenAICompatibleNotConfigured(
                    "RAG_ANSWERER_CHAIN names `custom`, which needs "
                    "RAG_ANSWERER_BASE_URL and a model (custom:<model>)."
                )
            _warn_if_base_url_looks_wrong(base_url)
            links.append(
                _link(
                    CUSTOM,
                    model,
                    base_url,
                    _setting("RAG_ANSWERER_KEY_ENV") or DEFAULT_KEY_ENV,
                    None,
                    trains=bool(
                        getattr(
                            settings, "RAG_ANSWERER_PROVIDER_TRAINS_ON_INPUT", False
                        )
                    ),
                    extras=(),
                )
            )
            continue

        preset = PRESETS.get(provider)
        if preset is None:
            raise OpenAICompatibleNotConfigured(
                f"RAG_ANSWERER_CHAIN names an unknown provider {provider!r}. Use "
                f"one of {', '.join(sorted(PRESETS))}, or `custom` with "
                "RAG_ANSWERER_BASE_URL."
            )
        extras = _extras(preset, explicit_chain=True)
        models = [model] if model else [preset.model, *preset.fallback_models]
        for name in models:
            links.append(
                _link(
                    provider,
                    name,
                    preset.base_url,
                    preset.key_env,
                    preset,
                    trains=preset.trains_on_free_tier,
                    extras=extras,
                )
            )

    if not links:
        raise OpenAICompatibleNotConfigured("RAG_ANSWERER_CHAIN names no links.")
    # The first mention of a link wins; a repeat would only be tried twice.
    unique = {}
    for link in links:
        unique.setdefault(link.id, link)
    return tuple(unique.values())


def links_from_settings() -> tuple:
    chain = _setting("RAG_ANSWERER_CHAIN")
    return _explicit_links(chain) if chain else _single_provider_links()


# --- The gateway -----------------------------------------------------------------


class Gateway:
    """A chain of links, the shared budget, and one client per endpoint."""

    name = NAME

    def __init__(self, links=(), *, broker=None):
        self.links = tuple(links)
        self.broker = broker or budget.broker
        # Every request-extra key any link sends, so a request built for one
        # link can be cleaned of them before it goes to another.
        self.extra_keys = frozenset(
            key for link in self.links for key, _ in link.request_extras
        )
        self._clients = {}
        self._clients_lock = threading.Lock()
        # One client standing in for every link: how the tests drive the
        # chain without a network, as `answerer._client = stub` always has.
        self.client_override = None
        self._timeout = float(getattr(settings, "RAG_ANSWERER_TIMEOUT", 60.0) or 60.0)
        self._retries = int(getattr(settings, "RAG_ANSWERER_RETRIES", 2) or 0)

    @classmethod
    def from_settings(cls) -> "Gateway":
        return cls(links_from_settings())

    @property
    def primary(self) -> Link | None:
        return self.links[0] if self.links else None

    # -- clients ---------------------------------------------------------

    def configured(self, link: Link) -> bool:
        return (
            self.client_override is not None
            or link.endpoint in self._clients
            or link.has_key()
        )

    def client_for(self, link: Link):
        if self.client_override is not None:
            return self.client_override
        client = self._clients.get(link.endpoint)
        if client is not None:
            return client

        # Our own configuration is checked BEFORE the dependency, for the
        # reason the Claude answerer gives: both can be missing at once,
        # and "install the SDK" is the less useful of the two messages
        # when the real problem is a key nobody exported.
        api_key = link.api_key()

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise OpenAICompatibleNotConfigured(
                "The openai SDK is not installed. "
                "pip install -r backend/requirements-llm.txt"
            ) from exc

        with self._clients_lock:
            client = self._clients.get(link.endpoint)
            if client is not None:
                return client
            if link.trains_on_input:
                # Once per client, at warning level, because the people who
                # need to see this read logs rather than this module - and
                # because it is a property of the deployment, not of any one
                # question. Every answer carries the same fact in its metadata.
                logger.warning(
                    "llm.provider_may_train_on_content",
                    extra={
                        "provider": link.provider,
                        "model": link.model,
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
            client = OpenAI(
                base_url=link.base_url,
                api_key=api_key,
                timeout=self._timeout,
                max_retries=self._retries,
            )
            self._clients[link.endpoint] = client
            return client

    # -- state -------------------------------------------------------------

    def _state(self, link: Link, state: budget.LinkState, now: float) -> str:
        if not self.configured(link):
            return UNCONFIGURED
        if state.exhausted_until > now or (
            link.daily_limit and state.calls_today >= link.daily_limit
        ):
            return EXHAUSTED
        if state.cooling_until > now:
            return COOLING
        return READY

    def _not_configured(self) -> Unavailable:
        if not self.links:
            return OpenAICompatibleNotConfigured(
                "No generative model is configured. Set RAG_ANSWERER to "
                "documents.rag.openai_compat.OpenAICompatibleAnswerer and a "
                "provider's API key."
            )
        try:
            self.primary.api_key()
        except OpenAICompatibleNotConfigured as exc:
            return exc
        return OpenAICompatibleNotConfigured("No link in the chain has a key.")

    def plan(self, purpose: str, *, now: float | None = None) -> Plan:
        """The links a call for `purpose` may try now, in order."""
        _check_purpose(purpose)
        now = budget._now() if now is None else now
        states = self.broker.states(self.links, now)
        labelled = [
            (link, self._state(link, states[link.id], now)) for link in self.links
        ]

        configured = [(link, s) for link, s in labelled if s != UNCONFIGURED]
        if not configured:
            return Plan([], self._not_configured())

        usable = [(link, s) for link, s in configured if s != EXHAUSTED]
        if not usable:
            return Plan(
                [],
                Unavailable(
                    NO_CAPACITY,
                    min(
                        budget.next_reset(link.quota_tz, now) for link, _ in configured
                    ),
                    "Every model has used today's allowance.",
                ),
            )

        if purpose == CHAT:
            # A model benched a moment ago will probably refuse again: try it
            # last, not first. Never skipped outright - a bench is an
            # estimate, and if every model is cooling the order stands.
            return Plan(
                [link for link, s in usable if s == READY]
                + [link for link, s in usable if s == COOLING]
            )

        headroom = budget.chat_reserve() * PURPOSE_RESERVE_MULTIPLIER[purpose]
        remaining = sum(
            max(0, link.daily_limit - states[link.id].calls_today)
            for link, _ in usable
            if link.daily_limit
        )
        eligible = [
            (link, s)
            for link, s in usable
            if not link.daily_limit or remaining > headroom
        ]
        if not eligible:
            return Plan(
                [],
                Unavailable(
                    RESERVED,
                    min(budget.next_reset(link.quota_tz, now) for link, _ in usable),
                    "Today's remaining capacity is held back for chat.",
                ),
            )
        ready = [link for link, s in eligible if s == READY]
        if not ready:
            return Plan(
                [],
                Unavailable(
                    ALL_COOLING,
                    datetime.fromtimestamp(
                        min(states[link.id].cooling_until for link, _ in eligible), UTC
                    ),
                    "Every model is cooling down after a rate limit.",
                ),
            )
        return Plan(ready)

    def available(self, purpose: str) -> bool:
        """Whether a call for `purpose` is expected to succeed now. No network."""
        _check_purpose(purpose)
        now = budget._now()
        plan = self.plan(purpose, now=now)
        if not plan.links:
            return False
        states = self.broker.states(plan.links, now)
        return any(
            self._state(link, states[link.id], now) == READY for link in plan.links
        )

    def resumes_at(self, *, now: float | None = None) -> datetime | None:
        """When a link is next expected to be ready, if none is now."""
        now = budget._now() if now is None else now
        states = self.broker.states(self.links, now)
        moments = []
        for link in self.links:
            state = states[link.id]
            label = self._state(link, state, now)
            if label == READY:
                return None
            if label == COOLING:
                moments.append(datetime.fromtimestamp(state.cooling_until, UTC))
            elif label == EXHAUSTED:
                moments.append(
                    datetime.fromtimestamp(state.exhausted_until, UTC)
                    if state.exhausted_until > now
                    else budget.next_reset(link.quota_tz, now)
                )
        return min(moments) if moments else None

    # -- the walk ------------------------------------------------------------

    def adapt(self, request: dict, link: Link) -> dict:
        """A request built for one link, made right for another: its model,
        its own request extras and nobody else's, its output-token ceiling."""
        adapted = {k: v for k, v in request.items() if k not in self.extra_keys}
        adapted["model"] = link.model
        adapted.update(link.request_extras)
        if TOKEN_LIMIT_FIELD in adapted:
            adapted[TOKEN_LIMIT_FIELD] = link.token_cap(adapted[TOKEN_LIMIT_FIELD])
        return adapted

    def run(
        self,
        build,
        *,
        purpose: str = CHAT,
        on_delta=None,
        on_reset=None,
        trace: Trace | None = None,
        pin: Link | None = None,
    ) -> Completion:
        """One completion: `build(link)` makes each link's request.

        Raises the LAST link's error when every link failed, or Unavailable
        when no link was eligible to try. `pin` sends to that one link only,
        whatever its state - an explicit probe, not a question.
        """
        if pin is not None:
            plan = Plan([pin])
        else:
            plan = self.plan(purpose)
            if not plan.links:
                raise plan.unavailable

        dead = set()
        last_error = None
        shown = False  # text this walk streamed and has not withdrawn

        for index, link in enumerate(plan.links):
            if link.endpoint in dead:
                continue
            remaining = [
                later for later in plan.links[index + 1 :] if later.endpoint not in dead
            ]
            last = not remaining

            try:
                client = self.client_for(link)
            except OpenAICompatibleNotConfigured as exc:
                # No key after all (removed since the plan), or no SDK: nothing
                # on this endpoint can be reached.
                dead.add(link.endpoint)
                last_error = exc
                if trace is not None:
                    trace.failed(link, exc)
                if last:
                    raise
                continue

            reservation = self.broker.reserve(link)
            if (
                pin is None
                and link.daily_limit
                and reservation.count > link.daily_limit
            ):
                # Another process spent the last call of the day between the
                # plan and now. Not sent, so not counted.
                self.broker.refund(reservation)
                last_error = Unavailable(
                    NO_CAPACITY,
                    budget.next_reset(link.quota_tz, budget._now()),
                    f"{link.id} has used today's allowance.",
                )
                if last:
                    raise last_error
                continue

            if shown and on_reset is not None:
                on_reset()
                shown = False

            emitted = []

            def deliver(piece, _emitted=emitted):
                _emitted.append(piece)
                on_delta(piece)

            try:
                completion = self._complete_once(
                    client if last else _without_retries(client),
                    build(link),
                    link,
                    deliver if on_delta is not None else None,
                )
            except Exception as exc:
                shown = shown or bool(emitted)
                self._failed(link, exc, reservation)
                last_error = exc
                if trace is not None:
                    trace.failed(link, exc)

                status = _status(exc)
                if status in (401, 403) or (
                    status is None and not isinstance(exc, NoChoicesReturned)
                ):
                    # The account is refused, or the host could not be
                    # reached or broke mid-reply: every link on this endpoint
                    # would fail the same way. Other providers may not.
                    dead.add(link.endpoint)
                remaining = [
                    later
                    for later in plan.links[index + 1 :]
                    if later.endpoint not in dead
                ]
                if not remaining:
                    raise
                if shown and on_reset is None:
                    # Words were shown that cannot be withdrawn; another
                    # model's answer must not be appended to them.
                    raise
                logger.warning(
                    "llm.model_unavailable",
                    extra={
                        "provider": link.provider,
                        "model": link.model,
                        "status": status,
                        "error": type(exc).__name__,
                        "next_provider": remaining[0].provider,
                        "next_model": remaining[0].model,
                        "retry_after": budget._retry_after(exc),
                    },
                )
                continue

            self.broker.record_success(link)
            # It answered: whatever benched it is over.
            self.broker.clear(link)
            completion.link = link
            if pin is None:
                # Measured against the first link that COULD have answered:
                # one with no key was never a choice, so nothing fell back
                # from it.
                preferred = next(
                    (
                        candidate
                        for candidate in self.links
                        if self.configured(candidate)
                    ),
                    None,
                )
                if preferred is not None and preferred.id != link.id:
                    completion.fell_back_from = preferred.version
            return completion

        raise last_error

    def _failed(self, link: Link, exc, reservation) -> None:
        status = _status(exc)
        if status in (413, 429):
            # Refused before it was served: the provider does not count it,
            # so neither does the budget. 413 is how a small free tier (Groq's
            # 8K tokens a minute) refuses a prompt that does not fit its
            # window - it was not benched, so every question paid a wasted
            # round trip to the same refusal before moving on.
            self.broker.refund(reservation)
            if status == 429 and budget.is_daily_quota(exc):
                self.broker.mark_exhausted(link)
        if status in (413, 429, 503):
            self.broker.cool(link, exc)
        self.broker.record_error(link, exc)

    def _complete_once(self, client, request: dict, link: Link, on_delta) -> Completion:
        if on_delta is None:
            completion = client.chat.completions.create(**request)
            choices = getattr(completion, "choices", None)
            if not choices:
                self._no_choices(link, completion)
            choice = choices[0]
            message = getattr(choice, "message", None)
            return Completion(
                # Typed `str | None`, and genuinely None when the model
                # emitted only tool calls or was cut off by a filter.
                text=getattr(message, "content", None) or "",
                finish_reason=getattr(choice, "finish_reason", "") or "",
                usage=getattr(completion, "usage", None),
                served_by=getattr(completion, "model", "") or request["model"],
                model=request["model"],
            )

        # Streamed. No `stream_options`: some of these servers 400 on a
        # parameter they do not implement, and usage is worth less than an
        # answer that arrives at all.
        parts, finish_reason, usage, served_by, last = [], "", None, "", None
        stream = client.chat.completions.create(**request, stream=True)
        try:
            for chunk in stream:
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
                    on_delta(piece)
                finish_reason = (
                    getattr(choices[0], "finish_reason", None) or finish_reason
                )
        finally:
            # Closed whether it finished or not: a reader who pressed Stop
            # (on_delta raised streaming.Cancelled) should stop the
            # provider generating, and an abandoned stream holds a
            # connection until it is collected.
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        if not parts and not finish_reason:
            # The streamed form of OpenRouter's HTTP 200 with an `error`
            # body and no choices.
            self._no_choices(link, last)
        return Completion(
            text="".join(parts),
            finish_reason=finish_reason,
            usage=usage,
            served_by=served_by or request["model"],
            streamed=True,
            model=request["model"],
        )

    def _no_choices(self, link: Link, completion):
        error = _embedded_error(completion)
        logger.warning(
            "llm.no_choices",
            extra={"provider": link.provider, "error": error},
        )
        raise NoChoicesReturned(
            f"{link.provider} returned a success with no choices ({error})"
        )

    # -- the API every feature calls ---------------------------------------

    def complete(
        self,
        messages: list,
        *,
        purpose: str,
        max_tokens: int,
        temperature: float = 0.2,
        json_mode: bool = False,
        on_delta=None,
        on_reset=None,
        trace: Trace | None = None,
    ) -> LLMResponse:
        """One completion for `purpose`, from the first link that serves it."""
        _check_purpose(purpose)

        def build(link: Link) -> dict:
            request = {
                "model": link.model,
                "messages": list(messages),
                "temperature": temperature,
                TOKEN_LIMIT_FIELD: link.token_cap(max_tokens),
                **dict(link.request_extras),
            }
            if json_mode and link.json_object:
                request["response_format"] = {"type": "json_object"}
            return request

        try:
            result = self.run(
                build,
                purpose=purpose,
                on_delta=on_delta,
                on_reset=on_reset,
                trace=trace,
            )
        except Unavailable:
            raise
        except Exception as exc:
            raise Unavailable(
                FAILED,
                self.resumes_at(),
                f"Every model failed; the last with {type(exc).__name__}.",
            ) from exc

        text = result.text
        return response_from(
            result,
            text=text,
            name=self.name,
            prompt_characters=sum(
                len(str(message.get("content") or "")) for message in messages
            ),
            metadata={"purpose": purpose},
        )

    def status(self) -> dict:
        """Every link's state and today's totals. No keys and no URLs: the
        NAME of each link's key variable, so a missing one can be set."""
        from .labels import model_display

        now = budget._now()
        states = self.broker.states(self.links, now)
        chain = []
        calls = 0
        capacity = 0
        unbounded = False
        resets = []
        for link in self.links:
            state = states[link.id]
            label = self._state(link, state, now)
            reset = budget.next_reset(link.quota_tz, now)
            error = state.last_error or {}
            chain.append(
                {
                    "id": link.id,
                    "provider": link.provider,
                    "provider_display": display_name(link.provider),
                    "model": link.model,
                    "label": model_display(link.provider, link.model),
                    "state": label,
                    "cooling_until": _iso(state.cooling_until)
                    if label == COOLING
                    else None,
                    "calls_today": state.calls_today,
                    "daily_limit": link.daily_limit or None,
                    "resets_at": reset.isoformat(),
                    "last_error_type": error.get("type"),
                    "last_error_status": error.get("status"),
                    "last_error_at": _iso(error.get("at")),
                    "last_success_at": _iso(state.last_success_at),
                    "trains_on_input": link.trains_on_input,
                    "key_env": link.key_env,
                }
            )
            calls += state.calls_today
            if label in (UNCONFIGURED, EXHAUSTED):
                continue
            if link.daily_limit:
                capacity += max(0, link.daily_limit - state.calls_today)
                resets.append(reset)
            else:
                unbounded = True

        resumes = None
        available = any(row["state"] == READY for row in chain)
        if not available:
            moment = self.resumes_at(now=now)
            resumes = moment.isoformat() if moment else None
        return {
            "enabled": bool(self.links),
            "chain": chain,
            "totals": {
                # None when a usable link has no daily limit: there is no
                # number of answers left to show, only "no limit".
                "capacity_left_today": None if unbounded else capacity,
                "calls_today": calls,
                "chat_reserve": budget.chat_reserve(),
                "resets_at": min(resets).isoformat() if resets else None,
                "generative_available": available,
                "resumes_at": resumes,
            },
        }


def response_from(
    result: Completion, *, text: str, name: str, prompt_characters: int, metadata=None
) -> LLMResponse:
    """An LLMResponse for a completion, carrying what the provider said."""
    link = result.link
    usage = result.usage
    details = {
        "provider": link.provider if link else "",
        "model": result.model,
        "finish_reason": result.finish_reason,
        # Which model actually served it, which is not always the one
        # requested: a free-model router picks per request, and some
        # gateways substitute without saying so anywhere else.
        "served_by": result.served_by,
        "streamed": result.streamed,
        "provider_may_train_on_input": bool(link and link.trains_on_input),
        **(metadata or {}),
    }
    if result.fell_back_from:
        details["chain_fell_back_from"] = result.fell_back_from
    # OpenRouter prices every response and reports it on the usage block.
    # Recorded when present rather than assumed, because no other backend
    # here sends it.
    cost = getattr(usage, "cost", None)
    if cost is not None:
        details["cost"] = cost
    return LLMResponse(
        text=text,
        model_name=name,
        model_version=link.version if link else result.model,
        generative=True,
        prompt_characters=prompt_characters,
        completion_characters=len(text),
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        model=result.model,
        provider=link.provider if link else "",
        finish_reason=result.finish_reason,
        fell_back_from=result.fell_back_from,
        may_train_on_input=bool(link and link.trains_on_input),
        metadata=details,
    )


# --- The process's gateway ------------------------------------------------------

_EMPTY = Gateway(())


def default() -> Gateway:
    """The gateway of the configured answerer, so a feature and chat share
    one set of clients; an empty one when generation is not configured."""
    from .engine import get_answerer

    try:
        answerer = get_answerer()
    except Exception as exc:
        # A misconfigured RAG_ANSWERER is chat's error to raise. Here it
        # means "no generation": every caller has a fallback for that.
        logger.warning("llm.gateway_unconfigured", extra={"error": type(exc).__name__})
        return _EMPTY
    gateway = getattr(answerer, "gateway", None)
    return gateway if isinstance(gateway, Gateway) else _EMPTY


def complete(
    messages: list,
    *,
    purpose: str,
    max_tokens: int,
    temperature: float = 0.2,
    json_mode: bool = False,
    on_delta=None,
) -> LLMResponse:
    """See Gateway.complete; raises Unavailable when nothing can serve it."""
    return default().complete(
        messages,
        purpose=purpose,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=json_mode,
        on_delta=on_delta,
    )


def available(purpose: str) -> bool:
    return default().available(purpose)


def status() -> dict:
    return default().status()


def capabilities() -> dict:
    """What chat can do right now, for anyone signed in."""
    from . import general

    gateway = default()
    ready = gateway.available(CHAT)
    resumes = None if ready else gateway.resumes_at()
    return {
        "generative_available": ready,
        "mode": "writing" if ready else "quoting",
        "resumes_at": resumes.isoformat() if resumes else None,
        "general_knowledge": general.enabled(),
    }


def pinned(link: Link, model: str) -> Link:
    """`link` with a different model, for a request that names its own."""
    return replace(link, model=model)
