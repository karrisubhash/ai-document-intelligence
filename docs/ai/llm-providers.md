# LLM providers, the model chain and the shared budget

Every call this system makes to a generative model goes through one module,
`backend/documents/rag/gateway.py`. It decides which model to send a request to, keeps
count of what has been spent today across every process, holds the last calls of the day
back for chat, and reports all of it on **System status → AI providers**. This page is how
to configure it, and in particular how to add free API keys so answers keep being written
when Gemini's free tier runs out.

## Why it exists

The only model the public deployment has is Gemini's free tier: about 5 requests a minute
and 20 a day **per model**, across four Flash models - roughly 80 written answers a day,
shared by every user. After that, answers fall back to quoting passages (still grounded and
cited, but not written). Before the gateway, that budget was invisible, each gunicorn and
Celery process rediscovered every rate limit for itself, and the fallback chain could not
leave Gemini. Now:

- the chain can span providers (`gemini`, then `groq`, then `cerebras`...), each adding its
  own daily allowance;
- cooldowns, call counts and "used up today" marks live in Redis, so all processes agree;
- background AI features (document briefs, multi-document grids) can only spend while a
  reserve for chat is left;
- every answer records which model actually wrote it, and the UI says so.

## Quick start: add a second provider

1. Get a free key (see the table below) and add it to the server's `.env`, e.g.
   `GROQ_API_KEY=gsk_...`. Keys are read from the environment only - never from Django
   settings, never logged, never returned by any API.
