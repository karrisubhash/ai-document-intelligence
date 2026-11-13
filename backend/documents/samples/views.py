from rest_framework import exceptions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsSystemAdministrator
from accounts.services import UnknownDepartment, resolve_department
from documents import samples
from documents.throttling import BurstUserThrottle


class LibraryAccountConflict(exceptions.APIException):
    """A person's account holds the library's name (samples.library_owner)."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "sample_library_account_taken"


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
    is sixteen of. 409 when a person's account holds the library's name.
    """

    permission_classes = [IsSystemAdministrator]
    throttle_classes = [BurstUserThrottle]
    throttle_scope = "samples"

    def post(self, request):
        serializer = SampleLoadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            outcome = samples.seed(
                serializer.validated_data["department"], actor=request.user, via="api"
            )
        except samples.LibraryAccountTaken as exc:
            raise LibraryAccountConflict(
                {"detail": str(exc), "code": LibraryAccountConflict.default_code}
            ) from exc
        return Response(
            {"queued": len(outcome.queued), "skipped": len(outcome.skipped)},
            status=status.HTTP_202_ACCEPTED,
        )
