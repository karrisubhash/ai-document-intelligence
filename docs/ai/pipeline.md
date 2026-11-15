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

Up to 8 passages within an 8000-character budget when the answer will be quoted; up to 12
within 14,000 characters (`RAG_CONTEXT_BUDGET_GENERATIVE`, `RAG_MAX_PASSAGES_GENERATIVE`)
when a model will write it, including - in whatever room is left - the chunk before and
after each of the top 3 passages that cleared the relevance floor (`RAG_NEIGHBOUR_ANCHORS`;
none when every passage was below it), so a list that runs over a chunk boundary arrives
whole.
Duplicates and fragments are dropped. Each passage carries its marker, document title and
page; table passages carry their column labels; passages with instruction-like text are
marked as data. What a model is shown, and why, is in
[llm-providers.md](llm-providers.md#what-a-model-is-shown-and-what-happens-when-it-is-wrong).

## Answering

| Answerer | Setting | Generative | Behaviour |
|---|---|---|---|
| Extractive (default) | *(empty)* | no | Quotes up to three sentences verbatim; cannot hallucinate; will not quote a table header alone or a flagged passage, and declines a question that names a dotted or mixed-case code, an acronym a code-keyed table does not list, or a year when the passages state some other year |
| Claude | `…rag.claude.ClaudeAnswerer` | yes | Anthropic API or Bedrock (`RAG_ANSWERER_BACKEND`) |
| OpenAI-compatible | `…rag.openai_compat.OpenAICompatibleAnswerer` | yes | Presets for Gemini, Groq, Cerebras (needs a payment method), OpenRouter, Mistral, Ollama, OpenAI (and a GitHub Models preset, retired on 30 July 2026, that no longer works); any `/chat/completions` server. Calls go through the LLM gateway (`rag/gateway.py`): a chain of provider:model links (`RAG_ANSWERER_CHAIN`), a daily budget shared by every process, and a reserve kept for chat - see [llm-providers.md](llm-providers.md) |

Generative answerers share one system prompt with the grounding checker (so the two cannot
drift) and **fall back to extractive** on any failure (the OpenAI-compatible answerer sends a
near-zero temperature, `RAG_ANSWERER_TEMPERATURE`; the Claude answerer sends none and is tuned
with `RAG_ANSWERER_EFFORT` instead) —
missing key, rate limit, timeout, malformed response — with a note on the answer and
`adi_llm_fallbacks_total{answerer,reason}`. Keys are read from the environment only, never
from Django settings, and never logged or stored.

**Hosted answerers are tested with stub clients** in `tests_providers.py` and
`tests_llm_gateway.py`, including through the full engine to prove a fabricated figure is
still rejected. Real calls: the Gemini preset on Google's free tier, measured in development
on 2026-10-11 and answering on the public deployment since; on 2026-11-15 the Groq and
OpenRouter presets were each called with a real key to check the model id and the request
shape, and the v3 benchmark was run through Gemini's free tier (`--chat-path --pause 12`,
with a key kept apart from the live site's quota). The Cerebras preset has never been called
(it needs a verified payment method). Those are development checks on synthetic documents,
not an evaluation of any institution's.

**When no model can answer** - no key, every link used up for the day, a provider failure -
the extractive answerer quotes instead, so on a free tier readers see it every day
(`rag/quoting.py`). It still selects sentences by the words they share with the question,
so it cannot follow a paraphrase, but it no longer makes the mistakes that looked like
answers when it was measured with every model call failing:

- A title or clause heading comes after the sentences that qualify, and one that states no
  figure cannot answer "how much" or "when"; such a question prefers a sentence with a
  figure or a date. A "label: value" line ("Casual leave: 12 days per calendar year") and a
  list item are facts, not headings, and are quoted like any sentence; a label followed by
  a title ("Policy: Staff Leave Policy 2024-25") is a heading, so the sentence under it is
  not dropped for it.
- A table row is matched together with its column labels, and dotted codes ("B.Tech",
  "M.Tech") are single terms, so the wrong programme's row is not quoted first.
- Each part of a two-part question can be answered by its own sentence. A second sentence
  is added only when it brings terms of its own - not the document title's words - and one
  that brings just a single term must also share a term with the first sentence, so a
  sentence that merely uses a stray word of the question ("applied", "considered") is not
  appended. Two kinds of term never earn a second sentence: a number the asker gave about
  themselves ("60" in "a student with 60 percent attendance"), and a word the answer already
  has in another form ("examination" for a row under "Exam Fee"). The price of this rule is
  that a sentence about a neighbouring case can still follow the right one ("An
  undergraduate student may borrow up to four books" after the postgraduate limit), because
  a word-overlap ranker cannot tell it from the sentence that holds the missing term.
- A code or a year in the question that the documents show a gap for makes it decline
  instead of quoting the nearest thing: a dotted or mixed-case code ("B.Arch", "PhD")
  that no retrieved passage mentions, where the passages enumerate codes (prose that names
  programmes, or a code-keyed table whose columns the question asks about; an attendance
  rule that names none applies to every programme); an acronym ("MBA") only where it stands
  where a row key would, right after "for", "of" or "in" or beside a dotted code; a year
  when the passages state some other year ("2023-24" against the 2024-25 circular).
  A year-less rule is not refused because the question happens to mention a year, and
  emphasis words and common abbreviations ("PLEASE", "HOD", "CGPA") are not codes.
- What the user says about themselves ("I am a B.Tech student", "As a PhD scholar", "I joined
  in 2022", "I am in the 2021-25 batch") is not what is asked. It is not refused for the
  programme or year it names, and the words of it that no passage contains are not held
  against the sentence that answers the rest (a programme a table lists still picks its
  row).
- A sentence cut by a chunk boundary is marked with an ellipsis.

Measured offline on benchmarks v2 and v3 these changes removed wrong answers to questions
that should be refused and the unrelated sentences that used to be appended to correct
answers. The `correctness` metric is recall-only and has known false positives (see the
docstring of `evaluation/correctness.py`), so read the per-case results, not only the
number.

Every answer records which model actually wrote it and what the provider reported
(`Message.metadata.llm`), and the chat payload names it for people (`model_display`) and
says when the answer was quoted because no model could take it (`degraded`).

## Grounding

An answer is returned only if:

1. every `[n]` marker resolves to a supplied passage (a bracketed number printed in the
   document itself is not a marker: the extractive answerer quotes it as `(12)`, and an
   out-of-range `[n]` that appears verbatim in a retrieved passage is ignored),
2. it cites at least one passage,
3. **every figure it states** (numbers, percentages, amounts, dates, identifiers) appears
   in a passage it cites — formatting-insensitive (`75%` = `75 percent`,
   `85,000` = `85000`, `Rs 1200` = `Rs1200`, `10 PM` = `10:00 p.m.`,
   `31 March 2025` = `31.03.2025`, `2024-25` = `2024-2025`, `7` = a passage's "seven days"
   (a bare "one" only before a unit of time), `7:30 PM` = `7.30 pm`, `3 lakh` = `300000`,
   `15-day` = `15 days`, list numbering and markers ignored; a year the cited document's
   title states counts; a figure the user gave about their own situation ("I have 70
   percent attendance") may be repeated, though never cited, while a figure they only asked
   about must be in a cited passage; a hyphenated range `A-B` (or with an en dash) needs
   both ends together in one passage; structured identifiers such as `REG/2024/117`, a
   date that can be read two ways, and a date that does not exist must match exactly), and
4. at least 25 % of its content words appear in the cited passages (a backstop against
   answering a different subject).

An abstention is recognised however a model writes the sentence - with the quotes the
prompt shows it in, a typographic apostrophe or no full stop - so an honest refusal is
never reported as an ungrounded answer.

Check 3 is the gate. The earlier design gated on a 55 % word-overlap ratio, which rejected
correct, well-written answers and passed a fluent answer that changed 75 to 80; the reasons
are recorded in `grounding.py`.

A model's answer that fails check 3 may have a missing citation added, or be asked for once
more, before it is withheld; the rules, and the counter that records each case, are in
[llm-providers.md](llm-providers.md#what-a-model-is-shown-and-what-happens-when-it-is-wrong).
If the check itself raises, the answer is withheld, never shown unchecked.

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

When the answer will be quoted, questions that ask several things are split (up to
`RAG_MAX_PLAN_STEPS`, 4) and each part is answered and grounded independently (a model
reads the question whole unless `RAG_PLAN_WHEN_GENERATIVE` is on); markers are renumbered into one answer, and parts
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
- **Rerank cost.** The cross-encoder's forward pass is nearly all of a question's CPU time.
  Each distinct passage is scored once per question, and each worker process remembers up
  to `RAG_RERANKER_LOCAL_CACHE_SIZE` (4,096) scored pairs, per user, so a question a user
  asks again of the same worker reranks in milliseconds; `0` turns that off. Scores are
  never shared between users: a remembered one makes an answer seconds faster, which
  would tell one user what another had asked. Measured in
  [performance](../operations/performance.md#chat-latency-on-the-development-stack).
- **Telemetry.** Per-stage timings (`retrieval`, `rerank`, `generation`, `grounding`) are
  returned with every answer and exported as `adi_rag_stage_duration_seconds{stage}`;
  answerer latency as `adi_llm_duration_seconds{model}`; tokens and characters per model;
  abstentions by reason; first drafts that failed grounding and were then repaired or
  retried as `adi_rag_grounding_recoveries_total{outcome}`.