2. Name the chain: `RAG_ANSWERER_CHAIN=gemini,groq` (Gemini's four models first, then Groq).
3. Restart the backend and the Celery worker. On **System status → AI providers** the new
   link shows **Ready**; press **Test** to send it a one-token request.

Nothing else changes: grounding, citations, abstentions and the general-knowledge reply
work identically whichever link answers. A link whose key is missing is shown as
**No API key** and skipped.

## Chain syntax

`RAG_ANSWERER_CHAIN` is a comma-separated list, tried left to right:

| Entry | Meaning |
|---|---|
| `gemini` | A preset's default model followed by its fallback models (Gemini: `gemini-3.5-flash`, `gemini-3-flash-preview`, `gemini-3.7-flash`, `gemini-3.8-flash`) |
| `groq:openai/gpt-oss-120b` | Exactly that model at that provider. Split at the **first** colon, so `ollama:gpt-oss:20b` is Ollama's `gpt-oss:20b` |
| `custom:my-model` | `RAG_ANSWERER_BASE_URL`, keyed by `RAG_ANSWERER_KEY_ENV` (default `RAG_ANSWERER_API_KEY`) |

An unknown provider name is refused at start-up rather than guessed at. When
`RAG_ANSWERER_CHAIN` is **empty** the chain is the single provider configured the old way
(`RAG_ANSWERER_PROVIDER`, `RAG_ANSWERER_MODEL`, `RAG_ANSWERER_FALLBACK_MODELS`,
`RAG_ANSWERER_BASE_URL`), exactly as before this module existed - which is what the live
server runs with `RAG_ANSWERER_PROVIDER=gemini`. `RAG_ANSWERER` must still name
`documents.rag.openai_compat.OpenAICompatibleAnswerer`; with the extractive default there is
no chain at all.

`RAG_ANSWERER_REASONING_EFFORT` in an explicit chain only changes links whose preset already
sends `reasoning_effort` (Gemini sends `none` so its hidden thinking does not eat the output
budget); providers that do not implement it would answer 400.

## Providers

"Free tier" figures are what each provider documented when this was written (November
2026); only Gemini's were **measured** from this project. Providers change these without
notice - check your provider's console, and set `LLM_DAILY_LIMITS` to match.

| Preset | Key variable | Default model | Where to get a key | Free tier (as documented) | Default daily limit here | May train on inputs |
|---|---|---|---|---|---|---|
| `gemini` | `GEMINI_API_KEY` | `gemini-3.5-flash` (+3 fallbacks) | aistudio.google.com/apikey | **Measured 2026-10-11:** 5 requests/min and 20/day per model; day ends at midnight Pacific | 20 per model | **Yes** on unpaid tiers (Google's terms) |
| `groq` | `GROQ_API_KEY` | `openai/gpt-oss-120b` | console.groq.com/keys | ~30 requests/min, 1,000/day, but 8K tokens/min and 200K tokens/day for this model - the token cap binds first, around 65 grounded questions | 60 | No (unverified) |
| `cerebras` | `CEREBRAS_API_KEY` | `gpt-oss-120b` | cloud.cerebras.ai | Reported as about a million tokens/day; few requests per minute - set `RAG_ANSWERER_RETRIES` low | 100 (unverified) | No (unverified) |
| `openrouter` | `OPENROUTER_API_KEY` | `openrouter/free` (a router over free models) | openrouter.ai/keys | 50 free-model requests/day per **account** below $10 of credits, 1,000 above | 50 | **Yes** - free models may be trained on by whoever serves them |
| `mistral` | `MISTRAL_API_KEY` | `mistral-small-latest` | console.mistral.ai | "Experiment" plan: metered by tokens per month, not requests per day | 100 (unverified) | **Yes** on the free plan (unverified) |
| `github` | `GITHUB_MODELS_TOKEN` | `openai/gpt-4.1-mini` | github.com/settings/personal-access-tokens - a fine-grained token with **Models: read** | 50/day for "high" models, 150 for "low"; 8,000 input / 4,000 output tokens per request (requests are clamped to 4,000) | 50 | No (GitHub's statement; unverified) |
| `ollama` | `OLLAMA_API_KEY` (ignored) | `gpt-oss:20b` | runs on your own machine | No quota; nothing leaves the machine | unlimited | No |
| `openai` | `OPENAI_API_KEY` | `gpt-4.1-mini` | platform.openai.com | Paid only | unlimited - set one to cap spend | No (API data is not used for training by default) |

`github` uses its own variable rather than `GITHUB_TOKEN` on purpose: that name is often
exported for the `gh` CLI with far wider scopes, and a token issued for something else must
not quietly switch a provider on. The OpenAI reasoning models (o-series, gpt-5) reject
`temperature` and `max_tokens` as this client sends them; use `gpt-4.1-mini` or similar.

**Data use is a configuration fact.** The "may train on inputs" column is shown per link on
System status and stored on every answer (`metadata.llm.may_train_on_input`). For
confidential documents, attach billing to the Gemini key, or use `ollama`.

A sensible free setup for the public site: `RAG_ANSWERER_CHAIN=gemini,groq,cerebras` - about
80 + 60 + 100 written answers a day before anything is quoted.

## Daily limits

The budget counts **requests sent**, per link, per the provider's own day. A request the
provider refuses with 429 is handed back (the provider does not count it either); anything
else - a timeout, a 500, an answer that later failed grounding - was served and stays
counted. A link that reaches its limit shows **Used up today** and is skipped until its day
rolls over; so is a link the provider itself answered with a per-day 429, whatever our
count said.

```
LLM_DAILY_LIMITS=groq=200,gemini:gemini-3.5-flash=40,*=0
```

Keys are `provider:model`, `provider`, or `*` for everything; the most specific wins; `0`
means no limit is enforced (paid accounts, local models). Unset keys keep the preset
defaults in the table above.

Day boundaries: Gemini's daily quota resets at **midnight Pacific time** (Google's
rate-limit documentation), so its counters roll over then - 08:00 UTC in winter. Every other
provider is assumed to reset at midnight UTC; nobody here has verified that, and a provider
whose window is rolling will simply be retried a little later than it could have been.

## Purposes and the chat reserve

Every call states what it is for:

| Purpose | Who | May spend |
|---|---|---|
| `chat` | grounded answers and general-knowledge replies | everything, including links that are cooling down (tried last, never skipped - a cooldown is an estimate) |
| `brief` | on-demand document AI summaries | only while more than `LLM_CHAT_RESERVE` calls are left today, and never on a cooling link |
| `grid` | multi-document extraction cells | only while more than **2 ×** `LLM_CHAT_RESERVE` are left |
| `other` | anything else | only while more than **3 ×** `LLM_CHAT_RESERVE` are left |

`LLM_CHAT_RESERVE` defaults to 20. With Gemini alone (80 a day) briefs can use 60, grids 40,
anything else 20, and chat always has the last 20. Links with no daily limit (Ollama, a paid
account) serve every purpose whatever is left elsewhere. The reserve is checked before each
call, so two background calls racing for the last slot may overshoot it by one.

## How a failure moves the chain on

| Failure | Effect |
|---|---|
| 429 rate limited | The link cools down (the provider's "retry in N s", else 60 s; a per-day quota: until its quota midnight) and the next link is tried |
| 503 overloaded | 30 s cooldown, next link |
| 400, 404, 422 | This model will not take this request (an unsupported parameter, a retired model): next link |
| 500, 502, 504, a 200 with no answer | Next link |
| 401, 403 | The account is refused: every link on that provider is skipped for this request; other providers still get their turn |
| Timeout, connection error | The host is unreachable: its links are skipped; other providers still get their turn |

Only when every link has failed is the answer quoted from the passages instead. If a link
breaks off **mid-stream**, the words it showed are withdrawn (a `reset` event) before the
next link starts. The SDK's own retries are kept only for the last link tried.

## What people see

- **Chat** - every answer's payload has `model_display` ("Gemini 3.5 Flash",
  "GPT-OSS 120B · Groq", "Quoted from your documents") and
  `degraded: {answerer, reranker, reason}`, where `reason` is `"capacity"` (the AI was at
  its limits; it comes back by itself) or `"unavailable"` (something failed). The answer
  badge now names the model instead of "openai-compatible".
- **Composer** - `GET /api/v1/chat/capabilities/` says whether answers are being written or
  quoted right now, and when writing resumes. It names no provider or model.
- **System status → AI providers** (system administrators) - one row per link: state,
  calls today against the daily limit, cooling-until time, last error and last success,
  the may-train flag, and **Test**, which sends a one-token request (a real request: it
  counts against the link's allowance, is limited to 10 an hour per administrator, and is
  audited as `AI_PROVIDER_TEST`). A successful test clears the link's cooldown or
  used-up mark. The summary line gives the calls left today and when they reset, and
  today's answers written vs quoted.

## For developers: calling the gateway

```python
from documents.rag import gateway

try:
    response = gateway.complete(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        purpose=gateway.BRIEF,          # chat | brief | grid | other
        max_tokens=800,
        temperature=0.2,
        json_mode=False,                # asks for JSON where the provider supports it;
                                        # your prompt must ask for JSON too
        on_delta=None,                  # a callable to stream text as it arrives
    )
except gateway.Unavailable as exc:
    # exc.reason: not_configured | exhausted | cooling | reserved_for_chat | failed
    # exc.retry_at: when capacity is expected back, or None
    ...  # the extractive / non-LLM fallback, labelled as such
```

`response` is an `LLMResponse` with `text`, `model` (actually used), `provider`,
`prompt_tokens`, `completion_tokens`, `finish_reason`, `fell_back_from`,
`may_train_on_input`, `model_version` ("provider/model") and `metadata`.
`gateway.available(purpose)` answers "would a call succeed now?" without any network call;
`gateway.status()` is the admin view. Code reached through the RAG engine rather than
`complete()` can run under a purpose with `with gateway.purpose(gateway.OTHER): ...`.

Never call an LLM on the ingest path, and always have a fallback that does not need one:
in tests there is no network and no generative model (`RAG_ANSWERER` is empty, so
`complete()` raises `Unavailable` and `available()` is false). To drive the chain in a test,
build a `Gateway` and give it a stub client (see `documents/tests_llm_gateway.py`). The test
settings set `LLM_DAILY_LIMITS="*=0"` because every test shares one cache: budget tests set
their own limits.

## Known limits

- **The Claude answerer is outside the gateway.** `RAG_ANSWERER=…claude.ClaudeAnswerer`
  answers grounded questions as before, but it does not stream, has no daily budget, cannot
  be chained with the providers above, and has no general-knowledge reply
  (`CHAT_GENERAL_KNOWLEDGE` is reported as off by the capabilities endpoint). Gateway
  features such as briefs are unavailable with it.
- **Evaluation runs spend chat-priority calls.** `manage.py evaluate` with a
  generative answerer asks one question per case through the normal engine; on the free
  tier a run can use the day's allowance. Run it with generation off, or overnight.
- **Counts are this deployment's.** Another application using the same key spends the same
  provider quota without our counters knowing; the provider's per-day 429 then marks the
  link used up anyway.
- **If Redis is unreachable** the budget falls back to per-process state (logged once a
  minute as `llm.budget_cache_unavailable`): answers keep flowing, but processes stop
  sharing what they know until it is back.
