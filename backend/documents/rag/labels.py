"""Model names for people: "Gemini 3.5 Flash", "GPT-OSS 120B · Groq".

An answer used to be labelled with its answerer's class ("openai-compatible"),
which tells a reader nothing, and the identifier the provider uses
("groq/openai/gpt-oss-120b") is not much better. This turns an identifier into
the name the model's own publisher uses, plus the provider that served it when
that is not obvious from the name: a Gemini model can only come from Google,
an open-weight model could come from any of four hosts here, and which one it
was matters (speed, quota, data use).

Pure string handling with no table to keep in step: unknown ids still come
out readable, just less polished.
"""

import re

from .providers import CUSTOM, GEMINI, MISTRAL, OLLAMA, OPENAI, OPENROUTER, display_name

QUOTED = "Quoted from your documents"

# Tokens with a fixed spelling. Anything else is capitalised, and a size
# such as "120b" is upper-cased ("120B").
_WORDS = {
    "gpt": "GPT",
    "oss": "OSS",
    "gemini": "Gemini",
    "gemma": "Gemma",
    "flash": "Flash",
    "pro": "Pro",
    "lite": "Lite",
    "mini": "mini",
    "nano": "nano",
    "llama": "Llama",
    "mistral": "Mistral",
    "mixtral": "Mixtral",
    "codestral": "Codestral",
    "qwen": "Qwen",
    "qwq": "QwQ",
    "deepseek": "DeepSeek",
    "phi": "Phi",
    "claude": "Claude",
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "instruct": "Instruct",
    "it": "IT",
    "kimi": "Kimi",
    "glm": "GLM",
}

# Tags a host appends to an id that are not part of the model's name.
_DROPPED = {"free", "latest", "beta"}

# Families whose name already says who serves them.
_SELF_NAMED = {
    GEMINI: ("gemini", "gemma"),
    MISTRAL: ("mistral", "mixtral", "codestral", "ministral", "magistral", "pixtral"),
    OPENAI: ("gpt", "o1", "o3", "o4"),
    "anthropic": ("claude",),
}

_SIZE = re.compile(r"^\d+(\.\d+)?[bm]$", re.I)
_SPLIT = re.compile(r"[-_\s:]+")


def _pretty(model: str) -> tuple:
    """(display name, first token) for a bare model id."""
    name = model.rsplit("/", 1)[-1]
    tokens = [token for token in _SPLIT.split(name.lower()) if token]
    preview = "preview" in tokens or "exp" in tokens
    tokens = [t for t in tokens if t not in _DROPPED and t not in ("preview", "exp")]
    if not tokens:
        return "", ""

    words = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if (
            token == "gpt"
            and following
            and (following == "oss" or following[0].isdigit())
        ):
            # OpenAI writes these joined: "GPT-4.1", "GPT-OSS".
            words.append(f"GPT-{_WORDS.get(following, following)}")
            index += 2
            continue
        if token in _WORDS:
            words.append(_WORDS[token])
        elif _SIZE.match(token):
            words.append(token.upper())
        elif token[0].isdigit() or token.startswith("v") and token[1:2].isdigit():
            words.append(token)
        else:
            words.append(token[:1].upper() + token[1:])
        index += 1

    text = " ".join(words)
    if preview:
        text += " (preview)"
    return text, tokens[0]


def model_display(
    provider: str, model: str, *, generative: bool = True, served_by: str = ""
) -> str:
    """The name to show for an answer written by `model` at `provider`."""
    if not generative:
        return QUOTED

    provider = (provider or "").lower()
    # A router picks per request; what it picked is the model that wrote it.
    if provider == OPENROUTER and served_by and served_by != model:
        model = served_by
    if provider == OPENROUTER and model.rsplit("/", 1)[-1] == "free":
        return "Free model · OpenRouter"

    name, family = _pretty(model or "")
    if not name:
        return display_name(provider) or "AI model"

    if provider in _SELF_NAMED and family in _SELF_NAMED[provider]:
        return name
    if provider == "claude" or (not provider and family == "claude"):
        return name
    if provider == OLLAMA:
        return f"{name} · Ollama (local)"
    if provider == CUSTOM or not provider:
        return name if not provider else f"{name} · custom endpoint"
    return f"{name} · {display_name(provider)}"


def display_for_version(
    model_version: str, *, generative: bool, served_by: str = ""
) -> str:
    """model_display() from a stored "provider/model" string.

    Older answers kept only that string. A model id can itself contain a
    slash ("openai/gpt-oss-120b"), so only the first one separates the
    provider. An extractive answer's version is its own ("0.1.0").
    """
    if not generative:
        return QUOTED
    provider, _, model = (model_version or "").partition("/")
    if not model:
        # "claude-opus-5": the Claude answerer records the bare model.
        return model_display("", provider, served_by=served_by)
    return model_display(provider, model, served_by=served_by)


def answer_display(
    *, model_name: str, model_version: str, generative: bool, llm=None
) -> str:
    """The name to show for one stored or fresh answer.

    Empty when no answerer ran at all (nothing was retrieved): there is no
    author to name. Quoted when the extractive answerer produced it.
    """
    if not generative:
        return QUOTED if model_name else ""
    llm = llm or {}
    if llm.get("model"):
        return model_display(
            llm.get("provider", ""), llm["model"], served_by=llm.get("served_by", "")
        )
    return display_for_version(model_version, generative=True)


# The engine's own notes, for answers stored before `degraded` was recorded.
_ANSWERER_NOTE = "answered by quoting passages instead"
_RERANKER_NOTE = "passages were scored lexically instead"


def degraded(
    *, answerer: bool, reranker: bool, llm=None, source: str = "", notes=()
) -> dict:
    """What ran in a lesser mode for this answer, for the UI to say plainly.

    `reason` is "capacity" when the AI was at its limits (a cooldown or the
    day's quota - it comes back by itself), "unavailable" when something
    failed, or None. A general-knowledge reply was written by a model and
    does not rest on ranked passages, so nothing about it is degraded.
    """
    if source == "general":
        return {"answerer": False, "reranker": False, "reason": None}
    notes = list(notes or ())
    answerer = bool(answerer) or any(_ANSWERER_NOTE in note for note in notes)
    reranker = bool(reranker) or any(_RERANKER_NOTE in note for note in notes)
    reason = None
    if answerer:
        kind = (llm or {}).get("fallback_kind")
        reason = "capacity" if kind == "capacity" else "unavailable"
    elif reranker:
        reason = "unavailable"
    return {"answerer": answerer, "reranker": reranker, "reason": reason}
