"""Collections API (spec 14, 15.2; FR-14).

    GET    /api/v1/collections/                  visible collections
    POST   /api/v1/collections/                  create (curators)
    GET    /api/v1/collections/{id}/
    PATCH  /api/v1/collections/{id}/             rename / describe (managers)
    DELETE /api/v1/collections/{id}/             delete; documents are untouched
    POST   /api/v1/collections/{id}/documents/   {"document_ids": [...]} add
    DELETE /api/v1/collections/{id}/documents/   {"document_ids": [...]} remove

Search and chat take `collection_id` to narrow to one collection.
"""

from django.db import IntegrityError, transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from accounts.services import UnknownDepartment, resolve_department
from documents import authorization
from documents.audit import record_event
from documents.models import AuditLog, Collection, Document

from .serializers import (
    CollectionCreateSerializer,
    CollectionSerializer,
    MembershipSerializer,
)


def _name_taken():
    return Response(
        {"name": ["A collection with this name already exists in the department."]},
        status=status.HTTP_400_BAD_REQUEST,
    )


class CollectionViewSet(viewsets.ModelViewSet):
    serializer_class = CollectionSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        # Authorization scopes every action, so an invisible collection is a
        # 404 - never a 403 that would confirm the id exists.
        return authorization.visible_collections(
            self.request.user,
            Collection.objects.select_related("created_by").prefetch_related(
                "documents"
            ),
        )

    def _require_manage(self, collection):
        if not authorization.can_manage_collection(self.request.user, collection):
            raise PermissionDenied("You cannot change this collection.")

    def _audit(self, action, collection, **metadata):
        record_event(
            user=self.request.user,
            action=action,
            result=AuditLog.Result.ALLOWED,
            collection_id=collection.pk,
            collection=collection.name,
            **metadata,
        )

    def create(self, request, *args, **kwargs):
        if not authorization.can_create_collection(request.user):
            raise PermissionDenied("Your role cannot create collections.")
        serializer = CollectionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # The same boundary rule as uploads: only a system administrator
        # chooses the department; everyone else's is their own.
        if authorization.is_system_admin(request.user):
            try:
                department = resolve_department(data["department"])
            except UnknownDepartment as exc:
                return Response(
                    {"department": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST
                )
        else:
            department = authorization.get_profile(request.user).department

        try:
            with transaction.atomic():
                collection = Collection.objects.create(
                    name=data["name"],
                    description=data["description"],
                    department=department,
                    created_by=request.user,
                )
        except IntegrityError:
            return _name_taken()
        self._audit(AuditLog.Action.COLLECTION_CREATE, collection)
        return Response(
            CollectionSerializer(collection, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        collection = self.get_object()
        self._require_manage(collection)
        serializer = CollectionSerializer(
            collection,
            data={
                k: v for k, v in request.data.items() if k in ("name", "description")
            },
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            return _name_taken()
        self._audit(AuditLog.Action.COLLECTION_UPDATE, collection, change="details")
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        # PUT is not offered (http_method_names); this guards a router change.
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        collection = self.get_object()
        self._require_manage(collection)
        self._audit(AuditLog.Action.COLLECTION_DELETE, collection)
        collection.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post", "delete"])
    def documents(self, request, pk=None):
        collection = self.get_object()
        self._require_manage(collection)
        serializer = MembershipSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = set(serializer.validated_data["document_ids"])

        if request.method == "DELETE":
            # Removing needs no visibility check beyond managing the
            # collection: it can only shrink what the collection exposes.
            collection.documents.remove(*ids)
            self._audit(
                AuditLog.Action.COLLECTION_UPDATE, collection, removed=sorted(ids)
            )
        else:
            documents = authorization.visible_documents(
                request.user, Document.objects.filter(pk__in=ids)
            ).filter(department=collection.department)
            found = set(documents.values_list("id", flat=True))
            missing = ids - found
            if missing:
                # One answer for "does not exist", "cannot see it" and "other
                # department", so the response is no oracle for any of them.
                raise NotFound(
                    {
                        "document_ids": sorted(missing),
                        "detail": "Not found, or not in this collection's department.",
                    }
                )
            collection.documents.add(*documents)
            self._audit(
                AuditLog.Action.COLLECTION_UPDATE, collection, added=sorted(found)
            )

        collection.refresh_from_db()
        return Response(
            CollectionSerializer(collection, context={"request": request}).data
        )
