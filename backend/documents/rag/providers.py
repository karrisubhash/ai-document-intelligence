"""What this system knows about each OpenAI-compatible provider.

The OpenAI chat-completions request has become the shape almost every other
provider also accepts, so a provider collapses into a handful of strings - a
base_url, a default model id, the NAME of the environment variable holding
the key - plus the quirks that make "compatible" something less than
"identical". Those strings are unforgiving, and they take their revenge in
another component's error message (see GEMINI below), so they live here,
once, as presets: `gemini` is the whole configuration, not a URL someone has
to type correctly.

Each preset also carries what the BUDGET needs to know about it: how many
requests a day its free tier allows per model, in which time zone that day
ends, and whether its free tier may train on what it is sent. Those numbers
change without notice; every one below says where it came from, and
docs/ai/llm-providers.md marks the ones nobody here has measured.
"""

from dataclasses import dataclass

GEMINI = "gemini"
GROQ = "groq"
OPENROUTER = "openrouter"
OLLAMA = "ollama"
CEREBRAS = "cerebras"
MISTRAL = "mistral"
GITHUB = "github"
OPENAI = "openai"

# A base URL with no preset behind it (RAG_ANSWERER_BASE_URL).
CUSTOM = "custom"

DEFAULT_PROVIDER = GEMINI

# Where the key comes from when a custom base_url is configured with no
# preset behind it. It names an environment variable; it never holds one.
DEFAULT_KEY_ENV = "RAG_ANSWERER_API_KEY"

UTC = "UTC"


