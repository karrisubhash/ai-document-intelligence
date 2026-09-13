from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents import evaluation
from documents.models import EvaluationDataset, EvaluationRun


class Command(BaseCommand):
    help = (
        "Load an evaluation dataset, run it against the live pipeline, and "
        "optionally compare the result against an earlier run (spec 19.2)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--load", help="Path to a dataset JSON file to import.")
        parser.add_argument("--dataset", help="Dataset name to run.")
        # NOT --version: Django reserves that for its own version flag.
        parser.add_argument(
            "--dataset-version", help="Dataset version to run."
        )
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
            raise CommandError(f"No such user: {options['as_user']}")

        query = EvaluationDataset.objects.filter(name=options["dataset"])
        if options["dataset_version"]:
            query = query.filter(version=options["dataset_version"])
        dataset = query.order_by("-created_at").first()
        if dataset is None:
            raise CommandError(f"No dataset named {options['dataset']!r}")

        self.stdout.write(f"Running {dataset} as {user.username}...")
        run = evaluation.run_dataset(dataset, user, label=options["label"])

        self._print_run(run)

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
        for k, value in sorted(retrieval.get("recall_at_k", {}).items(), key=lambda i: int(i[0])):
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
