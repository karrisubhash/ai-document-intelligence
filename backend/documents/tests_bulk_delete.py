"""Bulk delete: POST .../bulk-delete/ for documents, collections and chat
conversations - deleting selected rows, or all of them, under the same
access rules and audit trail as deleting one."""

from datetime import datetime, timedelta
from unittest import mock

from django.core.cache import cache
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from accounts.models import UserProfile
from documents import authorization, services
from documents.api import views
from documents.chat import views as chat_views
from documents.collections import views as collection_views
from documents.models import (
    AuditLog,
    Collection,
    Conversation,
    Document,
    DocumentVersion,
    Message,
)
from documents.tests import MEDIA_ROOT, make_txt_file, make_user

Role = UserProfile.Role
URL = reverse("document-bulk-delete")


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def document(owner, title="Policy", department=None, **fields):
    return Document.objects.create(
        title=title,
        owner=owner,
        department=owner.profile.department if department is None else department,
        **fields,
    )


class BulkDeleteTestCase(TestCase):
    def setUp(self):
        self.root = make_user("bulk_root", Role.SYSTEM_ADMIN, "")
        self.physics_admin = make_user(
            "bulk_phy_admin", Role.DEPARTMENT_ADMIN, "Physics"
        )
        self.alice = make_user("bulk_alice", Role.FACULTY, "Physics")
        self.bob = make_user("bulk_bob", Role.FACULTY, "Physics")
        self.carol = make_user("bulk_carol", Role.FACULTY, "Chemistry")

    def post(self, user, body):
        return client_for(user).post(URL, body, format="json")


class SelectedIdsTests(BulkDeleteTestCase):
    def test_an_owner_deletes_the_documents_they_selected(self):
        first, second = document(self.alice, "One"), document(self.alice, "Two")
        kept = document(self.alice, "Kept")

        response = self.post(self.alice, {"ids": [first.id, second.id]})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "deleted": [first.id, second.id],
                "forbidden": [],
                "not_found": [],
                "failed": [],
            },
        )
        self.assertEqual(list(Document.objects.values_list("id", flat=True)), [kept.id])

    def test_each_delete_is_audited_like_a_single_delete(self):
        target = document(self.alice, "Salary Revisions")

        self.post(self.alice, {"ids": [target.id]})

        row = AuditLog.objects.get(action=AuditLog.Action.DOCUMENT_DELETE)
        self.assertEqual(row.result, AuditLog.Result.ALLOWED)
        self.assertEqual(row.user, self.alice)
        self.assertEqual(row.document_ref, target.id)
        self.assertEqual(row.document_department, "Physics")
        self.assertEqual(
            row.metadata, {"document_title": "Salary Revisions", "bulk": True}
        )

    def test_what_the_caller_may_not_delete_is_refused_and_left_alone(self):
        mine = document(self.alice, "Mine")
        colleagues = document(self.bob, "Bob's")  # visible, not manageable
        outsiders = document(self.carol, "Carol's")  # not even visible

        response = self.post(
            self.alice, {"ids": [mine.id, colleagues.id, outsiders.id, 999999]}
        )

        self.assertEqual(response.data["deleted"], [mine.id])
        self.assertEqual(response.data["forbidden"], [colleagues.id])
        # Another department's document is indistinguishable from an id
        # that does not exist - the same rule as a single DELETE's 404.
        self.assertEqual(response.data["not_found"], [outsiders.id, 999999])
        self.assertTrue(Document.objects.filter(pk=colleagues.id).exists())
        self.assertTrue(Document.objects.filter(pk=outsiders.id).exists())

    def test_every_refusal_of_a_real_document_is_audited_as_denied(self):
        colleagues, outsiders = document(self.bob), document(self.carol)

        self.post(self.alice, {"ids": [colleagues.id, outsiders.id, 999999]})

        denied = AuditLog.objects.filter(
            action=AuditLog.Action.DOCUMENT_DELETE, result=AuditLog.Result.DENIED
        )
        self.assertEqual(
            sorted(denied.values_list("document_id", flat=True)),
            [colleagues.id, outsiders.id],
        )
        self.assertTrue(all(row.user == self.alice for row in denied))

    def test_a_department_admin_deletes_their_departments_documents(self):
        theirs = document(self.alice)
        chemistry = document(self.carol)

        response = self.post(self.physics_admin, {"ids": [theirs.id, chemistry.id]})

        self.assertEqual(response.data["deleted"], [theirs.id])
        self.assertEqual(response.data["not_found"], [chemistry.id])

    def test_a_system_admin_deletes_anything(self):
        docs = [document(self.alice), document(self.carol)]

        response = self.post(self.root, {"ids": [d.id for d in docs]})

        self.assertEqual(response.data["deleted"], [d.id for d in docs])
        self.assertFalse(Document.objects.exists())

    def test_a_repeated_id_is_deleted_once(self):
        target = document(self.alice)

        response = self.post(self.alice, {"ids": [target.id, target.id]})

        self.assertEqual(response.data["deleted"], [target.id])
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.DOCUMENT_DELETE).count(), 1
        )

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_the_versions_and_their_files_go_with_the_document(self):
        target = document(self.alice)
        with mock.patch.object(services.process_document_task, "delay"):
            version = services.create_document_version(
                target, make_txt_file(), self.alice
            )
        storage, name = version.file.storage, version.file.name
        self.assertTrue(storage.exists(name))

        with self.captureOnCommitCallbacks(execute=True):
            self.post(self.alice, {"ids": [target.id]})

        self.assertFalse(DocumentVersion.objects.filter(pk=version.pk).exists())
        self.assertFalse(storage.exists(name))


