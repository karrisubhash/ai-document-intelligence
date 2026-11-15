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

- the chain can span providers (`gemini`, then `groq`, then `openrouter`...), each adding
  its own daily allowance;
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
sends `reasoning_effort` - and three presets do: `gemini` sends `none` (so its hidden
thinking does not eat the output budget), `groq` and `cerebras` send `low`. One global value
is offered to **all three** links, and a link takes it only if its preset documents it
(`Preset.effort_values`: Gemini `none`, `minimal`, `low`, `medium` and `high`; gpt-oss on Groq
and Cerebras `low`, `medium` and `high`). For a link whose preset does not, the value is not
sent, the link keeps its own, and `llm.effort_not_sent` is logged at WARNING - so `none` costs
Groq and Cerebras nothing, it just is not applied to them. `default` sends nothing to any
link. A model outside a preset's `extras_models` (a llama model pinned on Groq, say) gets no
`reasoning_effort` at all. A value a preset documents but one particular model still refuses
answers 400: the request stays counted and the chain moves on. Presets that do not implement
the parameter send nothing.

## Providers

"Free tier" figures are what each provider documented when this was written; the facts
about who needs a payment card, and which free tiers exist at all, were read from each
provider's own documentation on **2026-11-15**. Only Gemini's limits were **measured** from
this project. Providers change these without notice - check your provider's console, and
set `LLM_DAILY_LIMITS` to match. Anything marked *unverified* is a figure this project could
not read from the provider's documentation.

