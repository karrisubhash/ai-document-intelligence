"""Seed the evaluation corpus and resolve the benchmark against it.

    python manage.py seed_evaluation

The phase-8 regression gate was built, wired into CI, and could never fire,
because `evaluation/benchmark.json` did not exist. Writing that file by hand
does not fix it: cases name `relevant_documents`, those are database ids,
and a fresh CI database has none - so a committed benchmark with hardcoded
ids would score retrieval against documents that are not there and report a
confident zero.

So the governed artifact is the TEMPLATE, which refers to documents by slug
(`@fee-structure`), and this command is what turns it into a runnable
dataset: it ingests the corpus through the real pipeline, then substitutes
the ids it actually created.

Ingesting through the real pipeline rather than inserting rows is the point.
The benchmark then scores extraction, chunking, table detection, embedding
and retrieval as they are configured on the machine running it - which is
the only thing worth scoring.

Idempotent: re-running replaces the corpus documents and rewrites the file.
"""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from documents.models import Document, ProcessingJob
from documents.processing.pipeline import run_pipeline

# The repository root: manage.py lives in backend/, the corpus does not.
ROOT = Path(settings.BASE_DIR).parent
CORPUS = ROOT / "evaluation" / "corpus"
TEMPLATE = ROOT / "evaluation" / "benchmark.template.json"
OUTPUT = ROOT / "evaluation" / "benchmark.json"

# Every corpus document is owned by this user and scored as this user.
# A benchmark is scored THROUGH the permission filter, so the answer
# depends on who is asking - see the --as-user requirement on `evaluate`.
EVALUATOR = "ci-evaluator"
DEPARTMENT = "Evaluation"

TITLE_PREFIX = "[eval] "


class Command(BaseCommand):
    help = "Ingest evaluation/corpus/ and resolve benchmark.template.json."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            default=EVALUATOR,
            help=f"Username to own and score the corpus (default {EVALUATOR}).",
        )
        parser.add_argument(
            "--output",
            default=str(OUTPUT),
            help="Where to write the resolved benchmark.",
        )

    def handle(self, *args, **options):
        if not TEMPLATE.exists():
            raise CommandError(f"{TEMPLATE} is missing")
        if not CORPUS.is_dir():
            raise CommandError(f"{CORPUS} is missing")

        user = self._evaluator(options["user"])
        ids = self._ingest(user)
        self._resolve(ids, Path(options["output"]))

    def _evaluator(self, username):
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username, defaults={"email": f"{username}@example.invalid"}
        )
        if created:
            # No usable password: this account exists to own documents and
            # to be the subject of a permission check, never to log in.
            user.set_unusable_password()
            user.save(update_fields=["password"])

        profile = getattr(user, "profile", None)
        if profile is not None and profile.department != DEPARTMENT:
            profile.department = DEPARTMENT
            profile.save(update_fields=["department"])

        self.stdout.write(f"evaluator: {user.username} ({DEPARTMENT})")
        return user

    def _ingest(self, user):
        sources = sorted(CORPUS.glob("*.txt"))
        if not sources:
            raise CommandError(f"no .txt documents in {CORPUS}")

        ids = {}
        for source in sources:
            slug = source.stem
            body = source.read_bytes()

            # Reused, not recreated. A loaded EvaluationDataset stores
            # document IDS, and datasets are immutable once loaded - so a
            # seeder that deleted and recreated the corpus would leave every
            # previously-loaded benchmark pointing at rows that no longer
            # exist, and score retrieval as a total miss against documents
            # that are sitting right there. Stable ids keep an old dataset
            # valid across a re-seed.
            document, _ = Document.objects.get_or_create(
                title=f"{TITLE_PREFIX}{slug}",
                owner=user,
                defaults={"document_type": "OTHER", "department": DEPARTMENT},
            )

            with transaction.atomic():
                # The versions and their content go; the document stays.
                document.current_version = None
                document.save(update_fields=["current_version"])
                document.versions.all().delete()

            version = document.versions.create(
                version_number=1,
                file=ContentFile(body, name=source.name),
                file_name=source.name,
                file_size=len(body),
                uploaded_by=user,
            )
            document.current_version = version
            document.save(update_fields=["current_version"])

            job = ProcessingJob.objects.create(document_version=version)
            run_pipeline(job.id)

            job.refresh_from_db()
            if job.status != ProcessingJob.Status.COMPLETED:
                raise CommandError(
                    f"{slug} failed to process ({job.status}: "
                    f"{job.error_message}). A benchmark over a corpus that "
                    f"did not ingest would score the pipeline against nothing."
                )

            ids[slug] = document.id
            self.stdout.write(f"  ingested {slug} -> document {document.id}")

        return ids

    def _resolve(self, ids, output):
        data = json.loads(TEMPLATE.read_text(encoding="utf-8"))

        for index, case in enumerate(data.get("cases", [])):
            resolved = []
            for reference in case.get("relevant_documents", []) or []:
                if not str(reference).startswith("@"):
                    resolved.append(reference)
                    continue
                slug = str(reference)[1:]
                if slug not in ids:
                    raise CommandError(
                        f"case {index} references @{slug}, which is not in "
                        f"{CORPUS}. A silently dropped reference would score "
                        f"retrieval as a miss for a document that exists."
                    )
                resolved.append(ids[slug])
            if resolved:
                case["relevant_documents"] = resolved

        # A resolved benchmark is specific to one database, so it is a build
        # artifact rather than something to commit. The TEMPLATE is what is
        # governed and reviewed.
        data["description"] = (
            data.get("description", "")
            + " Resolved from benchmark.template.json by `manage.py "
            "seed_evaluation`; document ids are specific to this database."
        )

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        answerable = sum(
            1 for case in data["cases"] if not case.get("expect_abstention")
        )
        abstaining = len(data["cases"]) - answerable
        self.stdout.write(
            self.style.SUCCESS(
                f"wrote {output} - {len(data['cases'])} case(s): "
                f"{answerable} answerable, {abstaining} expecting abstention"
            )
        )
