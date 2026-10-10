from django.db import transaction
from django.http import FileResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from accounts.services import UnknownDepartment, resolve_department
from documents import authorization
from documents.api import discovery
from documents.audit import record_event
from documents.models import AuditLog, Document, DocumentChunk, ProcessingJob
from documents.permissions import DocumentAccessPolicy
from documents.services import (
    DocumentArchived,
    DocumentQuarantined,
    ProcessingInProgress,
    archive_document,
    create_document_version,
    is_quarantined,
    reprocess_document,
    restore_document,
)
from documents.throttling import BurstUserThrottle

from .serializers import (
    ClassificationResultSerializer,
    DocumentChunkSerializer,
    DocumentContentSerializer,
    DocumentCreateSerializer,
    DocumentFigureSerializer,
    DocumentListQuerySerializer,
    DocumentListSerializer,
    DocumentSerializer,
    DocumentTableSerializer,
    DocumentVersionCreateSerializer,
    DocumentVersionSerializer,
    ExtractionResultSerializer,
    ProcessingJobSerializer,
)


def _live_content(version):
    """The extraction pass retrieval answers from, for this version.

    The `current_content` pointer, the same one search filters on - so the
    text a reader inspects here is the text a citation was built from.
    Data written before the pointer existed falls back to the newest pass.
    A quarantined version has none: quarantine withdraws its text from
    search and chat, and the fallback handed back the condemned pass here.
    """
    if is_quarantined(version):
        return None
    if version.current_content_id:
        return version.current_content
    return version.contents.first()


def _conflict(message):
    return Response({"detail": message}, status=status.HTTP_409_CONFLICT)


