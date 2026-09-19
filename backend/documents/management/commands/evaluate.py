import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents import evaluation
from documents.models import EvaluationDataset, EvaluationRun


def _without_system(metrics: dict) -> dict:
    """Everything except the environment-dependent timings."""
    return {key: value for key, value in metrics.items() if key != "system"}


class Command(BaseCommand):
    help = (
        "Load an evaluation dataset, run it against the live pipeline, and "
        "optionally compare the result against an earlier run (spec 19.2)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--load", help="Path to a dataset JSON file to import.")
        parser.add_argument("--dataset", help="Dataset name to run.")
        # NOT --version: Django reserves that for its own version flag.
        parser.add_argument("--dataset-version", help="Dataset version to run.")
        parser.add_argument(
            "--as-user",
            help=(
                "Username to evaluate as. Required: a benchmark is scored "
                "through the permission filter, so the answer depends on who "
                "is asking."
            ),
        )
        parser.add_argument("--label", default="", help="Label for this run.")
        parser.add_argument(
            "--compare-to",
            type=int,
            help="Run id to compare against, for a regression check.",
        )
        parser.add_argument(
            "--fail-on-regression",
            action="store_true",
            help="Exit non-zero if any metric dropped. For CI.",
        )
        parser.add_argument(
            "--baseline",
            help=(
                "Path to a committed baseline metrics file to compare "
                "against. Use this in CI: --compare-to needs a run id, and a "
                "fresh database has no previous run, so a pipeline using it "
                "runs the benchmark and can never fail."
            ),
        )
        parser.add_argument(
            "--write-baseline",
            help="Write this run's metrics to a file, to be committed.",
        )
        parser.add_argument(
            "--gate-latency",
            action="store_true",
            help=(
                "Also treat a latency increase as a regression. Off by "
                "default: wall-clock timings differ on every run and "
                "between machines, so gating on them makes the check fail "
                "permanently while saying nothing about quality."
            ),
        )
        parser.add_argument(
            "--tolerance",
            type=float,
            default=0.0,
            help=(
                "Metric movement smaller than this is not a regression. "
                "Defaults to 0: with a fixed dataset and a deterministic "
                "pipeline, any movement is a real change."
            ),
        )

    def handle(self, *args, **options):
        if options["load"]:
            dataset = evaluation.load_file(options["load"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"Loaded {dataset} with {dataset.cases.count()} case(s)"
                )
            )
            if not options["dataset"]:
                return

        if not options["dataset"]:
            raise CommandError("--dataset is required (or use --load alone)")

        if not options["as_user"]:
            raise CommandError(
                "--as-user is required. Evaluation runs through the same "
                "permission filter as a real question, so a score only means "
                "something alongside who it was scored as."
            )

        User = get_user_model()
        try:
            user = User.objects.get(username=options["as_user"])
        except User.DoesNotExist:
            raise CommandError(f"No such user: {options['as_user']}") from None

        query = EvaluationDataset.objects.filter(name=options["dataset"])
        if options["dataset_version"]:
            query = query.filter(version=options["dataset_version"])
        dataset = query.order_by("-created_at").first()
        if dataset is None:
            raise CommandError(f"No dataset named {options['dataset']!r}")

        self.stdout.write(f"Running {dataset} as {user.username}...")
        run = evaluation.run_dataset(dataset, user, label=options["label"])

        self._print_run(run)

        if options["write_baseline"]:
            self._write_baseline(run, Path(options["write_baseline"]))

        if options["baseline"]:
            self._compare_to_file(run, Path(options["baseline"]), options)

        if options["compare_to"]:
            baseline = EvaluationRun.objects.filter(id=options["compare_to"]).first()
            if baseline is None:
                raise CommandError(f"No run with id {options['compare_to']}")
            report = evaluation.compare_runs(baseline, run)
            self._print_comparison(report)

            if options["fail_on_regression"] and report["regressions"]:
                raise CommandError(
                    f"{len(report['regressions'])} metric(s) regressed against "
                    f"run {baseline.id}"
                )

    def _write_baseline(self, run, path):
        """Record this run's metrics as the number to beat.

        Configuration is recorded alongside them, because a score is only
        comparable against another score produced the same way - a baseline
        set with a semantic embedder is not a baseline for a lexical one.
        """
        payload = {
            "dataset": run.dataset.name,
            "dataset_version": run.dataset.version,
            "configuration": run.configuration,
            "metrics": run.metrics,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"\nWrote baseline to {path}"))

    def _compare_to_file(self, run, path, options):
        """Compare against a committed baseline rather than a previous run."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CommandError(
                f"No baseline at {path}. Create one with --write-baseline "
                f"once the current scores have been reviewed."
            ) from exc
        except json.JSONDecodeError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc

        if (
            payload.get("dataset") != run.dataset.name
            or payload.get("dataset_version") != run.dataset.version
        ):
            raise CommandError(
                f"Baseline is for {payload.get('dataset')} "
                f"{payload.get('dataset_version')}, this run is "
                f"{run.dataset.name} {run.dataset.version}. Comparing scores "
                f"from different benchmarks is comparing different exams."
            )

        # Latency is dropped from the comparison unless asked for.
        #
        # Found by proving this gate could fail: inflating one quality
        # metric in the baseline correctly failed the run - and so did
        # FOUR latency metrics, on an otherwise identical system, because
        # wall-clock timing differs on every run. A gate that fails every
        # time fails to be a gate; it gets disabled, and the quality
        # regression it was built for goes through with it.
        #
        # Latency still belongs on the dashboard (§20) and in the run
        # record. It just cannot be a pass/fail signal when the baseline
        # was measured on different hardware from the candidate.
        baseline_metrics = payload.get("metrics", {})
        candidate_metrics = run.metrics

        if not options["gate_latency"]:
            baseline_metrics = _without_system(baseline_metrics)
            candidate_metrics = _without_system(candidate_metrics)

        report = evaluation.compare_metrics(
            baseline_metrics, candidate_metrics, tolerance=options["tolerance"]
        )
        self.stdout.write(f"\nAgainst baseline {path}:")
        self._print_comparison(report)

        before = payload.get("configuration", {})
        if before and before != run.configuration:
            # Not fatal - an intentional model change is exactly when you
            # WANT to see the deltas - but it must not pass unremarked.
            self.stdout.write(
                self.style.WARNING(
                    "  note: the configuration differs from the baseline's, "
                    "so these deltas include that change"
                )
            )

        if options["fail_on_regression"] and report["regressions"]:
            raise CommandError(
                f"{len(report['regressions'])} metric(s) regressed against "
                f"the baseline: {', '.join(report['regressions'])}"
            )

    def _print_run(self, run):
        self.stdout.write("")
        self.stdout.write(f"Run #{run.id}  {run.status}  {run.cases_scored} case(s)")

        config = run.configuration
        self.stdout.write(
            f"  embedding : {config['embedding'].get('provider') or '(none)'} "
            f"(semantic={config['embedding'].get('semantic')})"
        )
        self.stdout.write(
            f"  answerer  : {config['answerer']['name']} "
            f"(generative={config['answerer']['generative']})"
        )

        retrieval = run.metrics.get("retrieval", {})
        generation = run.metrics.get("generation", {})

        self.stdout.write("\n  Retrieval")
        for k, value in sorted(
            retrieval.get("recall_at_k", {}).items(), key=lambda i: int(i[0])
        ):
            self.stdout.write(f"    recall@{k:<3} {value}")
        self.stdout.write(f"    MRR       {retrieval.get('mrr')}")
        self.stdout.write(f"    MAP       {retrieval.get('map')}")

        self.stdout.write("\n  Generation")
        for key in (
            "groundedness",
            "citation_accuracy",
            "source_coverage",
            "abstention_accuracy",
            "coverage",
        ):
            self.stdout.write(f"    {key:<20} {generation.get(key)}")
        self.stdout.write(
            f"    false abstentions    {generation.get('false_abstentions')}"
        )
        self.stdout.write(
            f"    missed abstentions   {generation.get('missed_abstentions')}"
        )

    def _print_comparison(self, report):
        self.stdout.write("\n  Regression check")
        if report.get("warning"):
            self.stdout.write(self.style.WARNING(f"    {report['warning']}"))

        for key in sorted(report["regressions"]):
            self.stdout.write(
                self.style.ERROR(f"    DOWN {key}: {report['deltas'][key]:+.4f}")
            )
        for key in sorted(report["improvements"]):
            self.stdout.write(
                self.style.SUCCESS(f"    UP   {key}: {report['deltas'][key]:+.4f}")
            )
        if not report["regressions"] and not report["improvements"]:
            self.stdout.write("    no change")