class AllMatchingTests(BulkDeleteTestCase):
    def test_all_deletes_what_the_listing_filters_match_and_nothing_else(self):
        policy = document(self.alice, "Leave Policy")
        other = document(self.alice, "Exam Timetable")

        response = self.post(self.alice, {"all": True, "filters": {"search": "policy"}})

        self.assertEqual(response.data["deleted"], [policy.id])
        self.assertEqual(response.data["remaining"], 0)
        self.assertEqual(response.data["skipped"], 0)
        self.assertTrue(Document.objects.filter(pk=other.id).exists())

    def test_all_takes_only_what_the_caller_may_delete_and_counts_the_rest(self):
        mine = document(self.alice, "Mine")
        colleagues = document(self.bob, "Bob's")
        outsiders = document(self.carol, "Carol's")

        response = self.post(self.alice, {"all": True})

        self.assertEqual(response.data["deleted"], [mine.id])
        self.assertEqual(response.data["forbidden"], [])
        self.assertEqual(response.data["remaining"], 0)
        # Bob's is in Alice's listing but not hers to delete; Carol's was
        # never in her listing, so it is not counted at all.
        self.assertEqual(response.data["skipped"], 1)
        self.assertTrue(Document.objects.filter(pk=colleagues.id).exists())
        self.assertTrue(Document.objects.filter(pk=outsiders.id).exists())
        # Only targeted documents are audited; a skipped one is not refused.
        self.assertFalse(
            AuditLog.objects.filter(result=AuditLog.Result.DENIED).exists()
        )

    def test_all_follows_the_archive_filter_like_the_listing(self):
        active = document(self.alice, "Active")
        archived = document(self.alice, "Archived", archived_at=timezone.now())

        response = self.post(self.alice, {"all": True})
        self.assertEqual(response.data["deleted"], [active.id])
        self.assertTrue(Document.objects.filter(pk=archived.id).exists())

        response = self.post(self.alice, {"all": True, "filters": {"archived": "all"}})
        self.assertEqual(response.data["deleted"], [archived.id])

    def test_all_works_through_a_large_set_in_bounded_requests(self):
        docs = [document(self.alice, f"Doc {n}") for n in range(5)]

        deleted, remaining = [], []
        with mock.patch.object(views, "BULK_DELETE_LIMIT", 2):
            # Bounded: a `remaining` that never reaches 0 fails, not hangs.
            for _ in range(3):
                response = self.post(self.alice, {"all": True})
                deleted += response.data["deleted"]
                remaining.append(response.data["remaining"])

        self.assertEqual(remaining, [3, 1, 0])
        self.assertEqual(sorted(deleted), [d.id for d in docs])
        self.assertFalse(Document.objects.exists())

    def test_preview_counts_without_deleting(self):
        mine, colleagues = document(self.alice, "Mine"), document(self.bob, "Bob's")

        response = self.post(self.alice, {"all": True, "preview": True})

        self.assertEqual(response.data["deleted"], [])
        self.assertEqual(response.data["remaining"], 1)
        self.assertEqual(response.data["skipped"], 1)
        self.assertIn("as_of", response.data)
        self.assertEqual(
            Document.objects.filter(pk__in=[mine.id, colleagues.id]).count(), 2
        )

    def test_documents_created_after_as_of_are_left_alone(self):
        """The snapshot a confirmation was given for: a colleague's upload
        landing between the preview and the delete is not swept up."""
        early = document(self.alice, "Early")
        as_of = timezone.now()
        late = document(self.alice, "Late")
        Document.objects.filter(pk=late.pk).update(
            created_at=as_of + timedelta(seconds=1)
        )

        response = self.post(self.alice, {"all": True, "as_of": as_of.isoformat()})

        self.assertEqual(response.data["deleted"], [early.id])
        self.assertEqual(response.data["remaining"], 0)
        self.assertTrue(Document.objects.filter(pk=late.id).exists())

    def test_the_snapshot_round_trips_as_the_client_sends_it_back(self):
        """The rendered `as_of` - not the datetime in response.data - is what
        a client has; it must parse back into the same snapshot."""
        early = document(self.alice, "Early")
        before = timezone.now()

        preview = self.post(self.alice, {"all": True, "preview": True}).json()
        as_of = datetime.fromisoformat(preview["as_of"].replace("Z", "+00:00"))
        self.assertGreaterEqual(as_of, before)
        late = document(self.alice, "Late")
        Document.objects.filter(pk=late.pk).update(
            created_at=as_of + timedelta(seconds=1)
        )

        response = self.post(self.alice, {"all": True, "as_of": preview["as_of"]})

        self.assertEqual(response.data["deleted"], [early.id])
        self.assertTrue(Document.objects.filter(pk=late.id).exists())

    def test_a_document_that_fails_does_not_lose_the_rest_of_the_batch(self):
        first, broken, last = (
            document(self.alice),
            document(self.alice),
            document(self.alice),
        )
        real = views._bulk_delete_one

        def flaky(user, pk):
            if pk == broken.id:
                raise DatabaseError("simulated")
            return real(user, pk)

        with (
            mock.patch.object(views, "_bulk_delete_one", side_effect=flaky),
            self.assertLogs("adi.documents", "WARNING"),
        ):
            response = self.post(self.alice, {"ids": [first.id, broken.id, last.id]})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["deleted"], [first.id, last.id])
        self.assertEqual(response.data["failed"], [broken.id])
        self.assertTrue(Document.objects.filter(pk=broken.id).exists())

    def test_bulk_delete_has_a_rate_limit_of_its_own(self):
        cache.clear()
        self.addCleanup(cache.clear)
        targets = [document(self.alice), document(self.alice)]

        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, {"bulk_delete": "1/hour"}
        ):
            first = self.post(self.alice, {"ids": [targets[0].id]})
            second = self.post(self.alice, {"ids": [targets[1].id]})

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertTrue(Document.objects.filter(pk=targets[1].id).exists())

    def test_only_mine_narrows_a_department_admins_all(self):
        own = document(self.physics_admin, "Own")
        members = document(self.alice, "Member's")

        response = self.post(
            self.physics_admin, {"all": True, "filters": {"owner": "me"}}
        )

        self.assertEqual(response.data["deleted"], [own.id])
        self.assertTrue(Document.objects.filter(pk=members.id).exists())


