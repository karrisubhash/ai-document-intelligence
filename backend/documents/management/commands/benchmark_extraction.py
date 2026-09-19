from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from documents.benchmarks import extraction


class Command(BaseCommand):
    help = (
        "Benchmark document extraction approaches over an evaluation set. "
        "See documents/benchmarks/extraction.py for the expected layout."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "dataset",
            help="Directory containing the evaluation documents.",
        )
        parser.add_argument(
            "--approach",
            action="append",
            choices=sorted(extraction.APPROACHES),
            help="Approach to run (repeatable). Defaults to all.",
        )
        parser.add_argument(
            "--json",
            dest="json_path",
            help="Write the full report to this path.",
        )

    def handle(self, *args, **options):
        dataset_dir = Path(options["dataset"])
        if not dataset_dir.is_dir():
            raise CommandError(f"No such directory: {dataset_dir}")

        approaches = options["approach"] or sorted(extraction.APPROACHES)

        documents = extraction.discover_documents(dataset_dir)
        if not documents:
            raise CommandError(
                f"No documents (.pdf/.png/.jpg/.jpeg) found under {dataset_dir}"
            )

        self.stdout.write(
            f"Benchmarking {len(documents)} document(s) "
            f"across {len(approaches)} approach(es): {', '.join(approaches)}\n"
        )

        results = extraction.run_benchmark(dataset_dir, approaches)
        report = extraction.to_report(results)

        self._print_results(results)
        self._print_summary(report["summary"])

        if options["json_path"]:
            path = Path(options["json_path"])
            extraction.write_report(report, path)
            self.stdout.write(self.style.SUCCESS(f"\nReport written to {path}"))

        if any(
            value["mean_similarity"] is None for value in report["summary"].values()
        ):
            self.stdout.write(
                self.style.WARNING(
                    "\nNo ground truth found for some documents - quality columns are "
                    "blank there. Add a matching .txt beside a document to score it. "
                    "Do not quote accuracy without a scored evaluation set."
                )
            )

    def _print_results(self, results):
        header = (
            f"{'document':<40} {'approach':<10} {'type':<6} {'pages':>5} "
            f"{'chars':>8} {'secs':>7} {'sim':>7} {'words':>7}  status"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for result in results:
            name = Path(result.path).name
            if len(name) > 39:
                name = name[:36] + "..."

            status = "ok" if result.succeeded else f"FAILED {result.error_code}"
            similarity = (
                "-" if result.similarity is None else f"{result.similarity:.3f}"
            )
            words = (
                "-" if result.word_accuracy is None else f"{result.word_accuracy:.3f}"
            )

            self.stdout.write(
                f"{name:<40} {result.approach:<10} {str(result.file_type or '?'):<6} "
                f"{result.page_count:>5} {result.character_count:>8} "
                f"{result.duration_seconds:>7.2f} {similarity:>7} {words:>7}  {status}"
            )

    def _print_summary(self, summary):
        self.stdout.write("\nSummary")
        header = (
            f"{'approach':<10} {'docs':>5} {'ok':>4} {'fail%':>7} {'pages':>6} "
            f"{'total_s':>8} {'mean_s':>7} {'mem_mb':>7} {'sim':>7} {'words':>7}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for approach, values in summary.items():
            similarity = (
                "-"
                if values["mean_similarity"] is None
                else f"{values['mean_similarity']:.3f}"
            )
            words = (
                "-"
                if values["mean_word_accuracy"] is None
                else f"{values['mean_word_accuracy']:.3f}"
            )
            self.stdout.write(
                f"{approach:<10} {values['documents']:>5} {values['succeeded']:>4} "
                f"{values['failure_rate'] * 100:>6.1f}% {values['total_pages']:>6} "
                f"{values['total_seconds']:>8.2f} {values['mean_seconds']:>7.2f} "
                f"{values['peak_child_memory_mb']:>7.1f} {similarity:>7} {words:>7}"
            )