@dataclass(frozen=True)
class Preset:
    """The strings that identify a backend, plus its caveats."""

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
    # per preset, never globally - see OpenAICompatibleAnswerer._request_for
    # for why.
    request_extras: tuple = ()

    # How a person should read the provider's name, e.g. "GitHub Models".
    display: str = ""

    # Requests per model per day that the free tier allows, as the budget
    # counts them (documents.rag.budget). 0 means no daily limit is
    # enforced - a local server, or a paid account billed per token.
    # Overridden by LLM_DAILY_LIMITS.
    daily_limit: int = 0

    # The time zone whose midnight ends that day. A counter that rolls over
    # at the wrong hour either wastes the first hours of a fresh quota or
    # spends calls the provider has already refused.
    quota_tz: str = UTC

    # The largest max_tokens the backend accepts, when it has a ceiling
    # below the answerers' own cap. Above it a request is a 400.
    max_output_tokens: int = 0

    # Whether `response_format={"type": "json_object"}` is known to be
    # accepted. Where it is not, json_mode asks nothing of the request and
    # the caller's prompt has to ask for JSON on its own - which it must do
    # anyway: providers that support the parameter require it too.
    json_object: bool = False


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
    # The trailing "/v1beta/openai/" is load-bearing - Gemini's compatibility
    # endpoint is served only there and answers 404 on anything else, which
    # the SDK reports as a model problem. Do not tidy it to "/v1/".
    GEMINI: Preset(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-3.5-flash",
        key_env="GEMINI_API_KEY",
        trains_on_free_tier=True,
        display="Gemini",
        # Measured 2026-10-11 on a new free-tier key: 5 requests per minute
        # PER MODEL. gemini-2.5-flash and -flash-lite answer 404 ("no longer
        # available to new users"); the -lite 3.x models reject
        # reasoning_effort "none" with a 400. These two accepted it, first
        # streamed token 1.5 s and 5.8 s. Re-check before relying on them:
        # Google rotates this catalogue.
        # The free tier also caps each model at 20 requests PER DAY (measured
        # 2026-10-11: GenerateRequestsPerDayPerProjectPerModel-FreeTier), so
        # every model in the chain adds a day's allowance of its own.
        # gemini-3.8-flash answered 503 at times that day; a 503 benches a
        # model for 30 s rather than removing it.
        # No -lite model belongs here while request_extras sends
        # reasoning_effort "none". Its 400 used to END the chain, and since
        # the cooling models sort last it was the model tried once the
        # others were rate-limited, so every question then fell back to
        # quoting passages. A 400 now moves on to the next link instead
        # (documents.rag.gateway), but a link that can never succeed still
        # costs every question a round trip.
        fallback_models=(
            "gemini-3-flash-preview",
            "gemini-3.7-flash",
            "gemini-3.8-flash",
        ),
        # Gemini 3 Flash "thinks" by default, and its hidden reasoning is
        # billed against max_tokens: at 200 tokens even "say hello" came
        # back cut off (finish_reason "length"). "none" also halved the
        # time to the first streamed word (3.4 s -> 1.6 s). This is
        # extraction over supplied passages, which needs no deliberation.
        request_extras=(("reasoning_effort", "none"),),
        # The measured 20 a day, above. Google's rate-limit documentation
        # says daily quotas reset at midnight Pacific time, not UTC.
        daily_limit=20,
        quota_tz="America/Los_Angeles",
    ),
    # Very fast. The free tier caps gpt-oss at 8K tokens PER MINUTE, and a
    # grounded prompt here is the system rules plus at most RAG_MAX_PASSAGES
    # passages inside RAG_CONTEXT_BUDGET characters (8,000 by default):
    # roughly 2-3K tokens, so two or three questions a minute. That makes
    # it a good second provider behind Gemini rather than the only one.
    #
    # The llama-3.x ids are deliberately not the default: they are absent
    # from the free-tier table and one of them is already deprecated in
    # favour of this model.
    GROQ: Preset(
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-120b",
        key_env="GROQ_API_KEY",
        display="Groq",
        # Groq's published free tier for this model is 1,000 requests and
        # 200K tokens a day, and 8K tokens a MINUTE. A grounded prompt is now
        # about 5-6K tokens and an answer a few hundred, so the token cap
        # binds first: around 30 questions a day, and one a minute. That is
        # why Groq belongs LAST in a chain, as a last resort.
        daily_limit=30,
        # gpt-oss is a reasoning model whose hidden reasoning is billed
        # against max_tokens; at the default effort a short answer can be cut
        # off at the cap (finish_reason "length"), which is an answerer
        # failure. This is extraction over supplied passages. The ceiling
        # keeps prompt + max_tokens inside the 8K-per-minute window, which
        # counts the reply the request ASKS for, not the one it gets.
        # NOT verified against a live key from here: confirm with the AI
        # providers test on System status before relying on it.
        request_extras=(("reasoning_effort", "low"),),
        max_output_tokens=1500,
        json_object=True,
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
        display="OpenRouter",
        # OpenRouter documents 50 free-model requests a day for an account
        # that has bought less than $10 of credits (1,000 above that), for
        # the whole account rather than per model. Not measured from here.
        daily_limit=50,
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
        display="Ollama",
    ),
    # A roomy per-minute token budget and very few requests per minute.
    # Set RAG_ANSWERER_RETRIES low here: at a 60 second timeout, the SDK's
    # own retries are enough to spend the minute's whole request allowance
    # on one question.
    CEREBRAS: Preset(
        base_url="https://api.cerebras.ai/v1",
        model="gpt-oss-120b",
        key_env="CEREBRAS_API_KEY",
        display="Cerebras",
        # Cerebras's free tier is reported at about a million tokens a day;
        # 100 grounded questions stays well inside that. Unverified.
        daily_limit=100,
        # gpt-oss reasons before it answers and bills that against
        # max_tokens; "low" is plenty for extraction over given passages.
        # NOT verified against a live key from here: confirm with the AI
        # providers test on System status before relying on it.
        request_extras=(("reasoning_effort", "low"),),
    ),
    # Mistral's own API speaks /chat/completions at this path. Its free
    # ("Experiment") plan meters tokens per month rather than requests per
    # day, and its terms let Mistral train on what the free plan is sent -
    # both as read from their documentation, neither measured here.
    MISTRAL: Preset(
        base_url="https://api.mistral.ai/v1",
        model="mistral-small-latest",
        key_env="MISTRAL_API_KEY",
        trains_on_free_tier=True,
        display="Mistral",
        daily_limit=100,
        json_object=True,
    ),
    # GitHub Models: OpenAI, Meta, Mistral and other models behind one
    # GitHub token (a fine-grained personal access token with the
    # `models: read` permission). The model id carries its publisher
    # ("openai/gpt-4.1-mini"). The free allowance depends on the model's
    # tier - documented as 50 requests a day for "high" models and 150 for
    # "low" ones - and every request is capped at 8,000 input and 4,000
    # output tokens.
    #
    # Its own variable rather than GITHUB_TOKEN: that name is commonly
    # exported for the gh CLI with far wider scopes, and a token issued for
    # something else should not quietly switch a provider on.
    GITHUB: Preset(
        base_url="https://models.github.ai/inference",
        model="openai/gpt-4.1-mini",
        key_env="GITHUB_MODELS_TOKEN",
        display="GitHub Models",
        daily_limit=50,
        max_output_tokens=4000,
        json_object=True,
    ),
    # The first-party OpenAI API: paid, with no free tier, and API traffic
    # is not used for training by default. gpt-4.1-mini because it takes
    # `temperature` and `max_tokens` as sent here; the reasoning models
    # (o-series, gpt-5) reject both and need max_completion_tokens, which
    # this client deliberately does not send (see TOKEN_LIMIT_FIELD).
    # No daily limit by default: set one in LLM_DAILY_LIMITS to cap spend.
    OPENAI: Preset(
        base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini",
        key_env="OPENAI_API_KEY",
        display="OpenAI",
        json_object=True,
    ),
}


def display_name(provider: str) -> str:
    """How a person should read a provider name."""
    preset = PRESETS.get(provider)
    if preset is not None and preset.display:
        return preset.display
    if provider == CUSTOM:
        return "Custom endpoint"
    return provider.replace("-", " ").replace("_", " ").title() if provider else ""
