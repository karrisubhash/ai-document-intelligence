"""Find processing jobs that stopped making progress, and say so.

A job is left PROCESSING forever in exactly one situation the pipeline
cannot report on itself: the process running it died. Celery's hard time
limit kills the child outright, and so does the kernel's OOM killer - in
both cases no code runs to mark the job finished, and the document sits
at "Processing" in every listing indefinitely.

Run from cron (or any scheduler) rather than from a Celery beat process:
this platform has no beat service, and one small periodic command is not
a reason to add one.

    python manage.py recover_stalled_jobs            # report only
    python manage.py recover_stalled_jobs --apply    # mark them STALLED
    python manage.py recover_stalled_jobs --apply --requeue
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from documents import services
from documents.models import ProcessingJob


class Command(BaseCommand):
    help = "Mark processing jobs that stopped making progress as STALLED."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Mark the jobs. Without this the command only reports.",
        )
        parser.add_argument(
            "--requeue",
            action="store_true",
            help="Queue a fresh run for each recovered job on a current version.",
        )

    def handle(self, *args, **options):
        cutoff = services.stalled_cutoff()
        candidates = ProcessingJob.objects.filter(
            Q(status=ProcessingJob.Status.PROCESSING) & Q(updated_at__lt=cutoff)
        ).select_related("document_version__document")

        if not options["apply"]:
            count = candidates.count()
            for job in candidates[:50]:
                self.stdout.write(
                    f"  job {job.id}  document {job.document_version.document_id}"
                    f"  stage {job.current_stage or '-'}"
                    f"  last update {job.updated_at.isoformat()}"
                )
            self.stdout.write(
                f"{count} stalled job(s) older than {cutoff.isoformat()}. "
                f"Re-run with --apply to mark them."
            )
            return

        recovered = services.recover_stalled_jobs(requeue=options["requeue"])
        requeued = sum(1 for _, replacement in recovered if replacement is not None)
        for job, replacement in recovered:
            suffix = f" -> requeued as job {replacement.id}" if replacement else ""
            self.stdout.write(f"  job {job.id} marked STALLED{suffix}")
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(recovered)} job(s) marked STALLED, {requeued} requeued."
            )
        )
