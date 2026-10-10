"""Ingest a document corpus at scale, through the real pipeline.

Every quality number this platform reports was measured over five documents
totalling 2,569 bytes. Recall@1 = 1.0 on that corpus is close to
meaningless: each question maps to exactly one plausible document, so a
retriever that returned a random one of the five would score 0.2 and one
that read the question at all scores 1.0. Nothing about it predicts what
happens when there are twelve thousand documents and forty of them mention
"budget".

This command builds the corpus that makes the number mean something. It
does NOT take a shortcut into the database: every document is created
through `services.create_document_version`, the same call the upload
endpoint makes, so each one gets a real version, a real checksum, a real
ProcessingJob, and travels the whole pipeline - ingestion, validation, text
extraction, layout analysis, classification, extraction, chunking,
embedding. A corpus staged by bulk INSERT would prove that the database
can hold rows. This proves the system can ingest.

THE SOURCE. `--source qs-ocr` reads the QS-OCR corpus already vendored in
this repository for classifier training: 12,000 OCR'd pages of real scanned
business documents, evenly split across 16 classes - letters, memos,
emails, invoices, budgets, forms, questionnaires, specifications, resumes,
scientific reports, presentations, news articles and more. They are real
documents with real OCR damage, which is the point: a corpus of clean
synthetic prose would flatter the retriever in exactly the way that hides
the failures worth finding. Any directory of `.txt` files works too.

DEPARTMENTS ARE ASSIGNED, NOT INVENTED. Documents are spread across
departments by class, so the corpus looks like an organization rather than
a pile: invoices and budgets to Finance, resumes to HR, specifications and
scientific reports to R&D. This is not decoration. The permission filter
runs before similarity on every search, and a single-department corpus
never exercises it - the filter that matters most at scale would be the one
piece never tested at scale.

THE GOLD MANIFEST. Each document's true class is written to a sidecar JSON
file keyed by document id, rather than squeezed into a model field it does
not fit. That file is ground truth for classification accuracy and for
building retrieval cases whose answers are known.

    python manage.py ingest_corpus --source qs-ocr --limit 1000 --owner admin
    python manage.py ingest_corpus --source qs-ocr --limit 12000 --owner admin
    python manage.py ingest_corpus --source ./some/dir --owner admin --dry-run

Re-running is safe: a document whose external id was already ingested is
skipped, so an interrupted run resumes rather than duplicating.
"""

import csv
import json
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError

from documents import services
from documents.models import Document

# Where the vendored corpus and its manifests live, relative to the
# repository root (one level above BASE_DIR, which is backend/).
QS_OCR_MANIFESTS = Path("ai/classification/data/manifests/qs_ocr")
QS_OCR_SPLITS = ("train", "validation", "test")

# QS-OCR's 16 classes onto this platform's DocumentType vocabulary, which
# is institutional rather than archival. The mapping is lossy on purpose -
# several archival classes have no institutional equivalent and land in
# OTHER rather than being forced into a category that would misdescribe
# them. The TRUE class is never lost: it goes to the gold manifest, which
# is what classification accuracy is measured against.
LABEL_TO_TYPE = {
    "form": Document.DocumentType.FORM,
    "questionnaire": Document.DocumentType.FORM,
    "invoice": Document.DocumentType.FORM,
    "memo": Document.DocumentType.CIRCULAR,
    "email": Document.DocumentType.CIRCULAR,
    "letter": Document.DocumentType.CIRCULAR,
    "news article": Document.DocumentType.CIRCULAR,
    "specification": Document.DocumentType.REGULATION,
    "scientific publication": Document.DocumentType.REGULATION,
    "scientific report": Document.DocumentType.REGULATION,
    "budget": Document.DocumentType.POLICY,
    "presentation": Document.DocumentType.GENERAL,
    "advertisement": Document.DocumentType.GENERAL,
    "resume": Document.DocumentType.OTHER,
    "handwritten": Document.DocumentType.OTHER,
    "file folder": Document.DocumentType.OTHER,
}

# Which department owns which class. Chosen so the split is uneven the way
# a real organization's is - Finance holds two high-volume classes, HR one
# - because an evenly split corpus would make the permission filter look
# cheaper than it is.
LABEL_TO_DEPARTMENT = {
    "invoice": "Finance",
    "budget": "Finance",
    "resume": "HR",
    "specification": "R&D",
    "scientific report": "R&D",
    "scientific publication": "R&D",
    "news article": "Communications",
    "advertisement": "Communications",
    "presentation": "Communications",
    "memo": "Administration",
    "email": "Administration",
    "letter": "Administration",
    "form": "Administration",
    "questionnaire": "Administration",
    "handwritten": "Records",
    "file folder": "Records",
}

