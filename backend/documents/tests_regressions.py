"""Regressions for bugs found by auditing the codebase.

Each test names the defect it pins down. They live together rather than
scattered into the suites for the modules they touch, so that the cost of
each past mistake stays visible in one place.
"""

import os
from io import BytesIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db.models import F
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import UserProfile
from documents import authorization
from documents.models import (
    ClassificationResult,
    Document,
    DocumentChunk,
    DocumentContent,
    ExtractionResult,
    PageContent,
    ProcessingJob,
)
from documents.processing.classification import service as classification_service
from documents.processing.errors import ErrorCode, StageError
from documents.processing.field_extraction import service as extraction_service
from documents.processing.field_extraction.matching import FOUND, INVALID, find_field
from documents.processing.field_extraction.rule_extractor import DEFAULT_PACK
from documents.processing.field_extraction.schemas import load_pack_data
from documents.processing.pipeline import run_pipeline
from documents.processing.scanning import DocumentInfected
from documents.processing.scanning import service as scanning_service
from documents.processing.scanning.heuristic import EICAR
from documents.tests import MEDIA_ROOT, make_user
from documents.tests_ingestion import make_pdf_bytes

RULE_EXTRACTOR = (
    "documents.processing.field_extraction.rule_extractor.RuleFieldExtractor"
)


# Module-level so import_string can actually reach it by dotted path.
ARTIFACTS_LOADED = []


class RecordingClassifier:
    """Records which artifact setting was in force when it was built.

    Used to prove the loader cache really is keyed on the artifact, rather
    than merely accepting a parameter named after it.
    """

    name = "recording"
    version = "0.0.1"

    def __init__(self):
        from django.conf import settings as live

        ARTIFACTS_LOADED.append(getattr(live, "DOCUMENT_CLASSIFIER_ARTIFACT", ""))

    def predict(self, *, document_content_id):
        raise NotImplementedError


def rule_for(pattern, **extra):
    pack = load_pack_data(
        {
            "name": "t",
            "version": "1",
            "schemas": {"X": [{"name": "f", "patterns": [pattern], **extra}]},
        }
    )
    return pack.for_document_type("X").fields[0]


