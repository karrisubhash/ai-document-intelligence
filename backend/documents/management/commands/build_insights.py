"""Queue post-publish enrichment for existing documents.

Documents processed before the brief existed have no brief, no centroid and
no thumbnail; this queues the same per-version task the pipeline queues
after publishing. One task per version on the index queue - never one task
for the corpus - so a backfill of thousands of documents is as interruptible,
parallel and visible in queue depth as a re-index (see tasks_reindex).
"""

from django.core.management.base import BaseCommand, CommandError

from documents.models import DocumentInsight
from documents.processing.insights import GENERATOR_VERSION
from documents.processing.insights.enrichment import Reason
from documents.processing.insights.service import live_versions
from documents.tasks_insights import enrich_document_task


class Command(BaseCommand):
    help = (
        "Queue the document brief, related-documents centroid and thumbnail "
        "for existing documents, one task per version on the index queue."
    )

    def add_arguments(self, parser):
        which = parser.add_mutually_exclusive_group(required=True)
        which.add_argument(
            "--missing",
            action="store_true",
            help="Live versions whose extraction pass has no brief yet.",
        )
        which.add_argument(
            "--stale",
            action="store_true",
            help="Live versions with no brief, a failed one, or one written by "
            "an older generator version.",
        )
        which.add_argument("--all", action="store_true", help="Every live version.")
        which.add_argument(
            "--document", type=int, metavar="ID", help="One document's live version."
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Queue at most this many."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would be queued."
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run inline instead of queueing. For small corpora and tests.",
        )

    def handle(self, *args, **options):
        versions = live_versions().filter(document__archived_at__isnull=True)
        if options["document"] is not None:
            versions = live_versions().filter(document_id=options["document"])
            if not versions.exists():
                raise CommandError(
                    f"Document {options['document']} has no processed current version."
                )
        elif options["missing"]:
            versions = versions.exclude(current_content__insight__isnull=False)
        elif options["stale"]:
            fresh = DocumentInsight.objects.filter(
                status=DocumentInsight.Status.READY, generator_version=GENERATOR_VERSION
            ).values("document_content_id")
            versions = versions.exclude(current_content_id__in=fresh)

        ids = list(versions.order_by("pk").values_list("pk", flat=True))
        if options["limit"]:
            ids = ids[: options["limit"]]

        self.stdout.write(f"Selected : {len(ids)} version(s)")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run - nothing queued."))
            return

        for version_id in ids:
            if options["sync"]:
                enrich_document_task(version_id, Reason.BACKFILL)
            else:
                enrich_document_task.delay(version_id, Reason.BACKFILL)

        verb = "Enriched" if options["sync"] else "Queued"
        where = "inline" if options["sync"] else "on the index queue"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(ids)} {where}"))
