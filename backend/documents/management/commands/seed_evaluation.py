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

THE CORPUS IS PART OF THE DATASET, and that is not a detail. A benchmark is
scored through `rag.ask`, which retrieves everything the evaluator user is
authorized to read - so the exam is not the 13 questions, it is the 13
questions ASKED OF A PARTICULAR SET OF DOCUMENTS. Adding a document to
`evaluation/corpus/` changes every benchmark scored under that user, whether
or not any case mentions it.

That happened. v2 of the benchmark added four documents to the shared corpus
directory; this command ingested the whole directory; and v1 - whose cases
were written against five documents - was suddenly scored against nine. Its
committed baseline stopped matching and the regression gate failed, reporting
precision_at_k.10 -0.0889 and citation_accuracy -0.0740. Nothing about
retrieval had got worse. The exam had changed, which is exactly what the
governance rule on datasets exists to prevent - applied to the half of the
dataset that lives on disk rather than in the JSON.

So this command ingests the documents THIS TEMPLATE declares, and nothing
else. A template may list them explicitly under `corpus`; otherwise they are
the slugs its own cases reference. And because two templates with different
corpora cannot both be scored under one user - retrieval would see the union
- seeding a second one under the same user is refused rather than silently
producing a third, unnamed exam.
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
        # A dataset is immutable once loaded, so improving the benchmark
        # means adding a VERSION beside the old one rather than editing it -
        # otherwise last month's score and this month's describe different
        # exams. That only works if the seeder can resolve more than the one
        # hardcoded template, which is why this exists.
        parser.add_argument(
            "--template",
            default=str(TEMPLATE),
            help="Benchmark template to resolve (default benchmark.template.json).",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help=(
                "Remove [eval] documents owned by --user that are outside "
                "this template's corpus. Needed when switching a database "
                "from one benchmark version to another; it INVALIDATES any "
                "loaded dataset that referenced them."
            ),
        )

    def handle(self, *args, **options):
        template = Path(options["template"])
        if not template.exists():
            raise CommandError(f"{template} is missing")
        if not CORPUS.is_dir():
            raise CommandError(f"{CORPUS} is missing")

        data = json.loads(template.read_text(encoding="utf-8"))
        slugs = self._corpus_slugs(data, template)

        user = self._evaluator(options["user"])
        self._check_for_foreign_documents(user, slugs, prune=options["prune"])
        ids = self._ingest(user, slugs)
        self._resolve(data, ids, Path(options["output"]))

    @staticmethod
    def _corpus_slugs(data, template) -> list:
        """Which documents this benchmark is scored against.

        An explicit `corpus` list wins, because a benchmark may legitimately
        want a distractor that no case names as relevant - that is part of
        what makes a retrieval score mean something. Falling back to the
        slugs the cases reference keeps existing templates working without an
        edit, and is the conservative reading: a document nothing references
        cannot have been intended as part of the exam.
        """
        declared = data.get("corpus")
        if declared:
            return sorted({str(slug).lstrip("@") for slug in declared})

        referenced = {
            str(reference).lstrip("@")
            for case in data.get("cases", [])
            for reference in (case.get("relevant_documents") or [])
            if str(reference).startswith("@")
        }
        if not referenced:
            raise CommandError(
                f"{template} declares no `corpus` and no case references a "
                f"@slug, so there is nothing to score against."
            )
        return sorted(referenced)

    def _check_for_foreign_documents(self, user, slugs, *, prune=False):
        """Refuse to score one benchmark's questions over another's corpus.

        Retrieval sees every document this user may read, so a database that
        already holds another version's corpus under the same user produces a
        score belonging to neither benchmark. There is no way to detect that
        from the numbers afterwards - they simply come out lower, and look
        like a quality regression.
        """
        wanted = {f"{TITLE_PREFIX}{slug}" for slug in slugs}
        foreign = list(
            Document.objects.filter(owner=user, title__startswith=TITLE_PREFIX).exclude(
                title__in=wanted
            )
        )
        if not foreign:
            return

        names = sorted(
            document.title.removeprefix(TITLE_PREFIX) for document in foreign
        )

        if not prune:
            raise CommandError(
                f"{user.username} already owns {len(foreign)} [eval] "
                f"document(s) outside this template's corpus: "
                f"{', '.join(names)}. Scoring here would ask this benchmark's "
                f"questions over a larger corpus than it was written for. "
                f"Seed the other benchmark under its own --user, or pass "
                f"--prune to remove these (which invalidates any loaded "
                f"dataset that references them)."
            )

        for document in foreign:
            document.current_version = None
            document.save(update_fields=["current_version"])
        Document.objects.filter(id__in=[d.id for d in foreign]).delete()
        self.stdout.write(
            self.style.WARNING(f"pruned {len(foreign)}: {', '.join(names)}")
        )

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

    def _ingest(self, user, slugs):
        sources = [CORPUS / f"{slug}.txt" for slug in slugs]
        missing = [source.name for source in sources if not source.exists()]
        if missing:
            raise CommandError(
                f"{CORPUS} is missing {', '.join(missing)}. A benchmark "
                f"resolved without them would score retrieval as a miss for "
                f"documents it expected to be there."
            )

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

    def _resolve(self, data, ids, output):
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
            + " Resolved by `manage.py seed_evaluation`; document ids are "
            "specific to this database, and the corpus is exactly the "
            f"document(s) this template declares: {', '.join(sorted(ids))}."
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