class EmptyDepartmentIsNotATenantTests(TestCase):
    """An unset department must not make two strangers colleagues.

    UserProfile.department and Document.department both default to "", and
    the default role is STUDENT - so plain equality made every freshly
    registered user able to read every other one's uploads.
    """

    def setUp(self):
        # Deliberately NOT make_user(), which assigns a department. The bug
        # only exists for users who have never been given one - which is
        # exactly the state the post_save signal leaves a new account in.
        self.alice = get_user_model().objects.create_user(
            username="regress_alice", password="pass1234"
        )
        self.bob = get_user_model().objects.create_user(
            username="regress_bob", password="pass1234"
        )
        self.document = Document.objects.create(
            title="Alice's upload", owner=self.alice, department=""
        )

    def test_profiles_really_do_default_to_an_empty_department(self):
        """The precondition for the bug, pinned so it stays true."""
        self.assertEqual(self.alice.profile.department, "")
        self.assertEqual(self.bob.profile.department, "")
        self.assertEqual(self.alice.profile.role, UserProfile.Role.STUDENT)

    def test_stranger_cannot_view_a_document_with_no_department(self):
        self.assertFalse(authorization.can_view_document(self.bob, self.document))

    def test_stranger_cannot_manage_a_document_with_no_department(self):
        self.assertFalse(authorization.can_manage_document(self.bob, self.document))

    def test_document_admin_with_no_department_cannot_manage_it_either(self):
        profile = self.bob.profile
        profile.role = UserProfile.Role.DEPARTMENT_ADMIN
        profile.save(update_fields=["role"])

        self.assertFalse(authorization.can_manage_document(self.bob, self.document))

    def test_listing_hides_it_from_a_stranger(self):
        visible = authorization.visible_documents(
            self.bob, Document.objects.filter(id=self.document.id)
        )
        self.assertFalse(visible.exists())

    def test_owner_still_sees_their_own(self):
        self.assertTrue(authorization.can_view_document(self.alice, self.document))
        self.assertTrue(
            authorization.visible_documents(
                self.alice, Document.objects.filter(id=self.document.id)
            ).exists()
        )

    def test_a_real_shared_department_still_grants_access(self):
        """The fix must not break the feature it protects."""
        for user in (self.alice, self.bob):
            profile = user.profile
            profile.department = "Computer Science"
            profile.save(update_fields=["department"])

        self.document.department = "Computer Science"
        self.document.save(update_fields=["department"])

        self.assertTrue(authorization.can_view_document(self.bob, self.document))

    def test_listing_never_shows_more_than_retrieve_allows(self):
        """The two paths must agree, or the listing is itself the leak."""
        visible = set(
            authorization.visible_documents(
                self.bob, Document.objects.all()
            ).values_list("id", flat=True)
        )
        for document in Document.objects.all():
            allowed = authorization.can_view_document(self.bob, document)
            self.assertEqual(document.id in visible, allowed)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class RetryDoesNotDuplicateContentTests(TestCase):
    """Spec 10.2: a repeated message must not create duplicate content.

    Every stage creates rather than updates, so before the fix each retry
    of the same job appended another full copy of the document's pages -
    which would become duplicate chunks and embeddings in phase 5.
    """

    def setUp(self):
        user = make_user("regress_retry")
        self.document = Document.objects.create(
            title="Retry probe", document_type="CIRCULAR", owner=user
        )
        self.version = self.document.versions.create(
            version_number=1,
            file=ContentFile(b"Circular No: REG/2024/117\n", name="probe.txt"),
            file_name="probe.txt",
            file_size=26,
            uploaded_by=user,
        )
        self.document.current_version = self.version
        self.document.save(update_fields=["current_version"])
        self.job = ProcessingJob.objects.create(document_version=self.version)

    def retry(self):
        """Run the job again the way a genuine Celery retry would.

        A retry follows a retryable FAILURE: the pipeline marks the job
        FAILED and Celery re-invokes it. Re-running a job that COMPLETED is
        a different event - a broker redelivery - which the pipeline now
        skips outright (tests_lifecycle pins that). So between runs the job
        is put back in the state a real retry finds it in.
        """
        ProcessingJob.objects.filter(pk=self.job.pk).update(
            status=ProcessingJob.Status.FAILED,
            error_code=ErrorCode.INFRASTRUCTURE_ERROR,
        )
        run_pipeline(self.job.id)

    def test_rerunning_the_same_job_replaces_rather_than_appends(self):
        run_pipeline(self.job.id)
        for _ in range(2):
            self.retry()

        self.assertEqual(
            DocumentContent.objects.filter(processing_job=self.job).count(), 1
        )
        self.assertEqual(
            PageContent.objects.filter(content__processing_job=self.job).count(), 1
        )

    def test_the_attempt_counter_still_records_every_try(self):
        """Deduplicating output must not erase the evidence of retries."""
        run_pipeline(self.job.id)
        for _ in range(2):
            self.retry()

        self.job.refresh_from_db()
        self.assertEqual(self.job.attempt, 3)

    def test_events_from_earlier_attempts_are_preserved(self):
        run_pipeline(self.job.id)
        first = self.job.events.count()
        self.retry()

        self.assertEqual(self.job.events.count(), first * 2)

    def test_analysis_results_do_not_accumulate_across_attempts(self):
        extraction_service.reset_extractor_cache()
        self.addCleanup(extraction_service.reset_extractor_cache)

        with override_settings(DOCUMENT_FIELD_EXTRACTOR=RULE_EXTRACTOR):
            run_pipeline(self.job.id)
            self.retry()

        self.assertEqual(
            ExtractionResult.objects.filter(processing_job=self.job).count(), 1
        )
        self.assertFalse(
            ClassificationResult.objects.filter(processing_job=self.job).exists()
        )

    def test_a_failed_retry_does_not_destroy_the_previous_good_content(self):
        """The first fix for the duplication bug was itself wrong.

        Discarding up front, before the replacement existed, meant a retry
        that failed early - a storage blip, a provider outage - deleted the
        content the previous attempt had successfully produced and put
        nothing in its place. Trading duplicate data for missing data is
        the worse bargain of the two.
        """
        run_pipeline(self.job.id)
        self.assertEqual(
            DocumentContent.objects.filter(processing_job=self.job).count(), 1
        )

        # Make the next attempt fail in INGESTION, before any content could
        # be produced.
        path = self.version.file.path
        os.rename(path, path + ".moved")
        self.addCleanup(
            lambda: os.path.exists(path + ".moved") and os.rename(path + ".moved", path)
        )

        with self.assertRaises(StageError):
            self.retry()

        self.assertEqual(
            DocumentContent.objects.filter(processing_job=self.job).count(),
            1,
            "the previous attempt's content must survive a failed retry",
        )
        self.assertEqual(
            PageContent.objects.filter(content__processing_job=self.job).count(), 1
        )

    def test_extracted_text_stays_consistent_with_the_page_rows(self):
        """The convenience copy on DocumentVersion and the page rows are
        written in one transaction, so they cannot disagree."""
        run_pipeline(self.job.id)
        self.version.refresh_from_db()

        pages = PageContent.objects.filter(content__processing_job=self.job).order_by(
            "page_number"
        )
        self.assertEqual(
            self.version.extracted_text,
            "\n".join(page.text for page in pages).strip(),
        )

    def test_a_separate_job_still_appends_history(self):
        """Idempotency is per job - reprocessing must stay comparable."""
        run_pipeline(self.job.id)
        second = ProcessingJob.objects.create(document_version=self.version)
        run_pipeline(second.id)

        self.assertEqual(
            DocumentContent.objects.filter(document_version=self.version).count(), 2
        )


