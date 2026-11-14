"""Re-extract documents that were read by an older pipeline.

Extraction improves - a better PDF engine, OCR that keeps table cells, a DOCX
reader that no longer skips text boxes - but a document already processed
keeps the text it was given. This queues them again, through the same
`reprocess_document` the Reprocess button uses, so every guard it has applies:
a run already in progress is not doubled, an archived document is left alone,
a quarantined file is not run again.

The previous extraction is kept until the new one is complete and indexed (the
live pass only moves when the new one has its chunks), so searches and answers
keep working throughout, and a run that fails leaves the old text in place.

By default only documents whose current extraction carries an older
`processing_version` than the pipeline now writes are selected - the
version exists to be bumped when extraction output changes. `--all` selects
everything. Reprocessing is real work (OCR especially), so run it in batches
with `--limit` and watch the queue depth that is printed at the end.
"""

from django.core.management.base import BaseCommand, CommandError

from documents.models import Document
from documents.observability import queues
from documents.processing.context import FileType
from documents.processing.pipeline import PROCESSING_VERSION
from documents.services import (
    DocumentArchived,
    DocumentQuarantined,
    ProcessingInProgress,
    reprocess_document,
)

FILE_TYPES = sorted(
    value
    for name, value in vars(FileType).items()
    if not name.startswith("_") and isinstance(value, str)
)


class Command(BaseCommand):
    help = (
        "Queue documents extracted by an older pipeline to be extracted again, "
        "so improvements in text, table and OCR extraction reach what is "
        "already uploaded."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Every live document, not only those from an older pipeline.",
        )
        parser.add_argument(
            "--file-type",
            choices=FILE_TYPES,
            help="Only documents of this detected file type.",
        )
        parser.add_argument(
            "--department",
            default="",
            help="Limit to one department's documents.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Queue at most this many. Use it to go in batches.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be queued and exit.",
        )

    def select(self, options):
        documents = Document.objects.filter(
            archived_at__isnull=True,
            current_version__current_content__isnull=False,
        ).select_related("current_version__current_content")

        if not options["all"]:
            documents = documents.exclude(
                current_version__current_content__processing_version=PROCESSING_VERSION
            )
        if options["file_type"]:
            documents = documents.filter(
                current_version__current_content__metadata__file_type=options[
                    "file_type"
                ]
            )
        if options["department"]:
            documents = documents.filter(department=options["department"])

        return documents.order_by("id")

    def handle(self, *args, **options):
        if options["limit"] < 0:
            raise CommandError("--limit cannot be negative.")

        documents = self.select(options)
        total = documents.count()
        if options["limit"]:
            documents = documents[: options["limit"]]
        chosen = list(documents)

        self.stdout.write(f"Pipeline : {PROCESSING_VERSION}")
        self.stdout.write(f"Matching : {total} document(s)")
        self.stdout.write(f"Selected : {len(chosen)}")

        if options["dry_run"]:
            by_type: dict = {}
            for document in chosen:
                kind = document.current_version.current_content.metadata.get(
                    "file_type", "UNKNOWN"
                )
                by_type[kind] = by_type.get(kind, 0) + 1
            for kind, count in sorted(by_type.items()):
                self.stdout.write(f"  {kind}: {count}")
            self.stdout.write(self.style.WARNING("Dry run - nothing queued."))
            return

        queued = 0
        skipped: dict = {}
        for document in chosen:
            try:
                reprocess_document(document)
            except ProcessingInProgress:
                reason = "already being processed"
            except DocumentArchived:
                reason = "archived"
            except DocumentQuarantined:
                reason = "quarantined by the security scan"
            except ValueError as exc:
                reason = str(exc)
            else:
                queued += 1
                continue
            skipped[reason] = skipped.get(reason, 0) + 1

        self.stdout.write(self.style.SUCCESS(f"Queued {queued} document(s)"))
        for reason, count in sorted(skipped.items()):
            self.stdout.write(self.style.WARNING(f"Skipped {count}: {reason}"))

        depths = queues.collect()
        if depths:
            self.stdout.write(
                "Queue depth: "
                + ", ".join(f"{name}={depth}" for name, depth in sorted(depths.items()))
            )
