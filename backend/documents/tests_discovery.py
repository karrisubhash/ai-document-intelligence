"""Document discovery: search, filtering, ordering and pagination on
GET /api/documents/ (§16, §22).

Pagination has existed since M2; what did not was any way to narrow the
list before paging through it. At the corpus size this platform targets,
"page through everything by hand" is not discovery - see
documents.api.discovery for the implementation and README's "Document
discovery" section for the query-parameter contract.

The property every test file for this endpoint must protect, restated
because it is the one that matters most: authorization narrows the result
set BEFORE any filter, search term or ordering value is applied, and no
combination of those can widen it back out. Several tests below exist only
to prove that - not because a bug was found, but because a leak here would
not look like a bug. It would look like an empty result, indistinguishable
from "there was nothing to find."
"""

import shutil

from django.core.files.base import ContentFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import UserProfile
from documents.models import Document
from documents.tests import MEDIA_ROOT, make_user

LIST_URL = reverse("document-list")


def make_document(
    *,
    title,
    owner,
    department="",
    document_type=Document.DocumentType.GENERAL,
    doc_status=Document.Status.COMPLETED,
    created_at=None,
    updated_at=None,
    file_name="d.txt",
    version=True,
):
    """A document built directly through the ORM, not the upload API.

    Fast, and deliberately bypasses Celery/the pipeline - what these tests
    exercise is the LIST endpoint's query, not ingestion, which already has
    its own suite (tests_ingestion.py). Matches the pattern
    tests_scale.py's query-count tests already use for the same reason.
    """
    document = Document.objects.create(
        title=title,
        document_type=document_type,
        department=department,
        owner=owner,
        status=doc_status,
    )
    if version:
        current_version = document.versions.create(
            version_number=1,
            file=ContentFile(b"body", name=file_name),
            file_name=file_name,
            file_size=4,
            uploaded_by=owner,
        )
        document.current_version = current_version
        document.save(update_fields=["current_version"])

    if created_at is not None or updated_at is not None:
        # bulk_update / .update() bypass auto_now(_add), which is the point:
        # these tests need CONTROLLED timestamps to test ordering and date
        # filters, not "whenever the test happened to run".
        Document.objects.filter(id=document.id).update(
            created_at=created_at or document.created_at,
            updated_at=updated_at or document.updated_at,
        )
        document.refresh_from_db()

    return document


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class DiscoveryTestCase(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def get(self, **params):
        return self.client.get(LIST_URL, params)

    def titles(self, response):
        return [document["title"] for document in response.data["results"]]


class UnauthenticatedAccessTests(DiscoveryTestCase):
    """Discovery is not a lower-security surface than the rest of the API."""

    def test_listing_requires_authentication(self):
        response = self.get()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authentication_is_checked_before_query_parameters_are(self):
        """An invalid filter must not earn an anonymous caller a 400 - that
        would confirm the endpoint parses query parameters before it checks
        who is asking, one bit more than an anonymous caller should learn."""
        response = self.get(ordering="not-a-real-field", status="NOT_A_STATUS")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class AuthorizationScopingTests(DiscoveryTestCase):
    """Department isolation, owner visibility, and system-admin reach -
    exercised through every discovery feature, not only a plain list."""

    def setUp(self):
        super().setUp()
        self.cs_faculty = make_user(
            "cs_disc", UserProfile.Role.FACULTY, "Computer Science"
        )
        self.cs_colleague = make_user(
            "cs_disc2", UserProfile.Role.FACULTY, "Computer Science"
        )
        self.mech_faculty = make_user(
            "mech_disc", UserProfile.Role.FACULTY, "Mechanical Engineering"
        )
        self.system_admin = make_user("admin_disc", UserProfile.Role.SYSTEM_ADMIN, "")

        self.cs_doc = make_document(
            title="CS Attendance Policy",
            owner=self.cs_faculty,
            department="Computer Science",
        )
        self.mech_doc = make_document(
            title="Mech Attendance Policy",
            owner=self.mech_faculty,
            department="Mechanical Engineering",
        )

    def test_owner_sees_their_own_document(self):
        self.client.force_authenticate(self.cs_faculty)
        response = self.get()
        self.assertIn("CS Attendance Policy", self.titles(response))

    def test_department_colleague_sees_it_too(self):
        self.client.force_authenticate(self.cs_colleague)
        response = self.get()
        self.assertIn("CS Attendance Policy", self.titles(response))

    def test_other_department_does_not_see_it_in_a_plain_list(self):
        self.client.force_authenticate(self.mech_faculty)
        response = self.get()
        self.assertNotIn("CS Attendance Policy", self.titles(response))

    def test_system_admin_sees_every_department(self):
        self.client.force_authenticate(self.system_admin)
        response = self.get()
        titles = self.titles(response)
        self.assertIn("CS Attendance Policy", titles)
        self.assertIn("Mech Attendance Policy", titles)

    # --- The leak-prone paths: search and filters must not reach past
    #     the boundary the plain list already respects. -------------------

    def test_search_cannot_find_another_departments_document(self):
        self.client.force_authenticate(self.mech_faculty)
        response = self.get(search="Attendance")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("CS Attendance Policy", self.titles(response))
        self.assertIn("Mech Attendance Policy", self.titles(response))

    def test_department_filter_cannot_be_used_to_browse_another_department(self):
        """Filtering by a department the caller does not belong to must
        return the empty set it would without the filter - not an error,
        which would itself confirm the department exists and has documents
        the caller cannot otherwise see."""
        self.client.force_authenticate(self.mech_faculty)
        response = self.get(department="Computer Science")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])

    def test_owner_filter_cannot_reveal_a_strangers_documents(self):
        self.client.force_authenticate(self.mech_faculty)
        response = self.get(owner=str(self.cs_faculty.id))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_status_filter_never_widens_the_scope(self):
        """A filter is a narrowing operation. It must never be the thing
        that turns an authorization-scoped queryset back into the whole
        table."""
        self.client.force_authenticate(self.mech_faculty)
        response = self.get(status=Document.Status.COMPLETED)

        self.assertNotIn("CS Attendance Policy", self.titles(response))

    def test_a_wide_open_date_range_still_respects_department(self):
        self.client.force_authenticate(self.mech_faculty)
        response = self.get(created_after="2000-01-01")

        self.assertNotIn("CS Attendance Policy", self.titles(response))

    def test_no_count_based_leak_of_another_departments_size(self):
        """Section 14's rule applied to a number, not just to rows: a count
        that changed with how many documents exist elsewhere would let a
        caller infer another department's size without ever seeing a title."""
        self.client.force_authenticate(self.mech_faculty)
        before = self.get().data["count"]

        for i in range(5):
            make_document(
                title=f"Extra CS Doc {i}",
                owner=self.cs_faculty,
                department="Computer Science",
            )

        after = self.get().data["count"]
        self.assertEqual(before, after)