| Preset | Key variable | Default model | Where to get a key | Free tier (as documented) | Default daily limit here | May train on inputs |
|---|---|---|---|---|---|---|
| `gemini` | `GEMINI_API_KEY` | `gemini-3.5-flash` (+3 fallbacks) | aistudio.google.com/apikey | **Measured 2026-10-11:** 5 requests/min and 20/day per model; day ends at midnight Pacific | 20 per model | **Yes** on unpaid tiers (Google's terms) |
| `groq` | `GROQ_API_KEY` | `openai/gpt-oss-120b` | console.groq.com/keys | No card. **Unverified:** Groq's free-plan limits are not on its documentation pages (checked 2026-11-15); the figures here (~30 requests/min, 1,000/day, 8K tokens/min and 200K tokens/day for this model) are as reported when this was written, so read your console's limits page. If they hold, the token cap binds first: a full grounded request counts 5-6K tokens against it (a typical question measured 2,000-2,700 prompt tokens and a full 12-passage context about 5,000, plus the up to 1,500 the request asks for in reply), so roughly one question a minute and ~30 a day, with no room for a corrective retry in the same minute | 30 | No (unverified) |
| `cerebras` | `CEREBRAS_API_KEY` | `gpt-oss-120b` | cloud.cerebras.ai | **Not a no-card free tier.** Its documentation requires a verified payment method before the API turns on; it then gives a one-time $5 credit that expires after 30 days and says it is not intended for production use. The request limits that follow the credit are unverified | 100 (unverified) | No (unverified) |
| `openrouter` | `OPENROUTER_API_KEY` | `openrouter/free` (a router over free models) | openrouter.ai/keys | Free models: 20 requests/min and **50 requests a day per account until 10 credits are bought** (1,000 a day after). Whether a card is needed to sign up is not stated in its documentation | 50 | **Yes** - free models may be trained on by whoever serves them |
| `mistral` | `MISTRAL_API_KEY` | `mistral-small-latest` | console.mistral.ai | **Unverified:** Mistral's documentation pages did not load on 2026-11-15. Earlier notes say an "Experiment" plan metered by tokens per month, not requests per day | 100 (unverified) | **Yes** on the free plan (unverified) |
| `github` | `GITHUB_MODELS_TOKEN` | `openai/gpt-4.1-mini` | - | **RETIRED.** GitHub Models was shut down on 30 July 2026; this preset no longer works - its link will fail and the chain will move on. Leave it out | 50 | - |
| `ollama` | `OLLAMA_API_KEY` (ignored) | `gpt-oss:20b` | runs on your own machine | No quota; nothing leaves the machine | unlimited | No |
| `openai` | `OPENAI_API_KEY` | `gpt-4.1-mini` | platform.openai.com | Paid only | unlimited - set one to cap spend | No (API data is not used for training by default) |

The retired `github` preset used its own variable rather than `GITHUB_TOKEN` on purpose:
that name is often exported for the `gh` CLI with far wider scopes, and a token issued for
something else must not quietly switch a provider on. The OpenAI reasoning models (o-series,
gpt-5) reject `temperature` and `max_tokens` as this client sends them; use `gpt-4.1-mini` or
similar.

**Providers without a preset, and why none of them is a free option** (read from their own
documentation on 2026-11-15):

| Provider | Free API access? |
|---|---|
| xAI Grok | **No.** Its documentation describes no free API tier |
| Hugging Face | **No.** Free accounts get no inference credits |
| Cloudflare Workers AI | A free allowance of 10,000 "Neurons" a day; whether a card is needed is not stated. The app has no preset for it, so using it takes a small code change |

**Data use is a configuration fact.** The "may train on inputs" column is shown per link on
System status and stored on every answer (`metadata.llm.may_train_on_input`). For
confidential documents, attach billing to the Gemini key, or use `ollama`.

A sensible free setup for the public site, needing no payment card at any of its three
links: `RAG_ANSWERER_CHAIN=gemini,groq,openrouter` - about 80 + 30 + 50 = 160 written answers
a day before anything is quoted. (Cerebras is not in it: it needs a verified payment method.)
Mind the order on a busy site: Groq's 8K-tokens-a-minute window fits roughly one
grounded question a minute, so `gemini,openrouter,groq` serves a burst better. On either
chain every answer a free tier writes sends that provider up to about 14,000 characters of
the asker's documents (see below), on a tier whose terms allow training on them - so this
suits demonstration documents, not confidential ones.

**Keys and quotas.** Rotating several accounts' keys to multiply one site's free quota is
against Google's API terms (section 2(d) of the Google APIs Terms of Service). Gemini's rate
limits are applied per **project**, not per key, so a second key created in the same project
shares the first one's quota and adds nothing.

**Three presets set a reasoning effort, two of them to `low`.** `gemini` sends `none`;
`groq` and `cerebras` send `reasoning_effort: "low"`
(gpt-oss is a reasoning model and bills its hidden reasoning against `max_tokens`, so a short
answer could be cut off and recorded as a failure), and `groq` also caps the reply it asks
for at 1,500 tokens - its window counts the reply a request *asks* for, not the one it gets.
Both providers' documentation (read 2026-11-15) lists `reasoning_effort` with the values
low, medium and high as a top-level parameter for gpt-oss-120b, and Cerebras states that
reasoning tokens count against the completion cap; the model ids (`openai/gpt-oss-120b` on
Groq, `gpt-oss-120b` on Cerebras, 65K context on its free tier) are listed there too. Neither
preset has been called with a real key from here. After adding a key, press **Test** on
System status; a provider that rejects the parameter answers 400, and the chain moves on to
the next link rather than failing the question.

A **413** ("request too large": a prompt that does not fit a free tier's window) is handled
like a 429 - handed back, not counted, and the link rested for a minute - instead of costing
every question a wasted round trip.

## What a model is shown, and what happens when it is wrong

These apply only while a model will be tried for the answer - a link that is resting after
a rate limit still counts for chat, which tries it rather than give up, and a chain with no
key or every link used up for the day does not. A quote keeps the settings it was measured
with. They are all in `.env.example`.

"A model will answer" is one test, and everything below that is for a model's benefit
follows from it: the larger context, the neighbouring chunks, the corrective retry, and
answering a compound question whole. Whether the model is also handed passages the relevance
floor rejects is a separate switch, `RAG_MODEL_JUDGES_RELEVANCE`, so turning that off does
not turn the rest off with it.

- **The model judges relevance, not the score floor.** The small reranker scores a correct
  paraphrase 0.00-0.08 against a floor of 0.30, so the model never saw it and the reader was
  told "not found" - or, with general knowledge on, got a general reply to a question about
  their own documents. The reranker's top 6 passages and retrieval's top 2 now go to the
  model even below the floor (`RAG_MODEL_JUDGES_RELEVANCE`), and a question scoped to one
  document also reads its opening chunks. A quote - including the fallback when every
  provider is out of quota - never sees those passages.
- **A question the documents cannot answer now costs up to two provider calls.** With
  `CHAT_GENERAL_KNOWLEDGE` on, "What is the capital of France?" used to stop at "nothing
  relevant" and make one call, the general reply, with no document text. No score tells a
  correct paraphrase from an unrelated question (both land at 0.00-0.08), so the model is
  shown the best passages first and answers "not found"; only then is the general reply asked
  for. The first call carries up to about 10,000-14,000 characters of the asker's documents
  to the provider, for a question that has nothing to do with them, and spends one of the
  day's requests (two of Gemini's 20 per model instead of one), and the reader waits for both.
  A question the documents answer is not affected. This is the price of not telling readers
  "not found" about their own documents; a site on a tight daily budget can set
  `RAG_MODEL_JUDGES_RELEVANCE=False` to return to one call for such a question, with the old
  score floor and the misses that come with it.
- **More context, with the chunks around the best ones.** 14,000 characters and 12 passages
  instead of 8,000 and 8, and the chunk before and after each of the top 3 passages that
  cleared the relevance floor, so a list or procedure that runs over a chunk boundary arrives
  whole. (A question whose every passage is below the floor, the paraphrase case, gets no
  neighbours: nothing vouches for which chunk to extend.)
- **A compound question goes whole.** Planning cut "the fee for X and Y, and when is it
  due?" into parts that lost what they referred to, spending one call per part.
- **A refusal in the model's own words is a refusal.** "I couldn't find X; these documents
  only list Y" was stored as an ungrounded answer - the hallucination signal - and kept
  chat's general reply away. A reply counts as a refusal when it cites nothing and states no
  figure the question did not itself type ("I couldn't find the 2023-24 fee" repeats the
  year that was asked, so it still counts). A reply that answers from the model's own
  knowledge is treated the same way, so chat's general reply can answer instead of the
  reader being told a draft "could not be verified". That is judged strictly: it cites
  nothing and states no figure, none of the passages it was shown passed the relevance
  floor, at least 3 of its content words are in none of the passages, and fewer than 30% of
  them are (`OWN_KNOWLEDGE_MAX_SUPPORT`). A short, correct paraphrase that merely forgot its
  citation shares more than that with its passage, and stays an ungrounded draft, which the
  reader is told could not be verified - not "your documents do not cover this". Anything
  that cites a passage or states a figure still goes through the grounding check.
- **A correct answer is not discarded for how it spells a fact.** Accepted as the same
  fact: a date written another way ("31 March 2025" for "31.03.2025"), a day and month
  without the year, a year range ("2024-25"), a numeral for a number the passage spells out
  ("7 days" for "seven days"; a bare "one" only before a unit of time, because "at one time"
  and "no one" are not quantities), a clock time written another way ("7:30 PM" for "7.30
  pm"), "3 lakh" for 300000 (exact equality, both ways), "15-day" or "Rs 500/week" for the
  number the passage states with its unit, a citation written "[1, 2]", "[1-2]", "[ 1 ]" or
  "[Source 1]", a list numbered "**1.**" or "(1)" - and a figure the **user** gave about
  their own situation ("I have 70 percent attendance" answered with "At 70 percent ..."),
  which is never a source and never cited. A figure the user only ASKED about ("Is the
  minimum 70 percent?") is not theirs to repeat: it must be in a cited passage, or the answer
  is rejected. Still rejected: a date that could be read two ways unless written the same
  way, a date that does not exist ("31 April 2025" is one exact token and is never rescued
  by a 31 and a 2025 elsewhere), a different day or month, a hyphenated range (A-B, also
  written with an en dash) whose two ends are not found together in one passage (within about
  40 characters), a marker that names no passage, and any figure that is neither in a cited
  passage nor given by the user about themselves. Only the hyphenated form of a range is
  checked as a pair: "between 40 and 75" and "40 to 75" are checked figure by figure, each
  of which must be in a cited passage. An interval such as "[0, 1]" in an answer is left
  alone unless every number in it is a passage. Numbers the *answer* spells out ("seven
  days") are not checked.
- **A rejected answer gets two chances before it is discarded.** A figure the answer states
  after its last citation in a sentence, that is written in exactly one passage it did not
  cite, where the sentence shares at least two words with that passage and the word beside
  the figure (its unit or noun: "200 rupees", "200 days") stands beside it in that passage
  too, gets that passage's citation (at most two per answer, never a table row, and the
  result is checked again from scratch). Otherwise the model is told which figures were not
  in its passages and asked once more (`RAG_GROUNDING_RETRY`). The second answer is checked
  exactly like the first, and if it fails too the first failure stands. If the retry never
  reaches a model (a timeout, every link rate limited) the answer is quoted from the
  passages instead - checked like any answer - and the outage is not counted against the
  model. The retry is a second provider call on a free tier's small daily count, which is
  why it is only for figures the passages lack, and why it is not attempted when the first
  call used the link's last request of the day (the draft is then withheld, with no quote).
  The stored record of an answer that took two
  calls sums their tokens and cost and remembers if EITHER provider may train on what it was
  sent (`first_provider` names the first).
- **The check cannot turn into an error page or a pass.** If the grounding check itself
  raises on something in a document, the answer is withheld as ungrounded and the failure
  is logged by type and place only (`grounding.check_failed`), never with the message,
  which can echo document text.
- **How often the first draft is wrong is counted.** An answer that was repaired, or
  recovered by the retry, no longer reaches the abstention counter, so
  `adi_rag_grounding_recoveries_total{outcome}` counts them: `repaired`, `retried_grounded`,
  `retried_failed` (the model answered again and failed) and `retry_unavailable` (the retry
  never reached a model: an outage, not a grounding failure). A `retried_failed` answer is
  still withheld, so it is counted in both places. That, not the abstention rate, is the
  number that says whether a provider is any good.
- **A failed model call does not lose a compound question.** Planning is skipped while a
  model will answer; if that whole-question call fails and the question splits into two or
  more parts, the parts are answered by quoting, as before, with no further model calls.
  "Fails" includes a first draft that was rejected when the corrective retry then could not
  reach a model (an outage): the first failure stands, but it is the outage, not the model's
  wrong draft, that the parts are quoted around.
- **An empty or cut-off reply moves on to the next link** (an empty reply, or a
  `finish_reason` of length or content_filter), like a 500 does; only when the last link
  fails this way too is the answer reported as failed. The tokens and cost of a reply that
  was passed over are summed into the answer's stored record, and each such reply stays
  counted against the day's allowance. So a reply that a provider's own filter refused
  (`content_filter`), or that was cut off, can cost one request on EVERY remaining link, and
  the same prompt and passages go to each of them: on the four-link Gemini chain that is four
  requests, one of each model's 20 a day, for one question, and with a chain that continues
  to other providers the passage text a safety classifier refused reaches them too. (Their
  tokens are not recorded if every later link then fails.) Answers are written in the
  language of the passages (English) whatever language the question is in, because the
  grounding check compares words with the passages.
- **A quote after a model failure uses the older context** - 8 passages, 8,000 characters,
  vetted passages only - as the settings say.

To measure all of it, `manage.py evaluate --chat-path --pause 12` against
`evaluation/benchmark-v3.template.json` reports `correctness` (the expected answer's key facts
are in the answer) and how many questions the documents answer got a general-knowledge reply.

## Daily limits

The budget counts **requests sent**, per link, per the provider's own day. A request the
provider refuses with 429 (rate limited) or 413 (too large for its window) is handed back
(the provider does not count it either); anything else - a timeout, a 500, a 400 for a
parameter it rejects, an answer that later failed grounding - was served and stays
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

**One walk has a time budget** (`RAG_ANSWERER_DEADLINE`, 50 s by default, 0 for none). Without
one, a hung provider is waited for the full `RAG_ANSWERER_TIMEOUT`, then the next, then the
SDK's retries on the last: a question can outlast nginx's 120 s and the reader gets a 504
instead of an answer or the quoted fallback (a real-model run saw a 61 s stall). With it, each
attempt is given what is left of the budget - half of it while another provider could still be
tried, so one hung provider cannot starve a healthy one - and never less than 8 s; no new
provider is started in the last 8 s; and the last link keeps only the SDK retries that still
fit. It decides whether another link STARTS and never interrupts one that is answering. A
corrective retry is a second walk.

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
