"""Queue a controlled re-index of the corpus (spec §11.4)."""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from documents.models import Document, DocumentContent
from documents.observability import queues
from documents.processing.chunking import service as chunking_service
from documents.processing.chunking.exceptions import EmbeddingProviderNotConfigured
from documents.tasks_reindex import reindex_content_task


class Command(BaseCommand):
    help = (
        "Re-chunk and re-embed existing documents on the index queue, so a "
        "bulk re-index does not sit in front of live uploads."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--department",
            default="",
            help="Limit to one department's documents.",
        )
        parser.add_argument(
            "--stale-only",
            action="store_true",
            help=(
                "Only content whose embeddings were produced by a different "
                "model than the one currently configured."
            ),
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Queue at most this many."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be queued and exit.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run inline instead of queueing. For small corpora and tests.",
        )

    def handle(self, *args, **options):
        try:
            provider = chunking_service.get_provider()
        except EmbeddingProviderNotConfigured:
            raise CommandError(
                "No DOCUMENT_EMBEDDING_PROVIDER configured. Re-indexing "
                "without one would delete existing vectors and write none "
                "back, which is a worse state than the one you started in."
            )

        contents = DocumentContent.objects.select_related(
            "document_version__document"
        ).order_by("id")

        if options["department"]:
            contents = contents.filter(
                document_version__document__department=options["department"]
            )

        if options["stale_only"]:
            # Content with no embedding from THIS model: either never
            # embedded, or embedded by a model that has since been replaced.
            contents = contents.filter(
                ~Q(
                    chunks__embeddings__model_name=provider.name,
                    chunks__embeddings__model_version=provider.version,
                )
            ).distinct()

        total = contents.count()
        if options["limit"]:
            contents = contents[: options["limit"]]

        queued = list(contents.values_list("id", flat=True))

        self.stdout.write(
            f"Provider : {provider.name} {provider.version} "
            f"(semantic={provider.semantic})"
        )
        self.stdout.write(f"Matching : {total} content row(s)")
        self.stdout.write(f"Selected : {len(queued)}")

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING("Dry run - nothing queued.")
            )
            return

        if options["sync"]:
            for content_id in queued:
                reindex_content_task(content_id)
            self.stdout.write(
                self.style.SUCCESS(f"Re-indexed {len(queued)} inline")
            )
        else:
            for content_id in queued:
                reindex_content_task.delay(content_id)
            self.stdout.write(
                self.style.SUCCESS(f"Queued {len(queued)} on the index queue")
            )

        depths = queues.collect()
        if depths:
            self.stdout.write("Queue depth: " + ", ".join(
                f"{name}={depth}" for name, depth in sorted(depths.items())
            ))