class SearchTests(DiscoveryTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("search_disc", UserProfile.Role.FACULTY, "Registry")
        self.client.force_authenticate(self.user)

        self.by_title = make_document(
            title="Grievance Procedure 2024", owner=self.user, department="Registry"
        )
        self.by_filename = make_document(
            title="Untitled Upload",
            owner=self.user,
            department="Registry",
            file_name="scholarship-rules.pdf",
        )
        self.unrelated = make_document(
            title="Library Rules",
            owner=self.user,
            department="Registry",
            file_name="library.txt",
        )

    def test_search_matches_the_title(self):
        response = self.get(search="grievance")
        self.assertEqual(self.titles(response), ["Grievance Procedure 2024"])

    def test_search_is_case_insensitive(self):
        response = self.get(search="GRIEVANCE")
        self.assertEqual(self.titles(response), ["Grievance Procedure 2024"])

    def test_search_matches_the_current_versions_file_name(self):
        """The title is 'Untitled Upload' - only the file name carries the
        term. A search that only looked at title would miss it."""
        response = self.get(search="scholarship")
        self.assertEqual(self.titles(response), ["Untitled Upload"])

    def test_a_document_with_no_current_version_is_still_searchable_by_title(self):
        """The file_name match is a LEFT JOIN precisely so a document that
        has not finished processing yet does not vanish from title search."""
        make_document(
            title="Grievance Draft, unprocessed",
            owner=self.user,
            department="Registry",
            version=False,
        )
        response = self.get(search="grievance")
        self.assertIn("Grievance Draft, unprocessed", self.titles(response))

    def test_no_match_is_an_empty_list_not_an_error(self):
        response = self.get(search="nothing-in-this-corpus")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_blank_search_is_the_same_as_no_search(self):
        with_blank = self.get(search="").data["count"]
        without = self.get().data["count"]
        self.assertEqual(with_blank, without)

    def test_search_narrows_rather_than_replaces_other_filters(self):
        make_document(
            title="Grievance Procedure - Archived",
            owner=self.user,
            department="Registry",
            doc_status=Document.Status.FAILED,
        )
        response = self.get(search="grievance", status=Document.Status.COMPLETED)
        self.assertEqual(self.titles(response), ["Grievance Procedure 2024"])


class ChoiceFilterTests(DiscoveryTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("filter_disc", UserProfile.Role.FACULTY, "Registry")
        self.client.force_authenticate(self.user)

        self.completed = make_document(
            title="Completed Doc",
            owner=self.user,
            department="Registry",
            doc_status=Document.Status.COMPLETED,
            document_type=Document.DocumentType.POLICY,
        )
        self.failed = make_document(
            title="Failed Doc",
            owner=self.user,
            department="Registry",
            doc_status=Document.Status.FAILED,
            document_type=Document.DocumentType.CIRCULAR,
        )

    def test_status_filter_selects_only_matching_documents(self):
        response = self.get(status=Document.Status.FAILED)
        self.assertEqual(self.titles(response), ["Failed Doc"])

    def test_an_invalid_status_is_a_400_naming_the_field(self):
        response = self.get(status="NOT_A_REAL_STATUS")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)

    def test_document_type_filter_selects_only_matching_documents(self):
        response = self.get(document_type=Document.DocumentType.CIRCULAR)
        self.assertEqual(self.titles(response), ["Failed Doc"])

    def test_an_invalid_document_type_is_a_400(self):
        response = self.get(document_type="NOT_A_REAL_TYPE")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document_type", response.data)

    def test_department_filter_selects_only_matching_documents(self):
        make_document(title="Other Dept Doc", owner=self.user, department="Other")
        response = self.get(department="Registry")
        self.assertCountEqual(self.titles(response), ["Completed Doc", "Failed Doc"])

    def test_combining_status_and_type_intersects_rather_than_unions(self):
        make_document(
            title="Completed Circular",
            owner=self.user,
            department="Registry",
            doc_status=Document.Status.COMPLETED,
            document_type=Document.DocumentType.CIRCULAR,
        )
        response = self.get(
            status=Document.Status.COMPLETED,
            document_type=Document.DocumentType.CIRCULAR,
        )
        self.assertEqual(self.titles(response), ["Completed Circular"])


class OwnerFilterTests(DiscoveryTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("owner_disc", UserProfile.Role.FACULTY, "Registry")
        self.colleague = make_user("owner_disc2", UserProfile.Role.FACULTY, "Registry")
        self.client.force_authenticate(self.user)

        self.mine = make_document(title="Mine", owner=self.user, department="Registry")
        self.theirs = make_document(
            title="Theirs", owner=self.colleague, department="Registry"
        )

    def test_owner_me_returns_only_my_documents(self):
        response = self.get(owner="me")
        self.assertEqual(self.titles(response), ["Mine"])

    def test_owner_by_id_is_accepted(self):
        response = self.get(owner=str(self.colleague.id))
        self.assertEqual(self.titles(response), ["Theirs"])

    def test_a_non_integer_owner_is_a_400(self):
        response = self.get(owner="not-an-id")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("owner", response.data)

    def test_a_negative_owner_id_is_a_400(self):
        response = self.get(owner="-1")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_nonexistent_owner_id_is_an_empty_list_not_an_error(self):
        response = self.get(owner="999999999")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_an_owner_value_int_cannot_parse_is_a_400_not_a_500(self):
        """Each of these passed the old `lstrip("-").isdigit()` check and
        then crashed int(): a doubled sign, a superscript digit, and a
        number past Python's 4,300-digit conversion limit."""
        for value in ("--5", "²", "9" * 4301):
            with self.subTest(value=value[:10]):
                response = self.get(owner=value)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("owner", response.data)


class DateRangeFilterTests(DiscoveryTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("date_disc", UserProfile.Role.FACULTY, "Registry")
        self.client.force_authenticate(self.user)

        january = timezone.datetime(
            2025, 1, 15, 12, 0, tzinfo=timezone.get_current_timezone()
        )
        february = timezone.datetime(
            2025, 2, 15, 12, 0, tzinfo=timezone.get_current_timezone()
        )

        # updated_at is pinned here too, not left at real creation time -
        # otherwise it defaults to whenever the test actually ran (e.g.
        # 2026), which trivially satisfies any 2025 updated_after filter
        # and made test_updated_range_filters_independently_of_created_range
        # see BOTH documents instead of the one it pinned forward.
        self.jan_doc = make_document(
            title="January Doc",
            owner=self.user,
            department="Registry",
            created_at=january,
            updated_at=january,
        )
        self.feb_doc = make_document(
            title="February Doc",
            owner=self.user,
            department="Registry",
            created_at=february,
            updated_at=february,
        )

    def test_created_after_a_bare_date_includes_documents_from_that_whole_day(self):
        response = self.get(created_after="2025-02-01")
        self.assertEqual(self.titles(response), ["February Doc"])

    def test_created_before_a_bare_date_is_inclusive_of_the_named_day(self):
        """A document created ON 2025-01-15 must still be included when
        `created_before=2025-01-15` - the day named is inside the range,
        not its exclusive edge."""
        response = self.get(created_before="2025-01-15")
        self.assertEqual(self.titles(response), ["January Doc"])

    def test_a_range_spanning_both_documents(self):
        response = self.get(created_after="2025-01-01", created_before="2025-02-28")
        self.assertCountEqual(self.titles(response), ["January Doc", "February Doc"])

    def test_a_range_excluding_both_documents(self):
        response = self.get(created_after="2025-03-01")
        self.assertEqual(response.data["count"], 0)

    def test_a_full_iso_datetime_is_accepted(self):
        response = self.get(created_after="2025-02-15T00:00:00Z")
        self.assertEqual(self.titles(response), ["February Doc"])

    def test_an_unparseable_date_is_a_400(self):
        response = self.get(created_after="not-a-date")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("created_after", response.data)

    def test_a_well_formed_but_impossible_date_is_a_400_not_a_500(self):
        """Django's parsers return None for a malformed value but RAISE for
        a well-formed impossible one - the 30th of February, hour 25."""
        for field, value in (
            ("created_after", "2025-02-30"),
            ("created_before", "2025-01-31T25:00:00"),
            ("updated_after", "0000-01-01"),
        ):
            with self.subTest(field=field, value=value):
                response = self.get(**{field: value})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)

    def test_created_before_earlier_than_created_after_is_a_400(self):
        """A reversed range cannot be satisfied and is caught here, rather
        than silently returned as an empty list that looks like 'no
        documents in that period' when really the period is inside out."""
        response = self.get(created_after="2025-02-01", created_before="2025-01-01")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("created_before", response.data)

    def test_updated_range_filters_independently_of_created_range(self):
        Document.objects.filter(id=self.jan_doc.id).update(
            updated_at=timezone.datetime(
                2025, 6, 1, tzinfo=timezone.get_current_timezone()
            )
        )
        response = self.get(updated_after="2025-05-01")
        self.assertEqual(self.titles(response), ["January Doc"])


class ParseBoundaryUnitTests(APITestCase):
    """documents.api.discovery.parse_boundary directly.

    Regression coverage for a bug the API-level DateRangeFilterTests found:
    a bare date given as a `_before` bound resolved to midnight (the START
    of the day) rather than its last instant, because parse_datetime()
    accepts a bare date too and was tried first.
    """

    def test_a_bare_date_as_an_after_bound_is_its_first_instant(self):
        from datetime import time

        from documents.api import discovery

        boundary = discovery.parse_boundary("2025-01-15", "x", end_of_day=False)
        self.assertEqual(boundary.time(), time.min)

    def test_a_bare_date_as_a_before_bound_is_its_last_instant(self):
        from datetime import time

        from documents.api import discovery

        boundary = discovery.parse_boundary("2025-01-15", "x", end_of_day=True)
        self.assertEqual(boundary.time(), time.max)

    def test_a_full_datetime_is_returned_exactly_regardless_of_end_of_day(self):
        from documents.api import discovery

        after = discovery.parse_boundary("2025-01-15T09:30:00Z", "x", end_of_day=False)
        before = discovery.parse_boundary("2025-01-15T09:30:00Z", "x", end_of_day=True)
        self.assertEqual(after, before)
        self.assertEqual(after.hour, 9)
        self.assertEqual(after.minute, 30)


class OrderingTests(DiscoveryTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("order_disc", UserProfile.Role.FACULTY, "Registry")
        self.client.force_authenticate(self.user)

        self.a_doc = make_document(
            title="Alpha", owner=self.user, department="Registry"
        )
        self.b_doc = make_document(
            title="Bravo", owner=self.user, department="Registry"
        )
        self.c_doc = make_document(
            title="Charlie", owner=self.user, department="Registry"
        )

    def test_ordering_by_title_ascending(self):
        response = self.get(ordering="title")
        self.assertEqual(self.titles(response), ["Alpha", "Bravo", "Charlie"])

    def test_ordering_by_title_descending(self):
        response = self.get(ordering="-title")
        self.assertEqual(self.titles(response), ["Charlie", "Bravo", "Alpha"])

    def test_default_ordering_is_unchanged_newest_first(self):
        """The existing contract - no `ordering` given behaves exactly as
        it always has."""
        response = self.get()
        ids = [document["id"] for document in response.data["results"]]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_an_unknown_ordering_field_is_a_400_naming_the_choices(self):
        response = self.get(ordering="not_a_real_field")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ordering", response.data)
        self.assertIn("created_at", str(response.data))

    def test_an_ordering_field_that_names_a_relation_is_refused(self):
        """The allowlist, not the ORM, decides what is sortable - a related
        lookup or a dunder must never reach .order_by() as a raw string."""
        response = self.get(ordering="owner__profile__department")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_multiple_ordering_fields_are_applied_in_order(self):
        make_document(
            title="Alpha",
            owner=self.user,
            department="Registry",
            doc_status=Document.Status.FAILED,
        )
        response = self.get(ordering="title,-status")
        titles_and_status = [
            (document["title"], document["status"])
            for document in response.data["results"]
        ]
        # The two Alphas are ordered by the SECOND key, status descending.
        # Asserting only the first title could not fail if that key were
        # dropped: the id tiebreak alone puts the older, COMPLETED one first.
        self.assertEqual(
            titles_and_status[:2],
            [
                ("Alpha", Document.Status.FAILED),
                ("Alpha", Document.Status.COMPLETED),
            ],
        )

    def test_too_many_ordering_fields_is_a_400(self):
        response = self.get(ordering="title,status,document_type,created_at")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_ordering_is_deterministic_across_repeated_requests(self):
        """Same query, same page, fetched twice - the tiebreak on `id` is
        what makes this guaranteed rather than coincidental."""
        first = self.get(ordering="status").data["results"]
        second = self.get(ordering="status").data["results"]
        self.assertEqual([d["id"] for d in first], [d["id"] for d in second])

    def test_ties_on_the_ordering_field_are_broken_by_id(self):
        """All three documents share a status (COMPLETED, the default), so
        `ordering=status` alone ties completely - the appended id tiebreak
        is the only thing giving a defined order."""
        response = self.get(ordering="status")
        ids = [document["id"] for document in response.data["results"]]
        self.assertEqual(ids, sorted(ids))


class PaginationTests(DiscoveryTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("page_disc", UserProfile.Role.FACULTY, "Registry")
        self.client.force_authenticate(self.user)

        for i in range(55):
            make_document(title=f"Doc {i:03d}", owner=self.user, department="Registry")

    def test_default_page_size_is_unchanged(self):
        response = self.get()
        self.assertEqual(len(response.data["results"]), 50)
        self.assertEqual(response.data["count"], 55)
        self.assertIsNotNone(response.data["next"])
        self.assertIsNone(response.data["previous"])

    def test_second_page_holds_the_remainder(self):
        response = self.get(page=2)
        self.assertEqual(len(response.data["results"]), 5)
        self.assertIsNone(response.data["next"])

    def test_pagination_composes_with_a_filter(self):
        """count reflects the FILTERED set, not the whole table - a caller
        paging through search results must see the right total."""
        make_document(title="Findable Needle", owner=self.user, department="Registry")

        response = self.get(search="Needle")
        self.assertEqual(response.data["count"], 1)
        self.assertIsNone(response.data["next"])

    def test_no_document_appears_on_two_pages(self):
        first_page = {d["id"] for d in self.get(page=1).data["results"]}
        second_page = {d["id"] for d in self.get(page=2).data["results"]}
        self.assertEqual(first_page & second_page, set())
        self.assertEqual(len(first_page) + len(second_page), 55)


class EmptyResultTests(DiscoveryTestCase):
    def test_a_user_with_no_documents_gets_an_empty_list_not_an_error(self):
        user = make_user("empty_disc", UserProfile.Role.FACULTY, "Nobody Here")
        self.client.force_authenticate(user)

        response = self.get()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])
        self.assertIsNone(response.data["next"])
        self.assertIsNone(response.data["previous"])

    def test_an_empty_result_still_reports_valid_pagination_shape(self):
        user = make_user("empty_disc2", UserProfile.Role.FACULTY, "Nobody Here 2")
        self.client.force_authenticate(user)

        response = self.get(status=Document.Status.FAILED)
        self.assertEqual(
            set(response.data.keys()), {"count", "next", "previous", "results"}
        )


class ResponseShapeTests(DiscoveryTestCase):
    """§16: a discovery listing exposes only what discovery needs."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shape_disc", UserProfile.Role.FACULTY, "Registry")
        self.client.force_authenticate(self.user)
        self.document = make_document(
            title="Shape Probe",
            owner=self.user,
            department="Registry",
            file_name="shape.txt",
        )

    def test_list_response_carries_the_documented_top_level_fields(self):
        response = self.get()
        row = response.data["results"][0]
        self.assertEqual(
            set(row.keys()),
            {
                "id",
                "title",
                "document_type",
                "department",
                "owner",
                # Added with archival and the product frontend: who owns it,
                # readably, and whether it is archived. Additive - every
                # field a consumer already read is unchanged.
                "owner_username",
                "status",
                "current_version",
                "is_archived",
                "archived_at",
                "created_at",
                "updated_at",
            },
        )

    def test_list_response_does_not_expose_the_storage_key_or_checksum(self):
        """The one deliberate difference from the detail endpoint's shape -
        see DocumentListSerializer and README, 'Document discovery'."""
        response = self.get()
        version = response.data["results"][0]["current_version"]
        self.assertNotIn("file", version)
        self.assertNotIn("checksum", version)
        self.assertEqual(version["file_name"], "shape.txt")

    def test_the_detail_endpoint_is_unchanged(self):
        """Backward compatibility: only the LIST action's shape changed."""
        response = self.client.get(reverse("document-detail", args=[self.document.id]))
        version = response.data["current_version"]
        self.assertIn("file", version)
        self.assertIn("checksum", version)


class QueryEfficiencyTests(DiscoveryTestCase):
    """Discovery must not reintroduce the N+1 tests_scale.py already fixed,
    and must not add a new one of its own."""

    def setUp(self):
        super().setUp()
        self.user = make_user("perf_disc", UserProfile.Role.FACULTY, "Registry")
        self.client.force_authenticate(self.user)

    def add_documents(self, count, **kwargs):
        for i in range(count):
            make_document(
                title=f"Perf {i}", owner=self.user, department="Registry", **kwargs
            )

    def queries_for(self, **params):
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(LIST_URL, params)
        return len(ctx.captured_queries)

    def test_search_does_not_scale_query_count_with_the_corpus(self):
        self.add_documents(3)
        small = self.queries_for(search="Perf")

        self.add_documents(9)
        large = self.queries_for(search="Perf")

        self.assertEqual(small, large)

    def test_filters_and_ordering_do_not_scale_query_count_with_the_corpus(self):
        self.add_documents(3)
        small = self.queries_for(status=Document.Status.COMPLETED, ordering="-title")

        self.add_documents(9)
        large = self.queries_for(status=Document.Status.COMPLETED, ordering="-title")

        self.assertEqual(small, large)

    def test_a_full_discovery_query_stays_in_single_digits(self):
        self.add_documents(12)
        count = self.queries_for(
            search="Perf", status=Document.Status.COMPLETED, ordering="-created_at"
        )
        self.assertLess(count, 10)


class CombinedQueryTests(DiscoveryTestCase):
    """Everything at once - the shape a real product filter panel sends."""

    def setUp(self):
        super().setUp()
        self.user = make_user("combo_disc", UserProfile.Role.FACULTY, "Registry")
        self.client.force_authenticate(self.user)

        self.target = make_document(
            title="Attendance Circular 2025",
            owner=self.user,
            department="Registry",
            document_type=Document.DocumentType.CIRCULAR,
            doc_status=Document.Status.COMPLETED,
        )
        make_document(
            title="Attendance Circular Archived",
            owner=self.user,
            department="Registry",
            document_type=Document.DocumentType.CIRCULAR,
            doc_status=Document.Status.FAILED,
        )
        make_document(
            title="Unrelated Policy",
            owner=self.user,
            department="Registry",
            document_type=Document.DocumentType.POLICY,
            doc_status=Document.Status.COMPLETED,
        )

    def test_search_plus_type_plus_status_plus_ordering(self):
        response = self.get(
            search="Attendance",
            document_type=Document.DocumentType.CIRCULAR,
            status=Document.Status.COMPLETED,
            ordering="title",
        )
        self.assertEqual(self.titles(response), ["Attendance Circular 2025"])

    def test_an_over_narrow_combination_is_an_empty_list(self):
        response = self.get(
            search="Attendance",
            document_type=Document.DocumentType.POLICY,
        )
        self.assertEqual(response.data["count"], 0)
