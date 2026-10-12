"""Collections (spec 14, 15.2; FR-14): a collection narrows, never grants."""

from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Department, UserProfile
from accounts.services import DepartmentNameInUse, rename_department
from documents.models import AuditLog, Collection, Document, ProcessingJob
from documents.processing.chunking import service as chunking_service
from documents.processing.pipeline import run_pipeline
from documents.search import service as search_service
from documents.tests import MEDIA_ROOT, make_user

PROVIDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"
URL = "/api/v1/collections/"


def indexed_document(owner, title, text, department):
    document = Document.objects.create(
        title=title, document_type="POLICY", owner=owner, department=department
    )
    version = document.versions.create(
        version_number=1,
        file=ContentFile(text, name="doc.txt"),
        file_name="doc.txt",
        file_size=len(text),
        uploaded_by=owner,
    )
    document.current_version = version
    document.save(update_fields=["current_version"])
    run_pipeline(ProcessingJob.objects.create(document_version=version).id)
    return document


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=PROVIDER)
class CollectionTests(TestCase):
    def setUp(self):
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.faculty = make_user(
            "curator", UserProfile.Role.FACULTY, "Computer Science"
        )
        self.student = make_user("reader", UserProfile.Role.STUDENT, "Computer Science")
        self.outsider = make_user("outsider", UserProfile.Role.FACULTY, "Mechanical")
        self.attendance = indexed_document(
            self.faculty,
            "Attendance",
            b"Minimum attendance is 75 percent.\n",
            "Computer Science",
        )
        self.fees = indexed_document(
            self.faculty,
            "Fees",
            b"Attendance fines are charged per semester.\n",
            "Computer Science",
        )
        self.mech = indexed_document(
            self.outsider,
            "Mech",
            b"Attendance for workshops is mandatory.\n",
            "Mechanical",
        )
        self.client = APIClient()

    def as_user(self, user):
        self.client.force_authenticate(user=user)
        return self.client

    def make_collection(self, *documents, name="Rules"):
        response = self.as_user(self.faculty).post(URL, {"name": name}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        if documents:
            added = self.client.post(
                f"{URL}{response.data['id']}/documents/",
                {"document_ids": [d.id for d in documents]},
                format="json",
            )
            self.assertEqual(added.status_code, status.HTTP_200_OK, added.data)
        return Collection.objects.get(pk=response.data["id"])

    # --- Lifecycle and roles ---------------------------------------------

    def test_curator_creates_in_their_own_department(self):
        collection = self.make_collection(self.attendance)
        self.assertEqual(collection.department, "Computer Science")
        self.assertEqual(list(collection.documents.all()), [self.attendance])
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.COLLECTION_CREATE).exists()
        )

    def test_department_in_the_request_is_ignored_for_non_admins(self):
        response = self.as_user(self.faculty).post(
            URL, {"name": "X", "department": "Mechanical"}, format="json"
        )
        self.assertEqual(response.data["department"], "Computer Science")

    def test_student_cannot_create_but_can_see_and_use(self):
        collection = self.make_collection(self.attendance)
        client = self.as_user(self.student)
        self.assertEqual(
            client.post(URL, {"name": "Mine"}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        listed = client.get(URL).data["results"]
        self.assertEqual([c["id"] for c in listed], [collection.id])
        self.assertFalse(listed[0]["can_manage"])
        self.assertEqual(
            client.delete(f"{URL}{collection.id}/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_other_department_cannot_see_it(self):
        collection = self.make_collection(self.attendance)
        client = self.as_user(self.outsider)
        self.assertEqual(client.get(URL).data["results"], [])
        self.assertEqual(
            client.get(f"{URL}{collection.id}/").status_code, status.HTTP_404_NOT_FOUND
        )

    def test_duplicate_name_is_a_400_not_a_500(self):
        self.make_collection(name="Rules")
        response = self.client.post(URL, {"name": "Rules"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleting_a_collection_keeps_its_documents(self):
        collection = self.make_collection(self.attendance)
        self.assertEqual(
            self.client.delete(f"{URL}{collection.id}/").status_code,
            status.HTTP_204_NO_CONTENT,
        )
        self.assertTrue(Document.objects.filter(pk=self.attendance.pk).exists())

    def test_remove_documents(self):
        collection = self.make_collection(self.attendance, self.fees)
        response = self.client.delete(
            f"{URL}{collection.id}/documents/",
            {"document_ids": [self.fees.id]},
            format="json",
        )
        self.assertEqual(
            [d["id"] for d in response.data["documents"]], [self.attendance.id]
        )

    def test_another_curator_cannot_change_it_but_the_department_admin_can(self):
        collection = self.make_collection()
        colleague = make_user("colleague", UserProfile.Role.FACULTY, "Computer Science")
        refused = self.as_user(colleague).patch(
            f"{URL}{collection.id}/", {"name": "Mine now"}, format="json"
        )
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)

        head = make_user(
            "cs-head", UserProfile.Role.DEPARTMENT_ADMIN, "Computer Science"
        )
        allowed = self.as_user(head).patch(
            f"{URL}{collection.id}/", {"name": "Curated"}, format="json"
        )
        self.assertEqual(allowed.status_code, status.HTTP_200_OK, allowed.data)

    def test_patch_changes_the_details_and_nothing_else(self):
        collection = self.make_collection()
        response = self.as_user(self.faculty).patch(
            f"{URL}{collection.id}/",
            {"name": "Renamed", "department": "Mechanical"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        collection.refresh_from_db()
        self.assertEqual(collection.name, "Renamed")
        self.assertEqual(collection.department, "Computer Science")

    def test_patch_with_a_body_that_is_not_an_object_is_a_400(self):
        # It called .items() on whatever arrived: a list or a string was a 500.
        collection = self.make_collection()
        client = self.as_user(self.faculty)
        for body in ([], "x"):
            with self.subTest(body=body):
                response = client.patch(f"{URL}{collection.id}/", body, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- System administrators -------------------------------------------

    def test_a_system_admin_who_names_no_department_gets_their_own(self):
        # It used to store "": a collection no member could see, offering
        # every document in the institution and accepting none of them.
        admin = make_user("root", UserProfile.Role.SYSTEM_ADMIN, "Computer Science")
        created = self.as_user(admin).post(URL, {"name": "Admin set"}, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(created.data["department"], "Computer Science")
        added = self.client.post(
            f"{URL}{created.data['id']}/documents/",
            {"document_ids": [self.attendance.id]},
            format="json",
        )
        self.assertEqual(added.status_code, status.HTTP_200_OK, added.data)

    def test_a_system_admin_with_no_department_must_choose_one(self):
        admin = make_user("root", UserProfile.Role.SYSTEM_ADMIN, "")
        response = self.as_user(admin).post(URL, {"name": "Nowhere"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("department", response.data)
        self.assertFalse(Collection.objects.exists())

    def test_a_system_admin_may_choose_any_registered_department(self):
        Department.objects.get_or_create(name="Mechanical")
        admin = make_user("root", UserProfile.Role.SYSTEM_ADMIN, "")
        response = self.as_user(admin).post(
            URL, {"name": "Workshops", "department": "mechanical"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["department"], "Mechanical")

    # --- Department renames ----------------------------------------------

    def test_renaming_the_department_takes_its_collections_along(self):
        collection = self.make_collection(self.attendance)
        computer_science, _ = Department.objects.get_or_create(name="Computer Science")
        rename_department(computer_science, "Computing")

        collection.refresh_from_db()
        self.assertEqual(collection.department, "Computing")
        self.student.profile.refresh_from_db()
        listed = self.as_user(self.student).get(URL).data["results"]
        self.assertEqual([c["id"] for c in listed], [collection.id])
        self.assertEqual(listed[0]["document_count"], 1)

    def test_a_rename_onto_a_name_only_a_collection_still_uses_is_refused(self):
        Collection.objects.create(name="Old rules", department="Legacy")
        physics, _ = Department.objects.get_or_create(name="Physics")
        with self.assertRaises(DepartmentNameInUse):
            rename_department(physics, "Legacy")

    # --- Listing ---------------------------------------------------------

    def test_listing_costs_the_same_queries_for_one_collection_or_many(self):
        # Each row used to query its members again on top of a prefetch that
        # loaded every member's full row and was then thrown away.
        self.make_collection(self.attendance, name="First")
        with CaptureQueriesContext(connection) as one:
            self.as_user(self.student).get(URL)
        for number in range(4):
            self.make_collection(self.attendance, self.fees, name=f"More {number}")
        with CaptureQueriesContext(connection) as five:
            response = self.as_user(self.student).get(URL)
        self.assertEqual(len(response.data["results"]), 5)
        self.assertEqual(len(five), len(one))

    def test_members_are_only_those_the_caller_can_read(self):
        notes = Document.objects.create(
            title="Notes", owner=self.student, department="Computer Science"
        )
        collection = self.make_collection(self.attendance, notes)
        # The creator moves department: they still see the collection they
        # made, and the document they own, but no longer the student's.
        self.faculty.profile.department = "Mechanical"
        self.faculty.profile.save()
        response = self.as_user(self.faculty).get(f"{URL}{collection.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["documents"],
            [{"id": self.attendance.id, "title": "Attendance"}],
        )
        self.assertEqual(response.data["document_count"], 1)

    # --- The tenant boundary ---------------------------------------------

    def test_cannot_add_another_departments_document(self):
        collection = self.make_collection()
        response = self.client.post(
            f"{URL}{collection.id}/documents/",
            {"document_ids": [self.mech.id]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(collection.documents.exists())

    def test_system_admin_cannot_mix_departments_either(self):
        admin = make_user("root", UserProfile.Role.SYSTEM_ADMIN, "Computer Science")
        collection = self.make_collection()
        response = self.as_user(admin).post(
            f"{URL}{collection.id}/documents/",
            {"document_ids": [self.mech.id]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # --- Search and chat narrowing ---------------------------------------

    def test_search_narrows_to_the_collection(self):
        collection = self.make_collection(self.attendance)
        everything = search_service.search(self.faculty, "attendance", limit=10)
        narrowed = search_service.search(
            self.faculty, "attendance", limit=10, collection_id=collection.id
        )
        self.assertGreater(
            len({h.chunk.document_version.document_id for h in everything.hits}),
            len({h.chunk.document_version.document_id for h in narrowed.hits}),
        )
        self.assertEqual(
            {h.chunk.document_version.document_id for h in narrowed.hits},
            {self.attendance.id},
        )

    def test_an_invisible_collection_narrows_to_nothing(self):
        # The caller can read the member (they own it) but not the
        # collection, which is another department's. With an outsider who
        # could read neither, this passed whether or not the collection's
        # visibility was ever checked.
        owned = indexed_document(
            self.outsider,
            "Visitor attendance",
            b"Attendance rules for visiting students.\n",
            "Computer Science",
        )
        collection = self.make_collection(owned)
        everything = search_service.search(self.outsider, "attendance", limit=10)
        self.assertIn(
            owned.id, {h.chunk.document_version.document_id for h in everything.hits}
        )
        response = search_service.search(
            self.outsider, "attendance", limit=10, collection_id=collection.id
        )
        self.assertEqual(response.hits, [])

    def test_search_api_accepts_collection_id(self):
        collection = self.make_collection(self.fees)
        response = self.as_user(self.student).post(
            "/api/v1/search/",
            {"query": "attendance", "collection_id": collection.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {h["document_id"] for h in response.data["results"]}, {self.fees.id}
        )