def _query_number(raw):
    """A non-negative integer query parameter, or None if it is not one.

    `str.isdigit()` is not a parse check: "²" is a digit int() refuses, and
    int() refuses any string past 4,300 digits - so testing isdigit() and
    then calling int() turned both into a 500 instead of a 400.
    """
    raw = raw.strip()
    if not raw.isdecimal():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class DocumentViewSet(viewsets.ModelViewSet):
    serializer_class = DocumentSerializer
    permission_classes = [DocumentAccessPolicy]
    parser_classes = [MultiPartParser, FormParser]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    throttle_classes = [BurstUserThrottle]

    def get_throttles(self):
        # Only the write paths carry the upload budget. Throttling a
        # listing at the same rate as an upload would make the UI
        # unusable without making abuse meaningfully harder. "versions"
        # is both - GET lists, POST uploads - and matching the action name
        # alone spent the upload budget on every visit to the Versions tab.
        if self.action in ("create", "reprocess") or (
            self.action == "versions" and self.request.method == "POST"
        ):
            self.throttle_scope = "upload"
        else:
            self.throttle_scope = None
        return super().get_throttles()

    def get_serializer_context(self):
        # Carries the request so DocumentSerializer can say what the CALLER
        # may do with the document - a UI hint only; every action is still
        # decided by DocumentAccessPolicy.
        return {**super().get_serializer_context(), "request": self.request}

    def get_serializer_class(self):
        # The list action gets a discovery-appropriate shape - see
        # DocumentListSerializer for exactly how it differs and why.
        if self.action == "list":
            return DocumentListSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        # select_related on the listing, measured rather than guessed: the
        # serializer nests current_version, so without the join the endpoint
        # issued one extra query PER DOCUMENT - 14 queries for 12 documents,
        # and a little over a thousand for a thousand. The detail actions
        # fetch one row, so the join buys them nothing.
        queryset = Document.objects.all()

        if self.action == "list":
            # Validated BEFORE authorization is even consulted, so an
            # invalid filter value fails with a 400 naming the field,
            # never a 500 from an unparsed int() or a queryset that
            # silently matched nothing.
            query = DocumentListQuerySerializer(
                data=self.request.query_params, context={"request": self.request}
            )
            query.is_valid(raise_exception=True)

            # Authorization scopes the queryset FIRST. discovery.apply()
            # only ever narrows it further with additional .filter() calls,
            # which Django ANDs onto what is already here - so no search
            # term, filter value or ordering field it accepts can widen the
            # result set back past this boundary. See discovery.py.
            scoped = authorization.visible_documents(
                self.request.user, queryset.select_related("current_version", "owner")
            )
            return discovery.apply(scoped, query.validated_data)

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
        if authorization.is_system_admin(request.user):
            # Validated against the registry: a typo here used to create a
            # department nobody belonged to, and the document vanished from
            # every listing but the admin's own.
            try:
                department = resolve_department(data["department"])
            except UnknownDepartment as exc:
                return Response(
                    {"department": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST
                )
        else:
            department = profile.department

        # One transaction: a document is only meaningful with its first
        # version. Committed separately, a failure storing the version left
        # a document with no version behind a 500 - listed to the whole
        # department, UPLOADED forever, and refused by reprocess.
        with transaction.atomic():
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
            DocumentSerializer(document, context={"request": request}).data,
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

            try:
                version = create_document_version(
                    document,
                    serializer.validated_data["file"],
                    uploaded_by=request.user,
                )
            except DocumentArchived as exc:
                return _conflict(str(exc))

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
        except ProcessingInProgress as exc:
            # 409, with the job that is already running, so a client that
            # double-submitted can follow the existing run rather than
            # guessing whether anything happened.
            return Response(
                {"detail": str(exc), "job": ProcessingJobSerializer(exc.job).data},
                status=status.HTTP_409_CONFLICT,
            )
        except (DocumentArchived, DocumentQuarantined) as exc:
            return _conflict(str(exc))
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

        latest = _live_content(version)

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

        content = _live_content(version)

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
    def tables(self, request, pk=None):
        """Tables detected in the live extraction pass, with their cells.

        Each table carries the page and character span it was read from, so
        a cell can be checked against the page text it claims to come from.
        """
        document = self.get_object()
        version = document.current_version
        content = _live_content(version) if version else None
        if content is None:
            return Response([])
        return Response(DocumentTableSerializer(content.tables.all(), many=True).data)

    @action(detail=True, methods=["get"])
    def figures(self, request, pk=None):
        """Figures located in the live extraction pass. Located, not read:
        an empty description means no vision model has described it."""
        document = self.get_object()
        version = document.current_version
        content = _live_content(version) if version else None
        if content is None:
            return Response([])
        return Response(DocumentFigureSerializer(content.figures.all(), many=True).data)

    @action(detail=True, methods=["get"])
    def source(self, request, pk=None):
        """The exact evidence behind a citation: `?chunk=<id>`.

        Returns the page text of the extraction pass the chunk was cut from
        - not today's pass - with the chunk's span inside it. A citation
        stored on an old answer must still point at the words it quoted;
        highlighting the same offsets on reprocessed text would put the
        highlight on different words and present them as the evidence.
        `is_current` says whether the reader is looking at the live text.
        """
        document = self.get_object()
        chunk_id = _query_number(request.query_params.get("chunk", ""))
        if chunk_id is None:
            return Response(
                {"chunk": ["A chunk id is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        chunk = (
            DocumentChunk.objects.select_related("document_version", "document_content")
            .filter(pk=chunk_id, document_version__document=document)
            .first()
        )
        if chunk is None or is_quarantined(chunk.document_version):
            # A quarantined version's passages are withdrawn with its text,
            # including those an older answer still cites.
            return Response(
                {"detail": "That passage is not part of this document."},
                status=status.HTTP_404_NOT_FOUND,
            )

        page = chunk.document_content.pages.filter(
            page_number=chunk.page_number
        ).first()
        version = chunk.document_version
        return Response(
            {
                "document_id": document.id,
                "document_title": document.title,
                "chunk_id": chunk.id,
                "page_number": chunk.page_number,
                "page_count": chunk.document_content.page_count,
                "character_start": chunk.character_start,
                "character_end": chunk.character_end,
                "chunk_text": chunk.text,
                "page_text": page.text if page else "",
                "extraction_method": page.extraction_method if page else "",
                "version_number": version.version_number,
                "is_current": (
                    document.current_version_id == version.id
                    and version.current_content_id == chunk.document_content_id
                ),
            }
        )

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        document = archive_document(self.get_object(), request.user)
        return Response(DocumentSerializer(document, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        document = restore_document(self.get_object())
        return Response(DocumentSerializer(document, context={"request": request}).data)

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        """The original bytes of the current version, or of `?version=N`.

        Earlier versions stay downloadable because they are evidence: the
        regulation in force last year is the document a dispute about last
        year is decided by. The access policy records the requested version
        on the audit row it writes for this action.
        """
        document = self.get_object()
        requested = request.query_params.get("version", "").strip()

        if requested:
            number = _query_number(requested)
            if number is None:
                return Response(
                    {"version": ["Must be a positive version number."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            version = document.versions.filter(version_number=number).first()
        else:
            version = document.current_version

        if version is None:
            return Response(
                {"detail": "Document has no such version to download."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # A file the scanner flagged is quarantined: the pipeline refused to
        # parse it, and handing the original bytes to every department member
        # (students included) as an attachment undid that decision. Judged
        # per VERSION - an earlier infected upload stays infected after a
        # clean one replaces it. A system administrator may still retrieve
        # it, for review.
        if is_quarantined(version) and not authorization.is_system_admin(request.user):
            return Response(
                {
                    "detail": "This file was quarantined by the security scan and "
                    "cannot be downloaded. An administrator can review it."
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        return FileResponse(
            version.file.open("rb"),
            as_attachment=True,
            filename=version.file_name,
        )
