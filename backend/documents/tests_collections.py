"""Collections (spec 14, 15.2; FR-14): a collection narrows, never grants."""

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import UserProfile
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
        collection = self.make_collection(self.attendance)
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
