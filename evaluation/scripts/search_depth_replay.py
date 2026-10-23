"""Search quality at several SEARCH_RERANK_DEPTHs from ONE cross-encoder pass.

The depth table in docs/evaluation/search-relevance.md came from this. Run
from backend/, after seeding the search-relevance corpus (see that page),
with the cross-encoder configured:

    python manage.py shell -c "import sys; sys.argv = ['replay', \
        'search-evaluator', 'search-evaluator', '30,20,15,10', '10']; \
        exec(open('../evaluation/scripts/search_depth_replay.py').read())"

Arguments: the user to search as, the user the corpus was seeded under, the
depths (the first must be the deepest), the result limit, and optionally a
directory to write each depth's per-case scores to as JSON.

Exact, not an estimate, for two reasons: a cross-encoder scores each
(query, passage) pair on its own, so a passage's score does not depend on
which other candidates share its batch; and service.search fuses the same
lexical and vector lists whatever its `limit`, then slices, so the
candidates at depth d are the first max(limit, d) of those at the deepest
depth. The replay mirrors ranking.search/rank step for step, and its
deepest row must reproduce `evaluate_search` at that depth - if it does
not, the replay is wrong and nothing it prints should be used.
"""

import json
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model

from documents.evaluation import search as scoring
from documents.models import Document
from documents.rag import engine
from documents.search import ranking, service

as_user, owner_name, depths, limit = (
    sys.argv[1],
    sys.argv[2],
    sys.argv[3],
    int(sys.argv[4]),
)
out_dir = Path(sys.argv[5]) if len(sys.argv) > 5 else None
depths = [int(d) for d in depths.split(",")]
deepest = max(depths)
users = get_user_model().objects
user, owner = users.get(username=as_user), users.get(username=owner_name)
# Not __file__: under `manage.py shell -c "exec(...)"` there is none.
template = (
    Path(settings.BASE_DIR).parent / "evaluation" / "search-relevance.template.json"
)
data = json.loads(template.read_text(encoding="utf-8"))
docs = dict(
    Document.objects.filter(owner=owner, title__startswith="[eval] ").values_list(
        "title", "id"
    )
)
reranker = engine.get_reranker()
floor = ranking.relevance_floor(reranker)
fallback = ranking.fallback_results()
sys.stdout.write(
    f"reranker {reranker.name} {getattr(reranker, 'version', '')} "
    f"floor {floor} fallback {fallback} limit {limit}\n"
)

chunks_q, _ = service.authorized_chunks(user)
per_depth = {d: [] for d in depths}
scored = {}


def passes(hit):
    return floor is None or scored[hit.chunk.id].score >= floor


for case in data["cases"]:
    evidence, owners = [], {}
    for label in case["relevant"]:
        did = docs["[eval] " + label["document"].lstrip("@")]
        ids = {
            c.id
            for c in chunks_q.filter(document_version__document_id=did)
            if scoring.contains_evidence(c.text, label["contains"])
        }
        assert ids, (case["id"], label)
        evidence.append(ids)
        for i in ids:
            owners[i] = did

    pool_hits = service.search(user, case["query"], limit=1000).hits
    pool = [h.chunk.id for h in pool_hits]
    for h in pool_hits:
        owners.setdefault(h.chunk.id, h.chunk.document_version.document_id)

    fused = service.search(user, case["query"], limit=max(limit, deepest)).hits
    scored = {}
    if fused:
        reranked = reranker.rerank(case["query"], fused)
        assert not engine.was_degraded(reranked), "the reranker degraded"
        scored = {r.chunk.id: r for r in reranked}

    for depth in depths:
        candidates = fused[: max(limit, depth)]
        # The reranker's own order: score, then retrieval score, then id.
        order = sorted(
            candidates,
            key=lambda h: (-scored[h.chunk.id].score, -h.score, h.chunk.id),
        )
        above = [h for h in order if passes(h)]
        below = [h for h in order if not passes(h)]
        returned = above[:limit] if above else below[:fallback][:limit]
        ranked = [h.chunk.id for h in returned]
        marked = {h.chunk.id for h in returned if not passes(h)}
        per_depth[depth].append(
            scoring.score_case(
                case, ranked, owners, evidence, pool, k=5, below_floor=marked
            )
        )

for depth in depths:
    scores = per_depth[depth]
    sys.stdout.write(f"\n=== depth {depth}\n")
    for split in ("all", "dev", "test"):
        subset = scores if split == "all" else [s for s in scores if s.split == split]
        agg = scoring.aggregate(subset, 5)
        q, d = agg["quality"], agg["diagnostics"]
        sys.stdout.write(
            f"  [{split:4}] R@5 {q['recall_at_5']:.3f}  P@5 {q['precision_at_5']:.3f}  "
            f"MRR {q['mrr']:.3f}  nDCG@5 {q['ndcg_at_5']:.3f}  "
            f"with-a-match {q['answerable_with_a_match']:.3f}  "
            f"no-rel-said-no-match {q['no_relevant_said_no_match']:.3f}  "
            f"outcomes {d['outcomes']}\n"
        )
    if out_dir:
        path = out_dir / f"depth-{owner_name}-{as_user}-{depth}.json"
        path.write_text(json.dumps([s.to_dict() for s in scores]), encoding="utf-8")