DEFAULT_DEPARTMENT = "Records"

# A document with almost no extracted text is not a document this corpus
# should contain: it produces no chunks, so it can never be retrieved, and
# it would sit in the denominator of every metric making retrieval look
# worse than it is. OCR failures are real and worth counting - which is why
# they are reported as skipped rather than silently dropped.
MIN_CHARACTERS = 200

# Title length ceiling on the model.
MAX_TITLE = 255


class Command(BaseCommand):
    help = "Ingest a document corpus through the full processing pipeline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            default="qs-ocr",
            help=(
                "'qs-ocr' for the vendored corpus, or a path to a directory "
                "of .txt files."
            ),
        )
        parser.add_argument(
            "--owner",
            required=True,
            help="Username that will own the ingested documents.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=1000,
            help="Maximum documents to ingest (default 1000). 0 means all.",
        )
        parser.add_argument(
            "--manifest-out",
            default="evaluation/corpus_manifest.json",
            help="Where to write the gold-label manifest.",
        )
        parser.add_argument(
            "--department",
            default="",
            help=(
                "Force every document into one department, instead of "
                "spreading them by class."
            ),
        )
        parser.add_argument(
            "--no-process",
            action="store_true",
            help=(
                "Create documents without queueing the pipeline. Use when "
                "the worker is down and you intend to reprocess later."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be ingested, write nothing.",
        )

    # --- source resolution ----------------------------------------------

    def _repo_root(self) -> Path:
        # BASE_DIR is backend/; the corpus is vendored a level above it.
        return Path(settings.BASE_DIR).parent

    def _load_qs_ocr(self) -> list:
        """Every manifest row, as (external_id, path, label).

        Read from the manifests rather than by walking the tree, because
        the manifests carry the labels and the tree does not. All three
        splits are used: the train/validation/test division exists for
        training a classifier, and has no meaning for a document store.
        """
        root = self._repo_root()
        manifest_dir = root / QS_OCR_MANIFESTS

        if not manifest_dir.is_dir():
            raise CommandError(
                f"QS-OCR manifests not found at {manifest_dir}. "
                f"Pass --source with a directory of .txt files instead."
            )

        rows = []
        for split in QS_OCR_SPLITS:
            path = manifest_dir / f"{split}.csv"
            if not path.is_file():
                continue

            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    raw = (row.get("input_path") or "").strip()
                    if not raw:
                        continue
                    # Manifests were written on Windows and carry
                    # backslash separators. Normalised here rather than
                    # depending on the platform the command runs on.
                    relative = Path(raw.replace("\\", "/"))
                    rows.append(
                        (
                            (row.get("sample_id") or "").strip() or relative.stem,
                            root / relative,
                            (row.get("label") or "").strip(),
                        )
                    )

        if not rows:
            raise CommandError(f"No rows in the manifests under {manifest_dir}.")

        return rows

    def _load_directory(self, source: str) -> list:
        directory = Path(source).expanduser()
        if not directory.is_dir():
            raise CommandError(f"--source {source!r} is not a directory.")

        files = sorted(directory.rglob("*.txt"))
        if not files:
            raise CommandError(f"No .txt files under {directory}.")

        # No manifest, so no labels. An empty label is honest; a guessed
        # one would poison the gold manifest that evaluation trusts.
        return [
            (str(path.relative_to(directory).with_suffix("")), path, "")
            for path in files
        ]

    # --- main -------------------------------------------------------------

    def handle(self, *args, **options):
        source = options["source"]
        limit = options["limit"]
        dry_run = options["dry_run"]
        forced_department = options["department"].strip()

        User = get_user_model()
        try:
            owner = User.objects.get(username=options["owner"])
        except User.DoesNotExist:
            raise CommandError(
                f"No user named {options['owner']!r}. Create one first: "
                f"manage.py createsuperuser"
            ) from None

        entries = (
            self._load_qs_ocr() if source == "qs-ocr" else self._load_directory(source)
        )

        self.stdout.write(f"Source {source!r}: {len(entries)} candidate document(s).")

        # Resume rather than duplicate. Titles carry the external id, which
        # is what makes an interrupted run of twelve thousand restartable.
        existing = set(
            Document.objects.filter(owner=owner).values_list("title", flat=True)
        )

        manifest = {}
        ingested = skipped_short = skipped_existing = skipped_unreadable = 0
        started = time.monotonic()
        target = limit if limit > 0 else len(entries)

        try:
            for external_id, path, label in entries:
                if ingested >= target:
                    break

                title = self._title(external_id, label)

                if title in existing:
                    skipped_existing += 1
                    continue

                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    # A missing or unreadable file is counted, not fatal: a
                    # vendored corpus can be partially checked out, and
                    # losing the whole run to one bad path would be worse
                    # than reporting it.
                    skipped_unreadable += 1
                    continue

                if len(text.strip()) < MIN_CHARACTERS:
                    skipped_short += 1
                    continue

                department = forced_department or LABEL_TO_DEPARTMENT.get(
                    label, DEFAULT_DEPARTMENT
                )
                document_type = LABEL_TO_TYPE.get(label, Document.DocumentType.GENERAL)

                if dry_run:
                    ingested += 1
                    continue

                document = Document.objects.create(
                    title=title,
                    document_type=document_type,
                    department=department,
                    owner=owner,
                    status=Document.Status.UPLOADED,
                )

                upload = SimpleUploadedFile(
                    name=f"{Path(external_id).name}.txt",
                    content=text.encode("utf-8"),
                    content_type="text/plain",
                )

                if options["no_process"]:
                    # Still a real version with a real checksum - only the
                    # queueing is skipped, so `reprocess` can pick it up later.
                    self._version_without_processing(document, upload, owner)
                else:
                    services.create_document_version(
                        document, upload, uploaded_by=owner
                    )

                manifest[str(document.id)] = {
                    "external_id": external_id,
                    "label": label,
                    "document_type": document_type,
                    "department": department,
                    "characters": len(text),
                }
                ingested += 1

                if ingested % 250 == 0:
                    rate = ingested / max(time.monotonic() - started, 1e-6)
                    self.stdout.write(f"  {ingested}/{target} ingested ({rate:.1f}/s)")
        finally:
            # Written even when the run is cut short. A resumed run skips
            # every document already ingested, so a label not written now is
            # never written: written only at the end, an interrupted run left
            # everything it had ingested out of the manifest for good.
            if not dry_run and manifest:
                self._write_manifest(manifest, options["manifest_out"])

        elapsed = time.monotonic() - started

        verb = "Would ingest" if dry_run else "Ingested"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {ingested} document(s) in {elapsed:.1f}s "
                f"({ingested / max(elapsed, 1e-6):.1f}/s)"
            )
        )
        self.stdout.write(
            f"Skipped: {skipped_existing} already ingested, "
            f"{skipped_short} under {MIN_CHARACTERS} characters, "
            f"{skipped_unreadable} unreadable"
        )

        if not dry_run and not options["no_process"]:
            self.stdout.write(
                "Processing is queued on the 'ingest' queue. Watch it drain "
                "with: docker compose logs -f worker"
            )

    # --- helpers ----------------------------------------------------------

    def _write_manifest(self, manifest: dict, manifest_out: str) -> None:
        out = self._repo_root() / manifest_out
        out.parent.mkdir(parents=True, exist_ok=True)
        # Merged rather than overwritten, so ingesting a second batch
        # does not erase the gold labels of the first.
        previous = {}
        if out.is_file():
            try:
                previous = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
        previous.update(manifest)
        out.write_text(json.dumps(previous, indent=2, sort_keys=True), encoding="utf-8")
        self.stdout.write(f"Gold manifest: {out} ({len(previous)} documents)")

    @staticmethod
    def _title(external_id: str, label: str) -> str:
        """A title carrying the external id, so a re-run can recognise it.

        The id leads rather than trails: it is what uniqueness is checked
        on, and a truncated title must not be able to collide with another
        document's.
        """
        suffix = f" ({label})" if label else ""
        room = MAX_TITLE - len(suffix)
        return f"{external_id[:room]}{suffix}"

    @staticmethod
    def _version_without_processing(document, upload, owner):
        """Create the version, but no job and nothing queued.

        No ProcessingJob either. A QUEUED job with no broker message counts
        as an active run, so the "reprocess later" this option is for was
        refused with 409 for two hours per document, and recovery never saw
        it (it only reclaims PROCESSING jobs). Reprocess creates the job.

        Deliberately duplicates the small part of
        `services.create_document_version` that is not the enqueue, rather
        than reaching into it: the service's contract is "store it and
        queue it", and a flag that quietly made it not queue would make
        every other caller's behaviour conditional on a parameter they do
        not pass.
        """
        import hashlib

        from documents.models import DocumentVersion

        hasher = hashlib.sha256()
        for chunk in upload.chunks():
            hasher.update(chunk)
        upload.seek(0)

        version = DocumentVersion.objects.create(
            document=document,
            version_number=1,
            file=upload,
            file_name=upload.name,
            file_size=upload.size,
            mime_type=upload.content_type or "",
            checksum=hasher.hexdigest(),
            uploaded_by=owner,
        )
        document.current_version = version
        document.status = Document.Status.UPLOADED
        document.save(update_fields=["current_version", "status", "updated_at"])
        return version
