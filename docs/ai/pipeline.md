# The AI pipeline

```
question
  → follow-up resolution        rag/followup.py      (conversation context)
  → planning                    agents/planner.py    (multi-part questions)
  → permission-filtered hybrid retrieval   search/service.py
  → reranking                   rag/reranking.py | local_rerank.py | bedrock_rerank.py
  → relevance floor             rag/engine.py
  → context construction        rag/context.py
  → answerer                    rag/llm.py | claude.py | openai_compat.py
  → grounding and citations     rag/grounding.py
  → answer + citations, or an abstention with a reason
```

Every stage can end the question with an **abstention**, and each abstention has a
distinct reason (no authorized documents, nothing relevant enough, empty context, the
answerer found nothing, the answer could not be grounded, the evidence is in a figure the
system cannot read). "I could not find that" and "I found something I could not verify"
are different facts, and the user and the metrics both see which one happened.

## Indexing

Pages are split into **page-bounded chunks** (1000 characters, 150 overlap; tables kept
intact with their header rows as metadata). Each chunk records its page and character span
into the page text, so every citation can be resolved back to exact words. Embeddings
record their provider name and version; retrieval only compares vectors from the
configured provider, so a model change can never mix coordinate spaces — it requires
`manage.py reindex`.

| Provider | Setting value | Semantic | Notes |
|---|---|---|---|
| Hashing baseline | `…embedding.hashing.HashingEmbeddingProvider` | no | Deterministic, offline, 384-d. Scores vocabulary overlap, and says so in every response |
| Local | `…embedding.local.LocalEmbeddingProvider` | yes | bge-small-en-v1.5, 384-d (the column's width — no migration). CPU. Loads from the model cache without network |
| Bedrock Titan V2 | `…embedding.bedrock.BedrockTitanEmbeddingProvider` | yes | Needs AWS credentials and a migration (Titan emits 256/512/1024). Contract-tested only |

## Retrieval

Lexical (PostgreSQL full-text, per-term OR plus the whole query so identifiers like
`REG/2024/117` match) and vector (pgvector cosine, HNSW) retrieval each return up to 50
candidates **from the caller's authorized set only**, restricted to current versions,
live extraction passes and non-archived documents. Reciprocal-rank fusion (k = 60) merges
them. Optional filters: document type, department, a single document.

If the query cannot be embedded (a model that failed to load), search continues lexically
and says so in `notes` rather than returning a 500.

## Reranking

| Reranker | Floor | Degrades to |
|---|---|---|
| Lexical (default) | 0.15 | — |
| Local cross-encoder | per model, calibrated: **0.02** bge-reranker-base (default model), **0.30** MiniLM-L6, 0.30 for any other | lexical, with every passage marked degraded |
| Bedrock (Cohere Rerank 3.5) | 0.10 | lexical |

The floor belongs to the scoring scheme that produced the scores: a degraded cross-encoder
returns lexical scores, and they are held to the **lexical** floor. The cross-encoder floors
were chosen on a separate development set by a rule fixed in advance, then confirmed once on
the v2 test set ([evaluation](../evaluation/README.md#calibration)). `RAG_RELEVANCE_FLOOR`
overrides all of them. Degradation adds a note to the answer and increments
`adi_rerank_degraded_total`.

## Context

Up to 8 passages within an 8000-character budget; duplicates and fragments are dropped.
Each passage carries its marker, document title and page; table passages carry their
column labels; passages with instruction-like text are marked as data.

## Answering

| Answerer | Setting | Generative | Behaviour |
|---|---|---|---|
| Extractive (default) | *(empty)* | no | Quotes up to three sentences verbatim; cannot hallucinate; will not quote a table header alone or a flagged passage |
| Claude | `…rag.claude.ClaudeAnswerer` | yes | Anthropic API or Bedrock (`RAG_ANSWERER_BACKEND`) |
| OpenAI-compatible | `…rag.openai_compat.OpenAICompatibleAnswerer` | yes | Presets for Gemini, Groq, OpenRouter, Cerebras, Ollama; any `/chat/completions` server |

Generative answerers share one system prompt with the grounding checker (so the two cannot
drift) and **fall back to extractive** on any failure (the OpenAI-compatible answerer sends a
near-zero temperature, `RAG_ANSWERER_TEMPERATURE`; the Claude answerer sends none and is tuned
with `RAG_ANSWERER_EFFORT` instead) —
missing key, rate limit, timeout, malformed response — with a note on the answer and
`adi_llm_fallbacks_total{answerer,reason}`. Keys are read from the environment only, never
from Django settings, and never logged or stored.

**Hosted answerers are tested with stub clients** in `tests_providers.py`, including
through the full engine to prove a fabricated figure is still rejected. The only real calls
were made in development, to the Gemini preset on Google's free tier with a developer's own
key (2026-10-11); no deployment has called one and no evaluation has run through one.

## Grounding

An answer is returned only if:

1. every `[n]` marker resolves to a supplied passage (a bracketed number printed in the
   document itself is not a marker: the extractive answerer quotes it as `(12)`, and an
   out-of-range `[n]` that appears verbatim in a retrieved passage is ignored),
2. it cites at least one passage,
3. **every figure it states** (numbers, percentages, amounts, dates, identifiers) appears
   in a passage it cites — formatting-insensitive (`75%` = `75 percent`,
   `85,000` = `85000`, `Rs 1200` = `Rs1200`, `10 PM` = `10:00 p.m.`, list ordinals and
   markers ignored; structured identifiers such as `REG/2024/117` must match exactly), and
4. at least 25 % of its content words appear in the cited passages (a backstop against
   answering a different subject).

An abstention is recognised however a model writes the sentence - with the quotes the
prompt shows it in, a typographic apostrophe or no full stop - so an honest refusal is
never reported as an ungrounded answer.

Check 3 is the gate. The earlier design gated on a 55 % word-overlap ratio, which rejected
correct, well-written answers and passed a fluent answer that changed 75 to 80; the reasons
are recorded in `grounding.py`.

## Follow-ups

"What about MECH?" carries one content term. When a question looks dependent - an opener
that refers back ("what about", "and for", "is that…"), a pronoun with nothing in the
question it could refer to ("When is *it* due?", but not "How do students renew *their*
cards?", and never "the IT department"), or a single content term - terms from the user's
**own** earlier questions
(never the assistant's answers) are appended for retrieval. Independent questions are
never rewritten. The user's own words travel with it as the **focus**: the extractive
answerer only quotes sentences that address what was actually asked, and generative
prompts state the user's question with the widened query as context. This was a real
defect found end to end — the widened query had made "what about condonation?" re-answer
the attendance question — and is pinned by a regression test that fails without the fix.
`RAG_FOLLOWUP_HISTORY_TURNS=0` disables resolution.

## Planning

Questions that ask several things are split (up to `RAG_MAX_PLAN_STEPS`, 4) and each part
is answered and grounded independently; markers are renumbered into one answer, and parts
that could not be answered are listed. A widened follow-up is planned on the user's own
words, and each part is answered against its own words with the borrowed terms widening its
retrieval - planned on the widened string, the borrowed terms used to split off into a part
that re-asked the previous turn.

## Operational notes

- **Warm-up.** gunicorn workers load the embedder and cross-encoder at start; Celery worker
  children load only the embedder (workers never rerank). `manage.py warm_models` fills the
  cache at deploy time. Measured on this machine: embedder ready in 10.8 s including the
  torch import, cross-encoder 3.2 s; a cold first search dropped from 30.5 s to ~11 s
  under the dev server, and to request-time speed under gunicorn.
- **Memory.** bge-reranker-base is about 1.1 GB on disk and is held by each web worker
  that reranks. Budget web workers accordingly, or use a smaller cross-encoder
  (`RAG_RERANKER_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2`, ~90 MB) after measuring
  its effect with the benchmark.
- **Telemetry.** Per-stage timings (`retrieval`, `rerank`, `generation`, `grounding`) are
  returned with every answer and exported as `adi_rag_stage_duration_seconds{stage}`;
  answerer latency as `adi_llm_duration_seconds{model}`; tokens and characters per model;
  abstentions by reason.