class ExtractorCacheKeyTests(TestCase):
    """The loader cache was keyed on the dotted path alone, so changing the
    schema pack silently kept serving an extractor holding the old one."""

    def setUp(self):
        extraction_service.reset_extractor_cache()
        self.addCleanup(extraction_service.reset_extractor_cache)

    def test_changing_the_pack_setting_reloads_the_extractor(self):
        with override_settings(
            DOCUMENT_FIELD_EXTRACTOR=RULE_EXTRACTOR, DOCUMENT_FIELD_SCHEMA_PACK=""
        ):
            first = extraction_service.get_extractor()

        with override_settings(
            DOCUMENT_FIELD_EXTRACTOR=RULE_EXTRACTOR,
            DOCUMENT_FIELD_SCHEMA_PACK=str(DEFAULT_PACK.parent / "invoice-v1.json"),
        ):
            second = extraction_service.get_extractor()

        self.assertEqual(first.pack.name, "institutional")
        self.assertEqual(second.pack.name, "invoice")
        self.assertIsNot(first, second)

    def test_an_unchanged_pack_still_reuses_the_instance(self):
        """The cache must still do its job, or every document pays to
        recompile the pack."""
        with override_settings(
            DOCUMENT_FIELD_EXTRACTOR=RULE_EXTRACTOR, DOCUMENT_FIELD_SCHEMA_PACK=""
        ):
            self.assertIs(
                extraction_service.get_extractor(),
                extraction_service.get_extractor(),
            )

    def test_classifier_cache_is_keyed_on_its_artifact_too(self):
        """Behavioural, not introspective: an assertion about the function
        signature would still pass if get_classifier forgot to pass the
        artifact through."""
        classification_service.reset_classifier_cache()
        self.addCleanup(classification_service.reset_classifier_cache)

        ARTIFACTS_LOADED.clear()
        loaded = ARTIFACTS_LOADED
        path = f"{__name__}.RecordingClassifier"

        with override_settings(
            DOCUMENT_CLASSIFIER=path, DOCUMENT_CLASSIFIER_ARTIFACT="/models/a.joblib"
        ):
            first = classification_service.get_classifier()
        with override_settings(
            DOCUMENT_CLASSIFIER=path, DOCUMENT_CLASSIFIER_ARTIFACT="/models/b.joblib"
        ):
            second = classification_service.get_classifier()

        self.assertIsNot(first, second)
        self.assertEqual(loaded, ["/models/a.joblib", "/models/b.joblib"])