class RequestValidationTests(BulkDeleteTestCase):
    def assertRefused(self, body, field=None):
        target = document(self.alice)
        response = self.post(self.alice, body)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)
        if field:
            self.assertIn(field, response.data)
        self.assertTrue(Document.objects.filter(pk=target.id).exists())

    def test_ids_and_all_are_mutually_exclusive(self):
        self.assertRefused({"ids": [1], "all": True})

    def test_an_empty_request_deletes_nothing(self):
        self.assertRefused({})
        self.assertRefused({"ids": []}, "ids")
        self.assertRefused({"all": False})

    def test_ids_are_bounded(self):
        self.assertRefused({"ids": list(range(1, 102))}, "ids")

    def test_ids_must_be_positive_integers(self):
        self.assertRefused({"ids": ["x"]}, "ids")
        self.assertRefused({"ids": [0]}, "ids")

    def test_filters_are_validated_as_the_listing_validates_them(self):
        self.assertRefused({"all": True, "filters": {"status": "NOPE"}}, "filters")
        self.assertRefused({"all": True, "filters": {"owner": "x"}}, "filters")

    def test_filters_only_apply_to_all(self):
        self.assertRefused({"ids": [1], "filters": {"search": "x"}}, "filters")
        self.assertRefused({"ids": [1], "preview": True}, "preview")
        self.assertRefused({"ids": [1], "as_of": "2026-01-01T00:00:00Z"}, "as_of")

    def test_an_unknown_filter_is_refused_not_ignored(self):
        """Ignored, `q` (the web app's own URL name for search) would leave
        no filter at all - every document the caller may delete."""
        self.assertRefused({"all": True, "filters": {"q": "draft"}}, "filters")
        self.assertRefused({"all": True, "filters": {"page": 2}}, "filters")

    def test_an_unknown_field_is_refused_not_ignored(self):
        self.assertRefused({"all": True, "filter": {"search": "draft"}}, "filter")

    def test_a_signed_out_caller_is_refused(self):
        target = document(self.alice)
        response = APIClient().post(URL, {"ids": [target.id]}, format="json")
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        self.assertTrue(Document.objects.filter(pk=target.id).exists())


