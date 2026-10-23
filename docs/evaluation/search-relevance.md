# Search relevance

Why the Search page ranked irrelevant passages near the top, what changed, and how it was
measured. Every number here was produced by the commands on this page. **The benchmark is
synthetic** (see [datasets](../data/datasets.md#search-relevance-search-relevance-s1)): a
score here says the ranking behaves as designed on documents written for this repository,
not that it reaches this accuracy on an institution's documents.

## Root cause

Traced on the development database (1,729 documents, 7,503 indexed chunks, mostly OCR'd
RVL-CDIP scans plus a handful of clean regulations; bge-small embeddings, MiniLM
cross-encoder configured for chat), calling `search.service.search` directly.

`POST /api/v1/search/` (`search/views.py`) returned `search.service.search` as is:
permission filter, then lexical and vector retrieval, then reciprocal-rank fusion. Each
stage was working as written. The ranking failures came from what fusion cannot know:

1. **Fusion has no notion of relevance** (`service._fuse`). RRF scores a passage only by
   its rank in each list, so rank 1 in either list is worth the same 1/61. When the lexical
   and vector lists disagree, the result alternates between them. "library late return
   fine" put an OCR fragment reading `Final Results Deadline: I 2` (first in the vector
   list, absent from the lexical one) above the Library Rules clause that lexical search
   ranked first. "REG/2024/117" put a fee-structure header that matched only `2024` above
   the regulation carrying that exact reference.
2. **The vector half always returns its nearest 50, however far** (`service._vector`).
   Short OCR fragments sit close to almost every query in bge-small's space, so they
   arrived as candidates for every search.
3. **The lexical half matches any single query term** (`service._build_lexical_query`
   ORs the terms on purpose, so that long questions are not ANDed into nothing). One
   question matched 1,284 chunks; "quantum chromodynamics lattice gauge theory", which
   no document is about, matched a mechanics textbook on "theory".
4. **Nothing ranked the fused list for the Search page.** The chat engine reranks the same
   candidates with the configured cross-encoder and applies a relevance floor
   (`rag/engine.py`); the search view never called either. So every query, including one
   with no relevant document, got a full page in fused order.

Checked and **not** the cause:

| Suspect | Finding |
|---|---|
| Vector search disabled or mis-wired | Enabled and contributing (`matched_by` shows both halves; 7,503 chunks embedded by the configured model) |
| Scores from different retrievers mixed | Not mixed: RRF uses ranks only, which is correct for incomparable scores — the problem is the absence of any relevance signal, not the fusion arithmetic |
| Duplicate or superseded passages | Already excluded: `authorized_chunks` keeps the current version's current extraction pass only (tested in `tests_search.py`) |
| Retrieval missing the answer | On the synthetic corpus the answer was a candidate for every query (pool recall 1.000); with 2,000 OCR scans added, one paraphrase was never retrieved (see [limitations](#known-limitations)) |
| Permissions | Enforced before any similarity is computed (`authorized_chunks`, then filters); the new ranking stage only reorders what that returned — tested, including that another department's passage is never shown to the reranker |

## The fix

`search/ranking.py`, called by the Search view instead of `service.search`:

1. When `RAG_RERANKER` is a cross-encoder, the top `SEARCH_RERANK_DEPTH` (20, and never
   fewer than the requested `limit`) fused candidates are reranked by it.
2. Passages scoring below the **search** relevance floor are left off the page. They are
   not deleted: `include_below_floor: true` returns the matches (`limit` of them at most)
   followed by **every** passage scored below the floor, each marked `below_floor` — so a
   response can hold more than `limit` passages, and the page's "Show N weaker matches"
   shows exactly N. When **nothing** clears the floor, the response says so (a `notes`
   entry; the page shows "No close match") and returns only the `SEARCH_FALLBACK_RESULTS`
   (3) best-ranked passages, each marked `below_floor` — never a page of unrelated
   passages presented as results, and never an empty page that hides an answer the floor
   could not recognise.
3. With the bundled lexical reranker — or a cross-encoder that failed and fell back to
   lexical scoring — the fused order is returned unchanged (measured below: lexical
   reranking made search worse).
4. A cross-encoder with no calibrated search floor reorders but hides nothing.
5. Search gains a per-minute budget (`THROTTLE_SEARCH_BURST`, 20/minute) beside the
   hourly one (`THROTTLE_SEARCH`, 120/hour): a reranked search costs seconds of CPU (see
   [latency](#latency-and-cost)), and the hourly budget alone could be spent in seconds.

`service.search` is unchanged, so chat's candidate generation is unchanged. The response
gains `limit`, `max_limit`, a `ranking` block (`reranker`, `degraded`, `relevance_floor`,
`reranked`, `below_floor`) and, per hit, `relevance` and `below_floor`; every existing field
keeps its meaning. Citations are the stored chunk exactly (document id, page, character
offsets, text) — tested. The Search page sends no `limit`, so the server's
`SEARCH_DEFAULT_LIMIT` sets its page size.

## The benchmark

`evaluation/search-relevance.template.json` (`search-relevance` s1): 55 cases over the 16
synthetic corpus documents — exact (11), identifier (5), paraphrase (13), ambiguous (5),
common-term (6), repeated-terminology (6) and no-relevant (9). A passage is relevant when it
belongs to the labelled document and contains the labelled evidence text, so labels survive
re-chunking. The split (19 dev, 36 test) was fixed in the template before anything was
measured, and the template, distractors and harness were committed (e83333a) before any
change to search.

```bash
python manage.py seed_evaluation --template ../evaluation/search-relevance.template.json \
    --user search-evaluator --department "Search evaluation" \
    --output ../evaluation/search-relevance.json
python manage.py evaluate_search --as-user search-evaluator --split test --show-cases
```

`evaluate_search` sends every query through `SearchView` itself and scores what it returns:
Recall@5 (per evidence item), Precision@5 (over what was returned), MRR, nDCG@5, the same at
document level, the share of no-relevant queries answered "no match" (nothing returned, or
only passages marked below the floor), and the share of answerable ones shown at least one
match. A second call to the retrieval primitive records the candidate pool, so every miss is
filed as **retrieval** (never a candidate) or **ranking** (a candidate, ordered or filtered
out of the top 5).

It refuses to produce misleading numbers: a run in which the configured reranker came back
degraded is rejected (those numbers would describe retrieval order under the model's name),
and the report records what actually ordered the results (`endpoint_ranking`). A baseline
from a different exam — dataset, version, k, limit or split — is refused rather than
compared; a gated metric missing from the run fails the gate; and `--output X --baseline X`
compares against the old `X` before replacing it, and keeps it if the run failed.

Two corpora were scored:

- **synthetic** — the 16 documents alone (reproducible anywhere; this is what CI gates);
- **synthetic + OCR** — the same 16 plus 2,000 QS-OCR scans (`ingest_corpus --source qs-ocr
  --limit 2000`), owned by a second evaluator in the same department, so the scans are real
  OCR noise nobody wrote for this test. Their topics are archival business documents, so the
  no-relevant labels are less certain here: "fire drill evacuation procedure" matched
  material-safety sheets with "SPECIAL FIRE-FIGHTING PROCEDURES".

## Decisions, made on the dev split

### Lexical reranking makes search worse

Dev split, synthetic corpus unless noted, rerank depth 30, limit 10:

| Configuration | Fused order (before) MRR | Lexical rerank, no floor, MRR |
|---|---|---|
| hashing embeddings | 0.844 | 0.733 |
| bge-small embeddings | 0.870 | 0.742 |
| bge-small, synthetic + OCR | 0.615 | 0.550 |

The lexical reranker cannot see a passage that answers in other words, and the vector half
can: it pushed two paraphrase answers from fused rank 1 and 3 to rank 7. No lexical floor
(0.1-0.5) gave a useful trade either — no-relevant queries scored 0.24-0.43 against a chat
floor of 0.15. Hence rule 3 above.

### The chat floor hides paraphrases; no score separates them

MiniLM's chat floor (0.30) left the answer to **every** paraphrased dev query off the page
(dev recall 0.973 → 0.643). Per dev case, the best relevant and best irrelevant passage's
cross-encoder score (synthetic corpus):

| Case | Kind | Relevant | Best irrelevant | bge-small cosine (relevant / best other) |
|---|---|---|---|---|
| pa-cheating | paraphrase | 0.0002 | 0.0002 | 0.661 / 0.640 |
| pa-recheck | paraphrase | 0.0000 | 0.0000 | 0.540 / 0.595 |
| pa-lawyer | paraphrase | 0.0091 | 0.0000 | 0.702 / 0.532 |
| pa-guests | paraphrase | 0.0798 | 0.0001 | 0.768 / 0.634 |
| nr-thesis | no-relevant | — | 0.0002 | — / 0.608 |
| nr-sports-quota | no-relevant | — | 0.0001 | — / 0.695 |
| nr-fire-drill | no-relevant | — | 0.0000 | — / 0.584 |
| (exact, identifier, common-term, repeated-terminology) | | 0.40-0.9999 | | |

Neither MiniLM nor the embedding cosine separates "a paraphrase of the answer" from
"nothing relevant". bge-reranker-base did no better (pa-cheating 0.0002, ranked 10th). The
cross-encoder's **ordering** is still good — the relevant passage ranked 1-3 for every dev
paraphrase — so the floor has to be a trade-off, and below-floor passages stay one request
away.

### The search floor

Rule, written before the sweep: choose the floor maximising *E − 2H*, summed over both
corpora, where *E* is the share of dev no-relevant queries returning nothing and *H* the
share of dev answerable queries whose every relevant passage is left off; ties go to the
lower floor.

| MiniLM floor | Synthetic H | Synthetic E | Synth. + OCR H | Synth. + OCR E | Sum of E − 2H |
|---|---|---|---|---|---|
| 0 | 0.000 | 0.000 | 0.062 | 0.000 | −0.125 |
| 0.0001 | 0.062 | 0.667 | 0.062 | 0.000 | 0.417 |
| 0.0003 | 0.125 | 1.000 | 0.125 | 0.000 | 0.500 |
| 0.001 | 0.125 | 1.000 | 0.125 | 0.333 | 0.833 |
| 0.003 | 0.125 | 1.000 | 0.125 | 0.333 | 0.833 |
| 0.01 | 0.188 | 1.000 | 0.188 | 0.333 | 0.583 |
| **0.03** | 0.188 | 1.000 | 0.188 | 0.667 | **0.917** |
| 0.1 | 0.250 | 1.000 | 0.250 | 0.667 | 0.667 |
| 0.3 (chat floor) | 0.312 | 1.000 | 0.312 | 1.000 | 0.750 |

So `CALIBRATED_FLOORS["cross-encoder/ms-marco-MiniLM-L6-v2"] = 0.03`. The choice is
fragile: the dev split has 3 no-relevant queries per corpus, and 0.03 beats 0.001 only
because of one OCR passage (a curriculum vitae scoring 0.0175 for "PhD thesis submission
format").

**bge-reranker-base is left uncalibrated** (reorders, hides nothing). Its synthetic dev
sweep peaked at 0.01, but the synthetic + OCR half of the rule never completed: Docker
Desktop stopped twice while that 1.1 GB model ran beside the other stacks on this 8 GB
machine. Its dev ordering was also worse than MiniLM's (MRR 0.850 vs 0.896).

### The fallback — decided after scoring the test split

The first version left the page **empty** whenever nothing cleared the floor. Scored on the
test split, that collapsed paraphrase recall: 9 of 13 paraphrased questions got "No close
match" and nothing else, although MiniLM ranked their answer first to third (synthetic
corpus, test split: Recall@5 0.987 → 0.773, MRR 0.928 → 0.800 against the old endpoint;
paraphrase Recall@5 1.000 → 0.308). The dev split had shown the same thing — 3 of its 4
paraphrases hidden — on too few cases to weigh it properly.

The fix is structural, not a re-tuned number: the floor value stays 0.03, and when nothing
clears it the three best-ranked passages are returned, marked as weaker matches. Three
because every dev paraphrase had its answer at reranked rank 1-3; `SEARCH_FALLBACK_RESULTS=0`
restores the strict behaviour. **Because this change was made after looking at the test
split, the test numbers below are no longer a fully independent estimate** — they are
reported for both versions so the effect of the change is visible.

### Rerank depth

Replayed from one MiniLM pass per query. A cross-encoder scores each (query, passage) pair
on its own, and the candidates at a smaller depth are a prefix of those at a larger one, so
one pass at depth 30 gives the exact result at every shallower depth; the replay's depth-30
rows reproduced the measured end-to-end results to the third decimal on both corpora and
both splits, and the shipped depth was later measured end-to-end and matched it case for
case (`evaluation/scripts/search_depth_replay.py`; usage in its docstring). Dev split, MRR /
nDCG@5 / Recall@5:

| Depth | Synthetic | Synthetic + OCR |
|---|---|---|
| 30 | 0.896 / 0.875 / 0.926 | 0.865 / 0.818 / 0.855 |
| **20** | 0.906 / 0.884 / 0.926 | 0.865 / 0.837 / 0.855 |
| 15 | 0.969 / 0.930 / 0.926 | 0.865 / 0.842 / 0.855 |
| 10 | 0.969 / 0.930 / 0.926 | 0.844 / 0.827 / 0.846 |

Deeper is not safer: MiniLM overrates some deep OCR junk that fusion had rightly put low,
so 30 loses a little to 20, and 20 to 15. At 10 recall falls — on the noisy corpus the
deepest relevant passage sits at fused rank 11 (test split Recall@5 0.940 → 0.883). The
default is **20**: at least as good as 30 on every dev metric above, a third fewer pairs to
score (measured: about a quarter less CPU per search, [below](#latency-and-cost)), and about
twice the headroom over rank 11 for larger corpora, which push relevant passages deeper. 15
scored best and halves 30's pairs; it is the option for latency-sensitive deployments, with
less margin. The first default, 30, had been chosen for headroom alone,
before this replay existed. The replay printed the test split beside dev; test agrees
(below).

## Results

"Before" is the old endpoint: the pre-fix commit e83333a, and `SEARCH_RERANK_DEPTH=0` on the
new code, gave identical top-5 lists and counts on all 55 cases. "After" is the shipped
default — bge-small embeddings, the MiniLM cross-encoder (the pilot configuration), depth
20, floor 0.03, fallback 3 — measured end-to-end through `SearchView` with
`evaluate_search` (2026-10-20), and identical, case for case, to the replay above. Measured
on the disposable measurement database. Limit 10, metrics at 5.

### Test split (36 cases: 30 answerable, 6 no-relevant)

| | Synthetic, before | Synthetic, after | Synthetic + OCR, before | Synthetic + OCR, after |
|---|---|---|---|---|
| Recall@5 | 0.987 | 0.973 | 0.800 | **0.940** |
| Precision@5 | 0.247 | **0.632** | 0.187 | **0.572** |
| MRR | 0.928 | **1.000** | 0.766 | **0.911** |
| nDCG@5 | 0.941 | **0.978** | 0.759 | **0.916** |
| Document Recall@5 / MRR | 1.000 / 0.967 | 0.975 / 1.000 | 0.808 / 0.789 | 0.942 / 0.911 |
| Candidate-pool recall (retrieval) | 1.000 | 1.000 | 0.967 | 0.967 |
| No-relevant queries answered "no close match" | 0 / 6 | **6 / 6** | 0 / 6 | **6 / 6** |
| Answerable queries shown at least one match | 30 / 30 | 24 / 30 | 30 / 30 | 24 / 30 |
| Passages returned, answerable / no-relevant | 10 / 10 | 2.6 / 3 (marked) | 10 / 10 | 2.9 / 3 (marked) |

At depth 30, measured end-to-end through the view: synthetic Recall@5 0.973, Precision@5
0.632, MRR 0.983, nDCG@5 0.966; synthetic + OCR 0.940, 0.566, 0.906, 0.912.

Where each answerable query landed (test split):

| | Synthetic, before | Synthetic, after | Synthetic + OCR, before | Synthetic + OCR, after |
|---|---|---|---|---|
| Relevant passage first | 26 | 30 | 21 | 26 |
| In the top 5, not first | 4 | 0 | 3 | 3 |
| Retrieved, but not in the top 5 (**ranking** failure) | 0 | 0 | 5 | **0** |
| Never retrieved (**retrieval** failure) | 0 | 0 | 1 | 1 |

On the noisy corpus every ranking failure is gone; the one retrieval failure remains (see
[limitations](#known-limitations)).

### Dev split (19 cases), for completeness

| | Synthetic, before → after | Synthetic + OCR, before → after |
|---|---|---|
| Recall@5 | 0.973 → 0.926 | 0.783 → 0.855 |
| Precision@5 | 0.300 → 0.641 | 0.200 → 0.557 |
| MRR | 0.870 → 0.906 | 0.615 → 0.865 |
| nDCG@5 | 0.881 → 0.884 | 0.621 → 0.837 |
| No-relevant answered "no close match" | 0 / 3 → 3 / 3 | 0 / 3 → 2 / 3 |

The noisy dev miss is "fire drill evacuation procedure", answered with a material-safety
sheet's fire-fighting procedure above the floor.

### By kind (all 55 cases, synthetic + OCR): Recall@5 / Precision@5 / MRR

| Kind | Before | After |
|---|---|---|
| exact (11) | 1.000 / 0.218 / 0.927 | 1.000 / 0.689 / 1.000 |
| identifier (5) | 1.000 / 0.240 / 0.750 | 1.000 / 0.570 / 0.900 |
| paraphrase (13) | 0.538 / 0.108 / 0.471 | 0.846 / 0.513 / 0.808 |
| ambiguous (5) | 0.307 / 0.240 / 0.384 | 0.576 / 0.453 / 0.633 |
| common-term (6) | 1.000 / 0.233 / 0.889 | 1.000 / 0.672 / 1.000 |
| repeated-terminology (6) | 1.000 / 0.200 / 0.917 | 1.000 / 0.444 / 1.000 |
| no-relevant (9), answered "no close match" | 0 / 9 | 8 / 9 |

On the synthetic corpus alone two kinds moved down: paraphrase MRR 0.949 → 0.923 (Recall@5
stays 1.000), and ambiguous Recall@5 0.834 → 0.604 (MRR 0.767 → 1.000) — see
[limitations](#known-limitations).

### The strict first version, for comparison

Without the fallback (`SEARCH_FALLBACK_RESULTS=0`), depth 30, test split: synthetic
Recall@5 0.773, Precision@5 0.565, MRR 0.800, nDCG@5 0.778; synthetic + OCR Recall@5 0.773,
Precision@5 0.511, MRR 0.761, nDCG@5 0.762; no-relevant queries returned nothing 6 / 6 on
both.

### Offline configuration (CI)

Hashing embeddings and the lexical reranker keep the fused order, so the endpoint returns
exactly what it did before (unit-tested, and each regenerated CI baseline's top 5 matched the
previous one case for case). Test split: Recall@5 0.920, Precision@5 0.233, MRR 0.765, nDCG@5
0.804 — the numbers `evaluation/search-baseline.json` gates.

## Latency and cost

The cross-encoder pass is nearly the whole cost, and it grows with the number of passages it
reads (`SEARCH_RERANK_DEPTH`) and their length. Measured on the development laptop — an
8-vCPU WSL2 VM shared with other running stacks, no GPU — with MiniLM-L6, 55 queries each:

| Synthetic corpus | Before | After, depth 30 | After, depth 20 |
|---|---|---|---|
| Single-threaded, interleaved: wall p50 / p95 | 287 / 456 ms | 8,252 / 10,920 ms | 6,332 / 8,427 ms |
| Same run: CPU time p50 | 237 ms | 7,929 ms | 6,118 ms |

End-to-end through the view with every core, all 55 queries, during the measurement runs
(other workloads running, before and after not interleaved, so treat these as ranges):

| | Before | After, depth 30 | After, depth 20 |
|---|---|---|---|
| Synthetic, p50 / p95 | 110 / 977 ms | 2,176 / 3,540 ms | 2,880 / 4,329 ms |
| Synthetic + OCR, p50 / p95 | 185 / 374 ms | 5,295 / 8,861 ms | 2,283 / 4,572 ms |

The depth-20 synthetic run being slower than the depth-30 one is the shared machine, not
the depth: the controlled single-threaded run above is the comparison to trust. OCR
passages are longer than the synthetic ones, so each pair costs more.

So on this hardware a search goes from a fraction of a second to a few seconds, and costs
several CPU-seconds: an 8-core host serves on the order of one reranked search per second.
What follows:

- `THROTTLE_SEARCH_BURST` (20/minute) bounds how much of that one account can take at
  once; `THROTTLE_SEARCH` (120/hour) still bounds the hour.
- Cheaper: `SEARCH_RERANK_DEPTH=15` (half of 30's pairs; quality above), a GPU, or a faster
  runtime for the cross-encoder — search and chat share the reranker, so a speed-up for one
  is a speed-up for both. `SEARCH_RERANK_DEPTH=0` restores the old cost and the old ranking.

### Examples on the development database

Old and new ordering for the same queries on the development database (the OCR-heavy corpus
the failure was reported on), same user, top 5, bge-small + MiniLM at depth 20 (depth 30
returned the same passages). Read-only: both calls were made to the search functions
directly, so nothing was written. Titles as stored.

| Query | Old (fused order) | New |
|---|---|---|
| library late return fine | 1. OCR fragment "Final Results Deadline: I 2" · 2. Library Rules · 3-5. an unrelated software report (two copies) and an OCR'd fax log | 1. Library Rules (relevance 0.999) — 19 others below the floor |
| hostel curfew on Saturdays | 1. Academic Regulations R24 · 2. Hostel Regulations · 3-5. OCR scans | 1. Hostel Regulations (1.000) · 2. Academic Regulations R24 (0.999) |
| REG/2024/117 | 1. Fee Structure 2024-25 (matched "2024") · 2. the regulation carrying REG/2024/117 · 3-5. OCR scans | 1. the regulation carrying REG/2024/117 (0.999) |
| What is the minimum attendance required to sit the end-semester examination? | 1-3. academic-regulation passages · 4. a scanned exam-schedule passage that does not mention attendance · 5. an attendance circular | 1-4. academic-regulation passages (0.995-1.000) · 5. the attendance circular (0.678) |
| quantum chromodynamics lattice gauge theory | ten passages of a mechanics textbook that mentions "theory" | "no close match": the 3 closest textbook passages, marked weaker (best 0.003) |

## Configuration and deployment

| Setting | Default | |
|---|---|---|
| `SEARCH_RERANK_DEPTH` | 20 | Fused candidates the cross-encoder reads per search. `15` is cheaper (above); `0` restores the old behaviour exactly (verified: identical top 5 on all 55 cases) |
| `SEARCH_RELEVANCE_FLOOR` | unset | Unset: `ranking.CALIBRATED_FLOORS` for the configured model, else no floor |
| `SEARCH_FALLBACK_RESULTS` | 3 | Best-ranked passages returned, marked, when none clears the floor; `0` returns none |
| `SEARCH_DEFAULT_LIMIT` / `SEARCH_MAX_LIMIT` | 10 / 50 | The Search page sends no `limit`, so it shows the default; "Show more" adds one more page of that size, up to `max_limit`. (It used to ask for a fixed 20, which a smaller configured maximum would have rejected) |
| `THROTTLE_SEARCH_BURST` | 20/minute | Per-account search budget per minute, beside the hourly `THROTTLE_SEARCH` |

- **Model.** The relevance stage needs a cross-encoder: `RAG_RERANKER=
  documents.rag.local_rerank.LocalCrossEncoderReranker` with
  `RAG_RERANKER_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2` (91 MB, the image's
  `INSTALL_LOCAL` layer; already the pilot configuration). It is loaded once per process,
  and the chat path already loads it. Without it, search behaves exactly as before.
- **Cost.** One cross-encoder pass over 20 passages per search — seconds of CPU on the pilot
  hardware ([latency](#latency-and-cost)).
- **API.** Additive. A client that ignores the new fields sees fewer, better-ordered
  results; where it used to get unrelated passages it now gets a `notes` entry and at most
  three passages marked `below_floor`. With `include_below_floor`, a response can hold more
  than `limit` passages (the weaker ones are appended).
- **Changing the model** (or `RAG_RERANKER_LOCAL_MODEL`) means re-measuring the floor:
  sweep on the dev split with the same rule and add the model to `CALIBRATED_FLOORS`, or set
  `SEARCH_RELEVANCE_FLOOR`.
- **CI.** The `ai-regression` job seeds this benchmark under its own user and department and
  gates on `evaluation/search-baseline.json` with the offline configuration (no model). With
  the lexical reranker the endpoint keeps retrieval order, so that gate covers retrieval and
  fusion only. The cross-encoder path — floor, fallback, `include_below_floor`, depth — is
  covered by unit tests with a stand-in cross-encoder (`tests_search_relevance.py`), which also
  pin the shipped MiniLM floor, fallback and depth; its quality is measured locally, as above.

## Review

A release review of the branch (five reviewers and adversarial verifiers, in a separate
session) reported eight issues; all were confirmed and fixed, each with a regression test
shown to fail on the reviewed commit (bdf7ef4): "Show N weaker matches" could return the same
matches again; a page test could not fail; focus dropped to the page body after revealing
weaker matches; search had no per-minute budget; the CI comment overstated what the gate
covers, and nothing pinned the shipped floor and fallback (now pinned and mutation-checked);
`evaluate_search` could score a degraded reranker under the model's name, compare runs of
different exams, and compare a refreshed baseline with itself.

## Known limitations

- **Synthetic.** 55 cases, written and labelled by one author; labels were checked
  mechanically, not by a second reader. No institutional relevance judgments exist.
- **Slower search.** A few seconds per search on CPU, where it was a fraction of one
  ([latency](#latency-and-cost)).
- **"No close match" is sometimes said over an answer.** One answerable test query in five
  (the paraphrases MiniLM scores below 0.03) is answered with "No close match" and its three
  closest passages — the answer is among them, but labelled a weaker match. No score
  available here separates those queries from ones nothing answers.
- **Ambiguous one-word queries lose secondary passages.** For "fee" or "late", the
  passages that clear the floor are all relevant (MRR 1.0), but other relevant ones fall
  below it: Recall@5 on that kind dropped (synthetic 0.834 → 0.604). They are one click away.
- **The floor is set on very little data**: 3 no-relevant dev queries per corpus, one of
  which (an OCR material-safety sheet for "fire drill") is arguably not irrelevant.
- **One retrieval failure is not addressed.** With 2,000 scans added, "deadline to ask for
  my answer script to be marked again" never retrieves the revaluation clause: it shares no
  term with it, and bge-small does not rank it in the nearest 50. Reranking cannot recover a
  passage retrieval never returned; better embeddings or query expansion could, and neither
  was added (the spec for this work asked to prove a simpler fix insufficient first).
- **Uncalibrated rerankers** (bge-reranker-base, Bedrock) reorder without a floor.
- **The chat path is unchanged.** It already reranked; its floor and depth are untouched.