class ValuePatternIsCaseSensitiveTests(TestCase):
    """value_pattern was compiled with IGNORECASE, which defeated the
    constraint most of them exist to express: [A-Z]{3} accepted "usd"."""

    def test_lowercase_is_rejected_by_an_uppercase_value_pattern(self):
        rule = rule_for(r"cur:\s*(?P<value>\w{3})", value_pattern=r"[A-Z]{3}")
        self.assertEqual(find_field(rule, [(1, "cur: usd")]).status, INVALID)

    def test_uppercase_still_passes(self):
        rule = rule_for(r"cur:\s*(?P<value>\w{3})", value_pattern=r"[A-Z]{3}")
        self.assertEqual(find_field(rule, [(1, "cur: USD")]).status, FOUND)

    def test_search_patterns_remain_case_insensitive(self):
        """Headings vary in case in real documents; only the *constraint*
        became case-sensitive."""
        rule = rule_for(r"subject:\s*(?P<value>\w+)")
        self.assertEqual(find_field(rule, [(1, "SUBJECT: Revised")]).value, "Revised")

    def test_shipped_course_code_still_accepts_lowercase_ocr(self):
        pack = load_pack_data(
            {
                "name": "t",
                "version": "1",
                "schemas": {
                    "X": [
                        {
                            "name": "c",
                            "patterns": [r"code:\s*(?P<value>\S+)"],
                            "value_pattern": r"[A-Za-z]{2,4}\s?\d{3,5}",
                        }
                    ]
                },
            }
        )
        rule = pack.for_document_type("X").fields[0]
        self.assertEqual(find_field(rule, [(1, "code: cs101")]).status, FOUND)


class OptionalValueGroupTests(TestCase):
    """A value group inside an optional branch returns None, which crashed
    normalization with AttributeError instead of being skipped."""

    def test_non_participating_group_is_skipped_not_crashed(self):
        rule = rule_for(r"(?:ref(?P<value>\d+))?item")
        match = find_field(rule, [(1, "item")])
        self.assertEqual(match.status, "NOT_FOUND")

    def test_the_same_pattern_still_matches_when_the_group_participates(self):
        rule = rule_for(r"(?:ref(?P<value>\d+))?item")
        self.assertEqual(find_field(rule, [(1, "ref42item")]).value, "42")


class DuplicateSpanTests(TestCase):
    """Two patterns matching the same text counted one occurrence twice."""

    def test_overlapping_patterns_count_one_occurrence(self):
        pack = load_pack_data(
            {
                "name": "t",
                "version": "1",
                "schemas": {
                    "X": [
                        {
                            "name": "f",
                            "patterns": [r"no:\s*(?P<value>R\d+)", r"(?P<value>R\d+)"],
                        }
                    ]
                },
            }
        )
        rule = pack.for_document_type("X").fields[0]
        match = find_field(rule, [(1, "no: R20")])

        self.assertEqual(match.status, FOUND)
        self.assertEqual(match.metadata["occurrences"], 1)
        self.assertEqual(len(match.spans), 1)

    def test_genuinely_repeated_values_are_still_counted_separately(self):
        rule = rule_for(r"no:\s*(?P<value>R\d+)")
        match = find_field(rule, [(1, "no: R20"), (2, "no: R20")])
        self.assertEqual(match.metadata["occurrences"], 2)


