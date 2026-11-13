from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsSystemAdministrator
from accounts.services import UnknownDepartment, resolve_department
from documents import samples
from documents.throttling import BurstUserThrottle


class SampleLoadSerializer(serializers.Serializer):
    department = serializers.CharField(max_length=255)

    def validate_department(self, value):
        try:
            department = resolve_department(value)
        except UnknownDepartment as exc:
            raise serializers.ValidationError(str(exc)) from exc
        if not department:
            raise serializers.ValidationError("Choose a department.")
        return department


class SampleLibraryView(APIView):
    """POST /api/v1/admin/samples/ {"department": "..."} - fill a department
    with the sample library, so an empty installation has something to read
    and ask about from its first minute.

    System administrators only: it places sixteen documents in whichever
    department is named. Answers 202 with how many samples were queued for
    processing and how many the department already held (documents.samples
    is idempotent by checksum). Audited - one SAMPLES_LOAD row, and a
    DOCUMENT_CREATE row per document - and throttled like uploads, which it
    is sixteen of.
    """

    permission_classes = [IsSystemAdministrator]
    throttle_classes = [BurstUserThrottle]
    throttle_scope = "samples"

    def post(self, request):
        serializer = SampleLoadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        outcome = samples.seed(
            serializer.validated_data["department"], actor=request.user, via="api"
        )
        return Response(
            {"queued": len(outcome.queued), "skipped": len(outcome.skipped)},
            status=status.HTTP_202_ACCEPTED,
        )
