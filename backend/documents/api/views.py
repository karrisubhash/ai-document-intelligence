from django.http import FileResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from documents import authorization
from documents.audit import record_event
from documents.models import AuditLog, Document, ProcessingJob
from documents.permissions import DocumentAccessPolicy
from documents.services import create_document_version, reprocess_document
from documents.throttling import BurstUserThrottle

from .serializers import (
    ClassificationResultSerializer,
    DocumentChunkSerializer,
    DocumentContentSerializer,
    DocumentCreateSerializer,
    DocumentSerializer,
    DocumentVersionCreateSerializer,
    DocumentVersionSerializer,
    ExtractionResultSerializer,
    ProcessingJobSerializer,
)


class DocumentViewSet(viewsets.ModelViewSet):
    serializer_class = DocumentSerializer
    permission_classes = [DocumentAccessPolicy]
    parser_classes = [MultiPartParser, FormParser]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    throttle_classes = [BurstUserThrottle]

    def get_throttles(self):
        # Only the write paths carry the upload budget. Throttling a
        # listing at the same rate as an upload would make the UI
        # unusable without making abuse meaningfully harder.
        if self.action in ("create", "versions", "reprocess"):
            self.throttle_scope = "upload"
        else:
            self.throttle_scope = None
        return super().get_throttles()


    def get_queryset(self):
        # select_related on the listing, measured rather than guessed: the
        # serializer nests current_version, so without the join the endpoint
        # issued one extra query PER DOCUMENT - 14 queries for 12 documents,
        # and a little over a thousand for a thousand. The detail actions
        # fetch one row, so the join buys them nothing.
        queryset = Document.objects.all()

        if self.action == "list":
            return authorization.visible_documents(
                self.request.user, queryset.select_related("current_version")
            )

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = DocumentCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Department is a security boundary: only a system admin may
        # place a document outside their own department. Everyone else's
        # upload is pinned to the department on their own profile,
        # regardless of what the client sent.
        profile = authorization.get_profile(request.user)
        department = (
            data["department"]
            if authorization.is_system_admin(request.user)
            else profile.department
        )

        document = Document.objects.create(
            title=data["title"],
            document_type=data["document_type"],
            department=department,
            owner=request.user,
        )
        create_document_version(document, data["file"], uploaded_by=request.user)
        document.refresh_from_db()

        record_event(
            user=request.user,
            action=AuditLog.Action.DOCUMENT_CREATE,
            document=document,
            result=AuditLog.Result.ALLOWED,
        )

        return Response(
            DocumentSerializer(document).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["get", "post"])
    def versions(self, request, pk=None):
        document = self.get_object()

        if request.method == "POST":
            serializer = DocumentVersionCreateSerializer(
                data=request.data, context={"request": request}
            )
            serializer.is_valid(raise_exception=True)

            version = create_document_version(
                document, serializer.validated_data["file"], uploaded_by=request.user
            )

            return Response(
                DocumentVersionSerializer(version).data,
                status=status.HTTP_202_ACCEPTED,
            )

        versions = document.versions.all()
        return Response(DocumentVersionSerializer(versions, many=True).data)

    @action(detail=True, methods=["get"])
    def processing(self, request, pk=None):
        document = self.get_object()
        jobs = ProcessingJob.objects.filter(
            document_version__document=document
        ).prefetch_related("events")

        return Response(ProcessingJobSerializer(jobs, many=True).data)

    @action(detail=True, methods=["post"])
    def reprocess(self, request, pk=None):
        document = self.get_object()

        try:
            job = reprocess_document(document)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            ProcessingJobSerializer(job).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["get"])
    def content(self, request, pk=None):
        """Page-level extraction output for the document's current version."""
        document = self.get_object()
        version = document.current_version

        if version is None:
            return Response(
                {"detail": "Document has no version."},
                status=status.HTTP_404_NOT_FOUND,
            )

        latest = version.contents.prefetch_related("pages").first()

        if latest is None:
            return Response(
                {"detail": "No extracted content for this version yet."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(DocumentContentSerializer(latest).data)

    @action(detail=True, methods=["get"])
    def classification(self, request, pk=None):
        """Every classification verdict recorded for the current version.

        A list rather than the latest verdict alone, because the point of
        storing runs per model is being able to see them side by side. An
        empty list is a real answer - the stage records SKIPPED when no
        classifier is configured, and the pipeline history says so.
        """
        document = self.get_object()
        version = document.current_version

        if version is None:
            return Response(
                {"detail": "Document has no version."},
                status=status.HTTP_404_NOT_FOUND,
            )

        results = version.classification_results.all()
        return Response(ClassificationResultSerializer(results, many=True).data)

    @action(detail=True, methods=["get"])
    def extraction(self, request, pk=None):
        """Every field-extraction pass recorded for the current version."""
        document = self.get_object()
        version = document.current_version

        if version is None:
            return Response(
                {"detail": "Document has no version."},
                status=status.HTTP_404_NOT_FOUND,
            )

        results = version.extraction_results.prefetch_related("extracted_fields")
        return Response(ExtractionResultSerializer(results, many=True).data)

    @action(detail=True, methods=["get"])
    def chunks(self, request, pk=None):
        """Retrievable units produced from the current version's content.

        Paginated: a long document produces thousands of chunks, and the
        full text of all of them is not a response anyone asked for.
        """
        document = self.get_object()
        version = document.current_version

        if version is None:
            return Response(
                {"detail": "Document has no version."},
                status=status.HTTP_404_NOT_FOUND,
            )

        content = version.contents.first()

        if content is None:
            return Response(
                {"detail": "No extracted content for this version yet."},
                status=status.HTTP_404_NOT_FOUND,
            )

        queryset = content.chunks.prefetch_related("embeddings")
        page = self.paginate_queryset(queryset)
        serializer = DocumentChunkSerializer(page, many=True)

        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        document = self.get_object()
        version = document.current_version

        if version is None:
            return Response(
                {"detail": "Document has no version to download."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return FileResponse(
            version.file.open("rb"),
            as_attachment=True,
            filename=version.file_name,
        )