class ManageableDocumentsTests(BulkDeleteTestCase):
    def test_the_queryset_scope_agrees_with_the_per_document_rule(self):
        no_department_admin = make_user("bulk_nodept_admin", Role.DEPARTMENT_ADMIN, "")
        users = [
            self.root,
            self.physics_admin,
            self.alice,
            self.bob,
            self.carol,
            no_department_admin,
        ]
        docs = [
            document(self.alice),
            document(self.carol),
            document(no_department_admin),
            document(self.bob, department=""),
        ]

        for user in users:
            expected = {
                d.id for d in docs if authorization.can_manage_document(user, d)
            }
            scoped = authorization.manageable_documents(user, Document.objects.all())
            self.assertEqual(
                set(scoped.values_list("id", flat=True)), expected, user.username
            )

    def test_listing_rows_say_whether_the_caller_may_manage_them(self):
        mine, colleagues = document(self.alice), document(self.bob)

        response = client_for(self.alice).get(reverse("document-list"))

        flags = {row["id"]: row["can_manage"] for row in response.data["results"]}
        self.assertEqual(flags, {mine.id: True, colleagues.id: False})


class CollectionBulkDeleteTests(BulkDeleteTestCase):
    url = reverse("collection-bulk-delete")

    def collection(self, name, creator, department=None):
        return Collection.objects.create(
            name=name,
            created_by=creator,
            department=creator.profile.department if department is None else department,
        )

    def test_selected_collections_go_and_their_documents_stay(self):
        member = document(self.alice, "Fee schedule")
        first = self.collection("Exam rules", self.alice)
        first.documents.add(member)
        second, kept = (
            self.collection("Fees", self.alice),
            self.collection("Kept", self.alice),
        )

        response = client_for(self.alice).post(
            self.url, {"ids": [first.id, second.id]}, format="json"
        )

        self.assertEqual(
            response.data,
            {"deleted": [first.id, second.id], "forbidden": [], "not_found": []},
        )
        self.assertEqual(
            list(Collection.objects.values_list("id", flat=True)), [kept.id]
        )
        self.assertTrue(Document.objects.filter(pk=member.id).exists())

    def test_each_delete_is_audited_like_a_single_delete(self):
        target = self.collection("Exam rules", self.alice)

        client_for(self.alice).post(self.url, {"ids": [target.id]}, format="json")

        row = AuditLog.objects.get(action=AuditLog.Action.COLLECTION_DELETE)
        self.assertEqual(row.user, self.alice)
        self.assertEqual(row.document_department, "Physics")
        self.assertEqual(row.metadata["collection_id"], target.id)
        self.assertTrue(row.metadata["bulk"])

    def test_what_the_caller_may_not_delete_is_refused_and_left_alone(self):
        mine = self.collection("Mine", self.alice)
        colleagues = self.collection("Bob's", self.bob)  # visible, not manageable
        outsiders = self.collection("Carol's", self.carol)  # not visible

        response = client_for(self.alice).post(
            self.url,
            {"ids": [mine.id, colleagues.id, outsiders.id, 999999]},
            format="json",
        )

        self.assertEqual(response.data["deleted"], [mine.id])
        self.assertEqual(response.data["forbidden"], [colleagues.id])
        self.assertEqual(response.data["not_found"], [outsiders.id, 999999])
        self.assertEqual(Collection.objects.count(), 2)

    def test_all_deletes_what_the_caller_manages_and_counts_the_rest(self):
        mine = self.collection("Mine", self.alice)
        colleagues = self.collection("Bob's", self.bob)
        outsiders = self.collection("Carol's", self.carol)

        response = client_for(self.alice).post(self.url, {"all": True}, format="json")

        self.assertEqual(response.data["deleted"], [mine.id])
        self.assertEqual(response.data["forbidden"], [])
        self.assertEqual(response.data["skipped"], 1)
        self.assertEqual(
            set(Collection.objects.values_list("id", flat=True)),
            {colleagues.id, outsiders.id},
        )

    def test_a_department_admin_deletes_their_departments_collections(self):
        members = self.collection("Bob's", self.bob)
        response = client_for(self.physics_admin).post(
            self.url, {"all": True}, format="json"
        )
        self.assertEqual(response.data["deleted"], [members.id])

    def test_the_request_is_validated(self):
        client = client_for(self.alice)
        for body in ({}, {"ids": []}, {"ids": [1], "all": True}):
            response = client.post(self.url, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)

    def test_filters_are_refused_here_not_ignored(self):
        """Collections take no filters; ignored, a 'delete all in X' would
        have deleted every collection the caller manages."""
        target = self.collection("Mine", self.alice)

        response = client_for(self.alice).post(
            self.url, {"all": True, "filters": {"department": "X"}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("filters", response.data)
        self.assertTrue(Collection.objects.filter(pk=target.id).exists())

    def test_all_works_through_a_large_set_in_bounded_requests(self):
        mine = [self.collection(f"Mine {n}", self.alice) for n in range(3)]
        theirs = self.collection("Bob's", self.bob)

        deleted, remaining = [], []
        with mock.patch.object(collection_views, "BULK_DELETE_LIMIT", 2):
            for _ in range(2):
                response = client_for(self.alice).post(
                    self.url, {"all": True}, format="json"
                )
                deleted += response.data["deleted"]
                remaining.append(response.data["remaining"])

        self.assertEqual(remaining, [1, 0])
        self.assertEqual(sorted(deleted), [c.id for c in mine])
        self.assertTrue(Collection.objects.filter(pk=theirs.id).exists())

    def test_a_collection_deleted_part_way_through_is_not_found(self):
        """Someone else deletes the second collection while this request is
        on the first. It must not be audited as a delete this request did."""
        first, second = (
            self.collection("First", self.alice),
            self.collection("Second", self.alice),
        )
        real_audit = collection_views.CollectionViewSet._audit

        def audit_then_race(viewset, action, collection, **metadata):
            real_audit(viewset, action, collection, **metadata)
            if collection.pk == first.pk:
                Collection.objects.filter(pk=second.pk).delete()

        with mock.patch.object(
            collection_views.CollectionViewSet,
            "_audit",
            autospec=True,
            side_effect=audit_then_race,
        ):
            response = client_for(self.alice).post(
                self.url, {"ids": [first.id, second.id]}, format="json"
            )

        self.assertEqual(response.data["deleted"], [first.id])
        self.assertEqual(response.data["not_found"], [second.id])
        audited = AuditLog.objects.filter(action=AuditLog.Action.COLLECTION_DELETE)
        self.assertEqual([row.metadata["collection_id"] for row in audited], [first.id])

    def test_the_queryset_scope_agrees_with_the_per_collection_rule(self):
        no_department_admin = make_user("bulk_nodept_cadmin", Role.DEPARTMENT_ADMIN, "")
        users = [
            self.root,
            self.physics_admin,
            self.alice,
            self.carol,
            no_department_admin,
        ]
        collections = [
            self.collection("A", self.alice),
            self.collection("C", self.carol),
            self.collection("N", no_department_admin, department="Physics"),
        ]

        for user in users:
            expected = {
                c.id
                for c in collections
                if authorization.can_manage_collection(user, c)
            }
            scoped = authorization.manageable_collections(
                user, Collection.objects.all()
            )
            self.assertEqual(
                set(scoped.values_list("id", flat=True)), expected, user.username
            )


class ConversationBulkDeleteTests(BulkDeleteTestCase):
    url = reverse("conversation-bulk-delete")

    def conversation(self, owner, title="Attendance rules"):
        conversation = Conversation.objects.create(owner=owner, title=title)
        Message.objects.create(
            conversation=conversation, role=Message.Role.USER, content="q"
        )
        return conversation

    def test_selected_conversations_and_their_messages_go(self):
        first, second = self.conversation(self.alice), self.conversation(self.alice)
        kept = self.conversation(self.alice)

        response = client_for(self.alice).post(
            self.url, {"ids": [first.id, second.id]}, format="json"
        )

        self.assertEqual(
            response.data, {"deleted": [first.id, second.id], "not_found": []}
        )
        self.assertEqual(
            list(Conversation.objects.values_list("id", flat=True)), [kept.id]
        )
        self.assertFalse(
            Message.objects.filter(conversation_id__in=[first.id, second.id]).exists()
        )

    def test_another_users_conversation_is_not_found_and_kept(self):
        """Even a system administrator's: a conversation is its owner's."""
        theirs = self.conversation(self.alice)

        response = client_for(self.root).post(
            self.url, {"ids": [theirs.id, 999999]}, format="json"
        )

        self.assertEqual(
            response.data, {"deleted": [], "not_found": [theirs.id, 999999]}
        )
        self.assertTrue(Conversation.objects.filter(pk=theirs.id).exists())

    def test_all_deletes_every_one_of_the_callers_conversations_only(self):
        mine = [self.conversation(self.alice) for _ in range(3)]
        theirs = self.conversation(self.bob)

        response = client_for(self.alice).post(self.url, {"all": True}, format="json")

        self.assertEqual(response.data["deleted"], [c.id for c in mine])
        self.assertEqual(
            list(Conversation.objects.values_list("id", flat=True)), [theirs.id]
        )

    def test_all_works_through_a_large_set_in_bounded_requests(self):
        mine = [self.conversation(self.alice) for _ in range(3)]

        deleted, remaining = [], []
        with mock.patch.object(chat_views, "BULK_DELETE_LIMIT", 2):
            for _ in range(2):
                response = client_for(self.alice).post(
                    self.url, {"all": True}, format="json"
                )
                deleted += response.data["deleted"]
                remaining.append(response.data["remaining"])

        self.assertEqual(remaining, [1, 0])
        self.assertEqual(sorted(deleted), [c.id for c in mine])

    def test_conversations_started_after_as_of_are_left_alone(self):
        early = self.conversation(self.alice)
        as_of = timezone.now()
        late = self.conversation(self.alice)
        Conversation.objects.filter(pk=late.pk).update(
            created_at=as_of + timedelta(seconds=1)
        )

        response = client_for(self.alice).post(
            self.url, {"all": True, "as_of": as_of.isoformat()}, format="json"
        )

        self.assertEqual(response.data["deleted"], [early.id])
        self.assertTrue(Conversation.objects.filter(pk=late.id).exists())

    def test_the_request_is_validated(self):
        client = client_for(self.alice)
        bodies = (
            {},
            {"ids": []},
            {"ids": [1], "all": True},
            {"ids": ["x"]},
            {"all": True, "filters": {"search": "x"}},
            {"ids": [1], "as_of": "2026-01-01T00:00:00Z"},
        )
        for body in bodies:
            response = client.post(self.url, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)

    def test_a_signed_out_caller_is_refused(self):
        response = APIClient().post(self.url, {"all": True}, format="json")
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
