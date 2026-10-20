"""Score the Search endpoint against a passage-level relevance set.

    python manage.py seed_evaluation --template ../evaluation/search-relevance.template.json \
        --user search-evaluator --department "Search evaluation" \
        --output ../evaluation/search-relevance.json
    python manage.py evaluate_search --as-user search-evaluator --split test

Each query goes through `SearchView` itself, as the evaluator, so what is
scored is exactly what the Search page receives: the permission filter, the
retrieval, and whatever ordering and selection the endpoint applies. A
second call to the retrieval primitive, `search.service.search`, records the
candidate pool, which is what lets a miss be filed as a RETRIEVAL failure
(never a candidate) or a RANKING failure (a candidate, ordered out of the
top k) - the two need different fixes.

The corpus is ingested by `seed_evaluation`, which titles each document
`[eval] <slug>`; this command resolves the template's `@slug` labels against
those titles and refuses to score a label whose evidence no current chunk
carries, since that would report a broken label as a ranking failure.
"""

import json
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from rest_framework.test import APIRequestFactory, force_authenticate

from documents.evaluation import metrics
from documents.evaluation import search as scoring
from documents.models import Document
from documents.search import service
from documents.search.views import SearchView

from .seed_evaluation import TITLE_PREFIX

ROOT = Path(settings.BASE_DIR).parent
TEMPLATE = ROOT / "evaluation" / "search-relevance.template.json"

# Large enough to hold every candidate both retrievers can produce, so the
# pool is the whole of what retrieval found rather than a prefix of it.
POOL_LIMIT = 1000


