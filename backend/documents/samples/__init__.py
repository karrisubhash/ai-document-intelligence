"""The sample library: a curated corpus that fills the demo, or an empty
installation, through the real upload path.

Sixteen synthetic institutional documents - regulations, policies, a
circular, lecture notes, a handbook and an academic calendar as text-layer
PDFs with tables, and a scanned notice that only OCR can read (see
corpus/manifest.json and build.py). Seeding them is not a fixture load:
each one is admitted by the same check as an upload (uploads.validate_upload),
stored as a real version with a real checksum by
services.create_document_version, audited as DOCUMENT_CREATE, and queued
for the whole pipeline - scanning, extraction or OCR, tables, chunking,
embedding. What a visitor then reads, searches and cites is exactly what an
uploaded document would give them.

Idempotent per department: a sample whose bytes are already there - any
version of any document in that department, archived ones included, has
its checksum - is skipped. Seeding twice, or after an administrator deleted
one sample, adds only what is missing.

The documents belong to a dedicated, inactive account (LIBRARY_USERNAME)
rather than to whoever asked for them: demo visitors see who owns and who
uploaded every document they read, and that must not be an administrator's
username. The account has no usable password and cannot sign in; the
department's administrators manage the samples like any of its documents.
"""

import hashlib
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

CORPUS = Path(__file__).resolve().parent / "corpus"
MANIFEST = CORPUS / "manifest.json"

LIBRARY_USERNAME = "sample-library"


@dataclass(frozen=True)
class Sample:
    file: str
    title: str
    document_type: str

    @property
    def path(self) -> Path:
        return CORPUS / self.file


@dataclass
class SeedOutcome:
    department: str
    queued: list = field(default_factory=list)  # Document instances created
    skipped: list = field(default_factory=list)  # file names already present


def samples() -> list[Sample]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [Sample(**entry) for entry in data["samples"]]


def library_owner():
    """The account the samples belong to, created on first use."""
    User = get_user_model()
    owner = User.objects.filter(username=LIBRARY_USERNAME).first()
    if owner is not None:
        return owner
    owner = User(
        username=LIBRARY_USERNAME,
        first_name="Sample",
        last_name="library",
        is_active=False,
    )
    owner.set_unusable_password()
    try:
        with transaction.atomic():
            owner.save()
    except IntegrityError:
        # Made by a simultaneous seed.
        owner = User.objects.get(username=LIBRARY_USERNAME)
    return owner


def seed(department: str, *, actor=None, via: str) -> SeedOutcome:
    """Queue every sample the department does not hold yet.

    `department` must already be the registry's spelling of an active
    department (accounts.services.resolve_department). `actor` is who asked
    - the administrator, or None for a management command - and is who the
    audit rows name; `via` says which door it came through. One
    SAMPLES_LOAD row records the request, beside a DOCUMENT_CREATE row per
    sample created.
    """
    from accounts.models import Department
    from documents.audit import record_event
    from documents.models import AuditLog, Document, DocumentVersion
    from documents.services import create_document_version
    from documents.uploads import validate_upload

    owner = library_owner()
    outcome = SeedOutcome(department=department)
    with transaction.atomic():
        # Locked, so two seeds of one department at once cannot both find a
        # sample missing and add it twice: the second waits, then skips.
        Department.objects.select_for_update().get(name=department)
        for sample in samples():
            data = sample.path.read_bytes()
            checksum = hashlib.sha256(data).hexdigest()
            if DocumentVersion.objects.filter(
                checksum=checksum, document__department=department
            ).exists():
                outcome.skipped.append(sample.file)
                continue

            upload = SimpleUploadedFile(
                sample.file,
                data,
                content_type=mimetypes.guess_type(sample.file)[0] or "",
            )
            validate_upload(upload, user=actor)
            document = Document.objects.create(
                title=sample.title,
                document_type=sample.document_type,
                department=department,
                owner=owner,
            )
            # Processing is queued when this transaction commits.
            create_document_version(document, upload, uploaded_by=owner)
            record_event(
                user=actor,
                action=AuditLog.Action.DOCUMENT_CREATE,
                document=document,
                result=AuditLog.Result.ALLOWED,
                sample=sample.file,
            )
            outcome.queued.append(document)

        record_event(
            user=actor,
            action=AuditLog.Action.SAMPLES_LOAD,
            result=AuditLog.Result.ALLOWED,
            subject_department=department,
            department=department,
            queued=len(outcome.queued),
            skipped=len(outcome.skipped),
            via=via,
        )
    return outcome