class ArtifactPathGuardTests(TestCase):
    """Path("") is PosixPath("."), whose str() is truthy - so the "not set"
    guard never fired and reported "No model artifact at ." instead."""

    def test_unset_artifact_reports_that_it_is_unset(self):
        from documents.processing.classification.exceptions import (
            ClassifierUnavailable,
        )
        from documents.processing.classification.text_classifier import (
            TextDocumentClassifier,
        )

        with override_settings(DOCUMENT_CLASSIFIER_ARTIFACT=""):
            with self.assertRaises(ClassifierUnavailable) as caught:
                TextDocumentClassifier()

        self.assertIn("is not set", str(caught.exception))


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class PaginationTests(TestCase):
    """Spec 16 requires pagination on collection endpoints."""

    def setUp(self):
        self.user = make_user("regress_pages")
        profile = self.user.profile
        profile.department = "Registry"
        profile.save(update_fields=["department"])

        self.client = APIClient()
        self.client.force_authenticate(self.user)

        for index in range(3):
            Document.objects.create(
                title=f"Doc {index}", owner=self.user, department="Registry"
            )

    def test_list_is_paginated(self):
        response = self.client.get(reverse("document-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("count", response.data)
        self.assertIn("results", response.data)
        self.assertEqual(response.data["count"], 3)

    def test_page_size_is_bounded(self):
        from django.conf import settings

        self.assertLessEqual(settings.REST_FRAMEWORK["PAGE_SIZE"], 100)

    def test_pagination_respects_authorization(self):
        """Paging must not become a way around the tenant filter."""
        stranger = make_user("regress_stranger")
        client = APIClient()
        client.force_authenticate(stranger)

        response = client.get(reverse("document-list"))
        self.assertEqual(response.data["count"], 0)


class AdminIsDepartmentScopedTests(TestCase):
    """The Django admin bypassed documents.authorization entirely.

    `is_staff` only means "may open the admin". Before this, any staff
    account read every department's documents there - the one place the
    access rules are supposed to live, routed around.
    """

    def setUp(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from documents.admin import DocumentAdmin

        self.factory = RequestFactory()
        self.admin = DocumentAdmin(Document, AdminSite())

        self.cs = make_user("admin_cs", department="Computer Science")
        self.mech = make_user("admin_mech", department="Mechanical")
        for user in (self.cs, self.mech):
            user.is_staff = True
            user.save(update_fields=["is_staff"])

        self.cs_doc = Document.objects.create(
            title="CS doc", owner=self.cs, department="Computer Science"
        )
        self.mech_doc = Document.objects.create(
            title="Mech doc", owner=self.mech, department="Mechanical"
        )

    def queryset_for(self, user):
        request = self.factory.get("/admin/documents/document/")
        request.user = user
        return self.admin.get_queryset(request)

    def test_staff_user_sees_only_their_department(self):
        titles = set(self.queryset_for(self.cs).values_list("title", flat=True))
        self.assertEqual(titles, {"CS doc"})

    def test_other_department_document_is_hidden(self):
        self.assertNotIn(
            self.mech_doc.id,
            set(self.queryset_for(self.cs).values_list("id", flat=True)),
        )

    def test_system_admin_still_sees_everything(self):
        root = make_user("admin_root", role=UserProfile.Role.SYSTEM_ADMIN)
        root.is_staff = True
        root.save(update_fields=["is_staff"])

        self.assertEqual(self.queryset_for(root).count(), 2)

    def test_staff_with_no_department_sees_only_what_they_own(self):
        stray = get_user_model().objects.create_user(
            username="admin_stray", password="pass1234", is_staff=True
        )
        self.assertEqual(self.queryset_for(stray).count(), 0)

    def test_admin_scoping_matches_the_api_rule(self):
        """Admin and API must not disagree about who may see what."""
        visible = set(self.queryset_for(self.cs).values_list("id", flat=True))
        for document in Document.objects.all():
            self.assertEqual(
                document.id in visible,
                authorization.can_view_document(self.cs, document),
            )


def encrypted_pdf_bytes(user_password="secret"):
    """A real password-protected PDF. RC4, which pypdf writes and reads
    without the optional cryptography package."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(BytesIO(make_pdf_bytes(["Salary statement"]))).pages:
        writer.add_page(page)
    writer.encrypt(
        user_password=user_password, owner_password="owner", algorithm="RC4-128"
    )
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class PipelineInputRegressionTests(TestCase):
    """Inputs the pipeline mishandled: each one either escaped as
    UNEXPECTED_ERROR, or completed with content that was not there."""

    def setUp(self):
        self.user = make_user("regress_inputs")

    def job_for(self, payload, name):
        document = Document.objects.create(title=name, owner=self.user)
        version = document.versions.create(
            version_number=1,
            file=ContentFile(payload, name=name),
            file_name=name,
            file_size=len(payload),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        return ProcessingJob.objects.create(document_version=version)

    def failed_job(self, payload, name):
        job = self.job_for(payload, name)
        with self.assertRaises(StageError):
            run_pipeline(job.id)
        job.refresh_from_db()
        return job

    def stored_page(self, payload, name):
        job = self.job_for(payload, name)
        run_pipeline(job.id)
        return PageContent.objects.get(content__processing_job=job)

    def test_a_password_protected_pdf_is_a_document_fault_not_a_bug(self):
        """pypdf builds a reader for an encrypted file without complaint and
        fails only when the page tree is first read - which VALIDATION did
        unguarded, so the job said UNEXPECTED_ERROR, "File has not been
        decrypted", for what is a property of the upload."""
        job = self.failed_job(encrypted_pdf_bytes(), "statement.pdf")

        self.assertEqual(job.error_code, ErrorCode.CORRUPT_DOCUMENT)
        self.assertEqual(job.current_stage, ProcessingJob.Stage.VALIDATION)
        self.assertIn("password", job.error_message)

    def test_an_unreadable_page_tree_is_corrupt_not_unexpected(self):
        # Same length, so every xref offset still holds: only the catalog's
        # /Pages reference now points at an object that does not exist.
        broken = make_pdf_bytes(["Body"]).replace(b"/Pages 2 0 R", b"/Pages 9 0 R", 1)

        job = self.failed_job(broken, "tree.pdf")

        self.assertEqual(job.error_code, ErrorCode.CORRUPT_DOCUMENT)
        self.assertEqual(job.current_stage, ProcessingJob.Stage.VALIDATION)

    def test_a_nul_only_text_layer_is_sent_to_ocr_not_stored_blank(self):
        """has_text was judged before NULs were stripped: a page whose text
        layer was nothing but NUL bytes skipped OCR, passed the "some text
        must exist" check, and the document COMPLETED with a blank page."""
        with mock.patch(
            "documents.processing.extractors.ocr.ocr_pdf_page",
            return_value="Notice recovered by OCR",
        ) as ocr:
            page = self.stored_page(make_pdf_bytes(["\x00\x00\x00\x00"]), "nul.pdf")

        ocr.assert_called_once()
        self.assertEqual(page.extraction_method, PageContent.ExtractionMethod.OCR)
        self.assertEqual(page.text, "Notice recovered by OCR")

    def test_a_text_file_of_nul_bytes_is_empty_not_completed(self):
        """The same check, where nothing can be OCR'd: it has to fail."""
        job = self.failed_job(b"\x00\x00 \x00\n", "zeroed.txt")

        self.assertEqual(job.error_code, ErrorCode.EMPTY_EXTRACTION)

    def test_character_count_describes_the_stored_text(self):
        """Counted before the NULs were stripped, so it disagreed with the
        very row it was stored on."""
        page = self.stored_page(b"Attendance\x00 minimum is 75 percent.\n", "n.txt")

        self.assertNotIn("\x00", page.text)
        self.assertEqual(page.character_count, len(page.text))

    def test_a_utf8_byte_order_mark_is_not_stored_as_text(self):
        """Decoded as plain utf-8, the BOM survived as U+FEFF at the start of
        page 1 - where the shipped issuing_authority rule, anchored with ^,
        then failed to match the document's first line."""
        page = self.stored_page(
            b"\xef\xbb\xbfFrom: Office of the Registrar\n", "bom.txt"
        )

        self.assertTrue(page.text.startswith("From: Office of the Registrar"))

    def test_a_quarantine_is_malware_on_the_stage_event_too(self):
        """DocumentInfected is a scanner exception and carries no code, and
        the pipeline mapped it only when writing the job: the job said
        MALWARE_DETECTED while the SCANNING event (and the failure metric)
        said UNEXPECTED_ERROR."""
        scanning_service.reset_scanner_cache()
        self.addCleanup(scanning_service.reset_scanner_cache)
        job = self.job_for(EICAR, "eicar.txt")

        scanner = "documents.processing.scanning.heuristic.HeuristicScanner"
        with override_settings(DOCUMENT_SCANNER=scanner):
            with self.assertRaises(DocumentInfected):
                run_pipeline(job.id)

        job.refresh_from_db()
        event = job.events.get(stage=ProcessingJob.Stage.SCANNING)
        self.assertEqual(job.error_code, ErrorCode.MALWARE_DETECTED)
        self.assertEqual(event.metadata["error_code"], ErrorCode.MALWARE_DETECTED)

    def test_a_later_quarantine_withdraws_the_live_text(self):
        """A version that completed before a scanner was configured, then
        quarantined on a rerun, kept current_content - so search and chat,
        which filter on it, went on answering from the flagged file."""
        scanning_service.reset_scanner_cache()
        self.addCleanup(scanning_service.reset_scanner_cache)
        first = self.job_for(EICAR, "eicar.txt")
        version = first.document_version

        run_pipeline(first.id)  # no scanner configured: processed as text
        version.refresh_from_db()
        self.assertIsNotNone(version.current_content_id)

        rerun = ProcessingJob.objects.create(document_version=version)
        scanner = "documents.processing.scanning.heuristic.HeuristicScanner"
        with override_settings(DOCUMENT_SCANNER=scanner):
            with self.assertRaises(DocumentInfected):
                run_pipeline(rerun.id)

        version.refresh_from_db()
        self.assertIsNone(version.current_content_id)
        self.assertEqual(version.extracted_text, "")
        # Withdrawn, not destroyed: the earlier pass is still on record.
        self.assertEqual(version.contents.count(), 1)
        self.assertFalse(
            DocumentChunk.objects.filter(
                document_version=version,
                document_content=F("document_version__current_content"),
            ).exists()
        )

    def test_a_later_quarantine_withdraws_the_text_from_the_api_too(self):
        """With the pointer cleared, the API fell back to the version's
        newest pass - the condemned one - for content, chunks and tables,
        and served any of its passages by id through `source`."""
        scanning_service.reset_scanner_cache()
        self.addCleanup(scanning_service.reset_scanner_cache)
        first = self.job_for(EICAR, "eicar.txt")
        version = first.document_version
        document = version.document
        document.department = self.user.profile.department
        document.save(update_fields=["department"])

        run_pipeline(first.id)
        chunk = DocumentChunk.objects.filter(document_version=version).first()
        self.assertIsNotNone(chunk)

        rerun = ProcessingJob.objects.create(document_version=version)
        scanner = "documents.processing.scanning.heuristic.HeuristicScanner"
        with override_settings(DOCUMENT_SCANNER=scanner):
            with self.assertRaises(DocumentInfected):
                run_pipeline(rerun.id)

        client = APIClient()
        client.force_authenticate(self.user)
        content = client.get(reverse("document-content", args=[document.id]))
        self.assertEqual(content.status_code, status.HTTP_404_NOT_FOUND)
        chunks = client.get(reverse("document-chunks", args=[document.id]))
        self.assertEqual(chunks.status_code, status.HTTP_404_NOT_FOUND)
        tables = client.get(reverse("document-tables", args=[document.id]))
        self.assertEqual(tables.json(), [])
        source = client.get(
            reverse("document-source", args=[document.id]), {"chunk": chunk.id}
        )
        self.assertEqual(source.status_code, status.HTTP_404_NOT_FOUND)