class Command(BaseCommand):
    help = "Score the Search endpoint on a passage-level relevance set."

    def add_arguments(self, parser):
        parser.add_argument("--template", default=str(TEMPLATE))
        parser.add_argument(
            "--as-user",
            required=True,
            help="The user the corpus was seeded under (seed_evaluation --user).",
        )
        parser.add_argument(
            "--corpus-owner",
            help=(
                "The user seed_evaluation ingested the corpus under, when "
                "scoring as someone else - for instance a user who can also "
                "read a noise corpus (ingest_corpus) on top of it. Defaults "
                "to --as-user."
            ),
        )
        parser.add_argument(
            "--split",
            choices=("dev", "test", "all"),
            default="all",
            help="Which cases to run. Choose thresholds on dev; report test.",
        )
        parser.add_argument("--k", type=int, default=5)
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Results requested per query, as the API's `limit`.",
        )
        parser.add_argument("--label", default="")
        parser.add_argument("--output", help="Write the full report as JSON.")
        parser.add_argument(
            "--show-cases",
            action="store_true",
            help="Print every case with its top k.",
        )
        parser.add_argument(
            "--baseline",
            help="A report written earlier with --output, to compare against.",
        )
        parser.add_argument("--fail-on-regression", action="store_true")
        parser.add_argument("--tolerance", type=float, default=0.0)

    def handle(self, *args, **options):
        template = Path(options["template"])
        if not template.exists():
            raise CommandError(f"{template} is missing")
        data = json.loads(template.read_text(encoding="utf-8"))

        users = get_user_model().objects
        user = users.filter(username=options["as_user"]).first()
        owner = users.filter(
            username=options.get("corpus_owner") or options["as_user"]
        ).first()
        if user is None or owner is None:
            raise CommandError(
                f"No user {options.get('corpus_owner') or options['as_user']!r}. "
                f"Seed the corpus first: manage.py seed_evaluation "
                f"--template {template} --user <name>"
            )

        documents = self._documents(owner, data)
        titles = {pk: slug for slug, pk in documents.items()}
        k = options["k"]

        cases = [
            case
            for case in data["cases"]
            if options["split"] == "all" or case.get("split") == options["split"]
        ]
        if not cases:
            raise CommandError(f"No cases in split {options['split']!r}.")

        view = SearchView.as_view(throttle_classes=())
        factory = APIRequestFactory()

        scores = []
        # What the endpoint actually did, per its own response - not what is
        # configured. A cross-encoder that fails to load (an empty model
        # cache offline, an OOM) degrades every search to retrieval order,
        # and a run that did not notice would score that order under the
        # configured model's name.
        degraded = []
        applied = set()
        for case in cases:
            evidence = self._evidence(user, case, documents)

            request = factory.post(
                "/api/v1/search/",
                {"query": case["query"], "limit": options["limit"]},
                format="json",
            )
            force_authenticate(request, user=user)
            started = time.monotonic()
            response = view(request)
            duration_ms = int((time.monotonic() - started) * 1000)
            if response.status_code != 200:
                raise CommandError(
                    f"{case['id']}: search returned {response.status_code} "
                    f"{getattr(response, 'data', '')}"
                )

            ranking_info = response.data.get("ranking") or {}
            if ranking_info.get("degraded"):
                degraded.append(case["id"])
            applied.add(ranking_info.get("reranker") or "retrieval order")

            results = response.data["results"]
            ranked = [hit["chunk_id"] for hit in results]
            # .get: an endpoint without the field marks nothing.
            below_floor = {hit["chunk_id"] for hit in results if hit.get("below_floor")}
            owners = {hit["chunk_id"]: hit["document_id"] for hit in results}

            pool_hits = service.search(user, case["query"], limit=POOL_LIMIT).hits
            pool = [hit.chunk.id for hit in pool_hits]
            for hit in pool_hits:
                owners.setdefault(hit.chunk.id, hit.chunk.document_version.document_id)
            for chunks, document_id in evidence:
                for chunk in chunks:
                    owners[chunk] = document_id

            scores.append(
                scoring.score_case(
                    case,
                    ranked,
                    owners,
                    [chunks for chunks, _ in evidence],
                    pool,
                    k=k,
                    duration_ms=duration_ms,
                    titles=titles,
                    below_floor=below_floor,
                )
            )

        if degraded:
            raise CommandError(
                f"The configured reranker was unavailable for {len(degraded)} of "
                f"{len(cases)} case(s) (first: {', '.join(degraded[:3])}), and "
                f"the endpoint fell back to retrieval order. These numbers would "
                f"describe that order under the reranker's name. Make the "
                f"reranker loadable (model cache, HF_HUB_OFFLINE, memory) and "
                f"run again, or configure the lexical reranker to measure "
                f"retrieval order on purpose."
            )

        report = self._report(data, options, scores, k, applied)
        self._print(report, scores, k, show_cases=options["show_cases"])

        # Compared BEFORE --output is written: with the two pointing at one
        # file - the natural way to refresh a baseline - writing first
        # compared the run with itself, which can never fail.
        failing = self._compare(report, options) if options.get("baseline") else []
        must_fail = bool(failing) and options["fail_on_regression"]

        if options.get("output"):
            overwrites_baseline = bool(options.get("baseline")) and (
                Path(options["output"]).resolve() == Path(options["baseline"]).resolve()
            )
            if must_fail and overwrites_baseline:
                self.stdout.write(
                    self.style.WARNING(
                        f"{options['output']} not written: it is the baseline "
                        f"this run failed against"
                    )
                )
            else:
                Path(options["output"]).write_text(
                    json.dumps(report, indent=2) + "\n", encoding="utf-8"
                )
                self.stdout.write(f"wrote {options['output']}")

        if must_fail:
            raise CommandError(
                f"{len(failing)} search metric(s) regressed against, or missing "
                f"from, {options['baseline']}: {', '.join(failing)}"
            )

    # --- resolution --------------------------------------------------------

    def _documents(self, owner, data) -> dict:
        slugs = [str(slug).lstrip("@") for slug in data.get("corpus", [])]
        found = dict(
            Document.objects.filter(
                owner=owner, title__in=[f"{TITLE_PREFIX}{slug}" for slug in slugs]
            ).values_list("title", "id")
        )
        resolved = {slug: found.get(f"{TITLE_PREFIX}{slug}") for slug in slugs}
        missing = sorted(slug for slug, pk in resolved.items() if pk is None)
        if missing:
            raise CommandError(
                f"{owner.username} owns no [eval] document for: "
                f"{', '.join(missing)}. Run seed_evaluation with this template."
            )
        return resolved

    def _evidence(self, user, case, documents) -> list:
        """(chunk ids carrying the evidence, document id) per label.

        Searched among the chunks the evaluator can retrieve - current
        version, current extraction pass - which is the same set search
        ranks over, so a label can only be satisfied by a passage the
        endpoint could actually have returned.
        """
        chunks, _ = service.authorized_chunks(user)
        resolved = []
        for label in case.get("relevant", []):
            slug = str(label["document"]).lstrip("@")
            if slug not in documents:
                raise CommandError(
                    f"{case['id']}: @{slug} is not in the template's corpus."
                )
            document_id = documents[slug]
            carrying = {
                chunk.id
                for chunk in chunks.filter(document_version__document_id=document_id)
                if scoring.contains_evidence(chunk.text, label["contains"])
            }
            if not carrying:
                raise CommandError(
                    f"{case['id']}: no current chunk of @{slug} contains "
                    f"{label['contains']!r}. The label is broken (or the "
                    f"evidence straddles a chunk boundary) - fix the template "
                    f"rather than scoring it as a miss."
                )
            resolved.append((carrying, document_id))
        return resolved

    # --- reporting ---------------------------------------------------------

    def _report(self, data, options, scores, k, applied) -> dict:
        from documents.processing.chunking import service as chunking_service
        from documents.rag import engine

        try:
            provider = chunking_service.get_provider()
            embedding = f"{provider.name} {provider.version}"
        except Exception as exc:  # reported, not fatal: lexical-only is a config
            embedding = f"unavailable ({type(exc).__name__})"
        from documents.search import ranking

        reranker = engine.get_reranker()

        splits = {"all": scores}
        for name in ("dev", "test"):
            subset = [s for s in scores if s.split == name]
            if subset:
                splits[name] = subset

        return {
            "dataset": data.get("name"),
            "dataset_version": data.get("version"),
            "label": options["label"],
            "k": k,
            "limit": options["limit"],
            # Which cases were run. Part of the exam: a baseline over every
            # case and a run over the test split share metric names but not
            # cases, and comparing them reports the difference as regressions.
            "split": options["split"],
            "configuration": {
                "embedding": embedding,
                "reranker": f"{reranker.name} {getattr(reranker, 'version', '')}",
                # What ordered the results, from the endpoint's responses:
                # a lexical reranker is configured in CI but the endpoint
                # keeps retrieval order with it, so the two can differ.
                "endpoint_ranking": sorted(applied),
                "search_candidates": getattr(settings, "SEARCH_CANDIDATES", None),
                "search_rerank_depth": ranking.rerank_depth(),
                "search_relevance_floor": ranking.relevance_floor(reranker),
            },
            "metrics": {
                name: scoring.aggregate(subset, k) for name, subset in splits.items()
            },
            "by_kind": {
                kind: scoring.aggregate([s for s in scores if s.kind == kind], k)
                for kind in sorted({s.kind for s in scores})
            },
            "cases": [s.to_dict() for s in scores],
        }

    def _print(self, report, scores, k, *, show_cases):
        config = report["configuration"]
        self.stdout.write(
            f"{report['dataset']} {report['dataset_version']}  "
            f"{report['label'] or '(no label)'}  k={k} limit={report['limit']} "
            f"split={report['split']}\n"
            f"embedding: {config['embedding']}  reranker: {config['reranker']}  "
            f"results ordered by: {', '.join(config['endpoint_ranking'])}"
        )
        for name, block in report["metrics"].items():
            self.stdout.write(f"\n[{name}]")
            for key, value in block["quality"].items():
                self.stdout.write(f"  {key:32} {value:.4f}")
            for key, value in block["diagnostics"].items():
                self.stdout.write(f"  {key:32} {value}")

        self.stdout.write("\n[by kind]  R@k    P@k    MRR    nDCG   no-match  returned")
        for kind, block in report["by_kind"].items():
            quality = block["quality"]
            self.stdout.write(
                f"  {kind:22} "
                f"{quality.get(f'recall_at_{k}', float('nan')):.3f}  "
                f"{quality.get(f'precision_at_{k}', float('nan')):.3f}  "
                f"{quality.get('mrr', float('nan')):.3f}  "
                f"{quality.get(f'ndcg_at_{k}', float('nan')):.3f}  "
                f"{quality.get('no_relevant_said_no_match', float('nan')):.3f}     "
                f"{block['diagnostics']['mean_returned_answerable'] or block['diagnostics']['mean_returned_no_relevant']:.1f}"
            )

        if show_cases:
            # * relevant, - not relevant; ~ marks a passage the endpoint
            # returned as below its relevance floor.
            self.stdout.write("")
            for s in scores:
                marks = " ".join(
                    ("~" if item.get("below_floor") else "")
                    + ("*" if item["relevant"] else "-")
                    + str(item["document"])
                    for item in s.top
                )
                self.stdout.write(
                    f"  {s.case_id:22} {s.outcome:24} rr={s.reciprocal_rank:.2f} "
                    f"n={s.returned:<3} {marks}"
                )

    # What makes two runs the same exam. A baseline written before `split`
    # was recorded covered every case.
    EXAM = (
        ("dataset", None),
        ("dataset_version", None),
        ("k", None),
        ("limit", None),
        ("split", "all"),
    )

    def _compare(self, report, options) -> list:
        """Print the comparison; return the keys that fail the gate.

        Refuses outright when the baseline is a different exam: `--k 10`
        against a k=5 baseline renamed the k-dependent metrics, so they were
        silently left out and only the rest were gated; `--split test`
        against an all-cases baseline reported four false regressions.
        """
        baseline = json.loads(Path(options["baseline"]).read_text(encoding="utf-8"))

        mismatched = [
            f"{field} {baseline.get(field, default)!r} there, {report[field]!r} here"
            for field, default in self.EXAM
            if baseline.get(field, default) != report[field]
        ]
        if mismatched:
            raise CommandError(
                f"{options['baseline']} measured a different exam: "
                f"{'; '.join(mismatched)}. Comparing them would report the "
                f"difference as regressions or improvements. Run with matching "
                f"options, or write a new baseline."
            )

        before = {name: block["quality"] for name, block in baseline["metrics"].items()}
        after = {name: block["quality"] for name, block in report["metrics"].items()}
        result = metrics.compare(before, after, tolerance=options["tolerance"])

        for key in result["regressions"]:
            self.stdout.write(
                self.style.ERROR(f"  regressed  {key} {result['deltas'][key]:+.4f}")
            )
        for key in result["improvements"]:
            self.stdout.write(
                self.style.SUCCESS(f"  improved   {key} {result['deltas'][key]:+.4f}")
            )
        for key in result["added"]:
            self.stdout.write(f"  new        {key} (not in the baseline, so not gated)")
        # A gated metric this run no longer produces is a failure, not a
        # footnote: otherwise renaming or dropping a metric removes it from
        # the gate without anyone deciding to.
        for key in result["removed"]:
            self.stdout.write(
                self.style.ERROR(
                    f"  missing    {key} (in the baseline, not in this run)"
                )
            )

        failing = result["regressions"] + result["removed"]
        if not failing:
            self.stdout.write(self.style.SUCCESS("no search regression"))
        return failing
