"""Score the Search endpoint against a passage-level relevance set.

    python manage.py seed_evaluation --template ../evaluation/search-relevance.template.json \
        --user search-evaluator --output /tmp/search-relevance.json
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

        user = get_user_model().objects.filter(username=options["as_user"]).first()
        if user is None:
            raise CommandError(
                f"No user {options['as_user']!r}. Seed the corpus first: "
                f"manage.py seed_evaluation --template {template} "
                f"--user {options['as_user']}"
            )

        documents = self._documents(user, data)
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

            results = response.data["results"]
            ranked = [hit["chunk_id"] for hit in results]
            owners = {hit["chunk_id"]: hit["document_id"] for hit in results}

            pool_hits = service.search(user, case["query"], limit=POOL_LIMIT).hits
            pool = {hit.chunk.id for hit in pool_hits}
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
                )
            )

        report = self._report(data, options, scores, k)
        self._print(report, scores, k, show_cases=options["show_cases"])

        if options.get("output"):
            Path(options["output"]).write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            self.stdout.write(f"wrote {options['output']}")

        if options.get("baseline"):
            self._compare(report, options)

    # --- resolution --------------------------------------------------------

    def _documents(self, user, data) -> dict:
        slugs = [str(slug).lstrip("@") for slug in data.get("corpus", [])]
        found = dict(
            Document.objects.filter(
                owner=user, title__in=[f"{TITLE_PREFIX}{slug}" for slug in slugs]
            ).values_list("title", "id")
        )
        resolved = {slug: found.get(f"{TITLE_PREFIX}{slug}") for slug in slugs}
        missing = sorted(slug for slug, pk in resolved.items() if pk is None)
        if missing:
            raise CommandError(
                f"{user.username} owns no [eval] document for: "
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

    def _report(self, data, options, scores, k) -> dict:
        from documents.processing.chunking import service as chunking_service
        from documents.rag import engine

        try:
            provider = chunking_service.get_provider()
            embedding = f"{provider.name} {provider.version}"
        except Exception as exc:  # reported, not fatal: lexical-only is a config
            embedding = f"unavailable ({type(exc).__name__})"
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
            "configuration": {
                "embedding": embedding,
                "reranker": f"{reranker.name} {getattr(reranker, 'version', '')}",
                "search_candidates": getattr(settings, "SEARCH_CANDIDATES", None),
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
            f"{report['label'] or '(no label)'}  k={k} limit={report['limit']}\n"
            f"embedding: {config['embedding']}  reranker: {config['reranker']}"
        )
        for name, block in report["metrics"].items():
            self.stdout.write(f"\n[{name}]")
            for key, value in block["quality"].items():
                self.stdout.write(f"  {key:32} {value:.4f}")
            for key, value in block["diagnostics"].items():
                self.stdout.write(f"  {key:32} {value}")

        self.stdout.write("\n[by kind]  R@k    P@k    MRR    nDCG   empty-ok  returned")
        for kind, block in report["by_kind"].items():
            quality = block["quality"]
            self.stdout.write(
                f"  {kind:22} "
                f"{quality.get(f'recall_at_{k}', float('nan')):.3f}  "
                f"{quality.get(f'precision_at_{k}', float('nan')):.3f}  "
                f"{quality.get('mrr', float('nan')):.3f}  "
                f"{quality.get(f'ndcg_at_{k}', float('nan')):.3f}  "
                f"{quality.get('no_relevant_returned_nothing', float('nan')):.3f}     "
                f"{block['diagnostics']['mean_returned_answerable'] or block['diagnostics']['mean_returned_no_relevant']:.1f}"
            )

        if show_cases:
            self.stdout.write("")
            for s in scores:
                marks = " ".join(
                    ("*" if item["relevant"] else "-") + str(item["document"])
                    for item in s.top
                )
                self.stdout.write(
                    f"  {s.case_id:22} {s.outcome:24} rr={s.reciprocal_rank:.2f} "
                    f"n={s.returned:<3} {marks}"
                )

    def _compare(self, report, options):
        baseline = json.loads(Path(options["baseline"]).read_text(encoding="utf-8"))
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

        if result["regressions"] and options["fail_on_regression"]:
            raise CommandError(
                f"{len(result['regressions'])} search metric(s) regressed "
                f"against {options['baseline']}"
            )
        if not result["regressions"]:
            self.stdout.write(self.style.SUCCESS("no search regression"))
