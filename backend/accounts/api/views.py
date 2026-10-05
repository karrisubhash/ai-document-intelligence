from django.contrib.auth import (
    authenticate,
    get_user_model,
    login,
    logout,
    update_session_auth_hash,
)
from django.db import transaction
from django.db.models import Count, Q
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import exceptions, mixins, status, viewsets
from rest_framework.authentication import CSRFCheck
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts import permissions
from accounts.authentication import issue_token
from accounts.models import Department, UserProfile
from accounts.services import (
    DepartmentNameInUse,
    record_failed_login,
    rename_department,
    revoke_api_tokens,
)
from documents.audit import record_event
from documents.models import AuditLog
from documents.throttling import AccountLoginRateThrottle, LoginRateThrottle

from .serializers import (
    AdminUserSerializer,
    DepartmentSerializer,
    LoginSerializer,
    MeSerializer,
    PasswordChangeSerializer,
    PublicDepartmentSerializer,
    SetPasswordSerializer,
)

User = get_user_model()


def _enforce_csrf(request):
    """Run Django's CSRF check on a view DRF would otherwise exempt.

    DRF only enforces CSRF for requests that are ALREADY session
    authenticated, which leaves login itself unprotected. A forged login -
    a hostile page that signs the victim's browser into the attacker's
    account - is the classic consequence: everything the victim then
    uploads or asks lands in an account the attacker can read.
    """
    check = CSRFCheck(lambda req: None)
    check.process_request(request)
    reason = check.process_view(request, None, (), {})
    if reason:
        raise exceptions.PermissionDenied(f"CSRF check failed: {reason}")


def _audit_login(user, *, method):
    AuditLog.objects.create(
        user=user,
        action=AuditLog.Action.LOGIN,
        result=AuditLog.Result.ALLOWED,
        metadata={"method": method},
    )


def _authenticate(request, data, *, method):
    user = authenticate(request, username=data["username"], password=data["password"])
    if user is None:
        record_failed_login(data["username"], method=method)
        # One message for "no such user" and "wrong password", so the
        # endpoint cannot be used to discover which usernames exist. A 400,
        # as DRF's own token view returns, so existing clients see no change.
        raise exceptions.ValidationError(
            {"non_field_errors": ["Invalid username or password."]}
        )
    return user


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MeSerializer(request.user).data)


@method_decorator(ensure_csrf_cookie, name="get")
class CsrfView(APIView):
    """Sets the CSRF cookie and returns the token.

    The single-page frontend calls this once before its first unsafe
    request. Same-origin only: CORS credentials stay off, so a foreign page
    can neither read this response nor send the cookie back.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    # Unthrottled on purpose. It is anonymous, so the default anonymous
    # throttle (30/hour per IP) applied - and behind nginx, or a campus NAT,
    # every browser shares one IP. The site would have stopped letting
    # anyone sign in after thirty page loads an hour. Issuing a CSRF token
    # costs nothing and authenticates nobody; the login it enables has its
    # own per-username throttle.
    throttle_classes = []

    def get(self, request):
        return Response({"csrf_token": get_token(request)})


class LoginView(APIView):
    """Browser sign-in: an httpOnly session cookie, never a readable token.

    The testing console kept a DRF token in localStorage, which any script
    running on the origin can read - one XSS and every signed-in user's
    credential leaves the building. A session cookie marked httpOnly cannot
    be read by script at all, is Secure and SameSite=Lax in production, and
    is invalidated server-side on logout and on every password change.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginRateThrottle, AccountLoginRateThrottle]

    def post(self, request):
        _enforce_csrf(request)
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = _authenticate(request, serializer.validated_data, method="session")
        # login() rotates the session key, so a session id planted before
        # authentication (fixation) is worthless after it.
        login(request._request, user)
        _audit_login(user, method="session")

        return Response(MeSerializer(user).data)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        method = "token" if request.auth is not None else "session"
        if request.auth is not None:
            # A token client logging out means "revoke this credential".
            request.auth.delete()
        logout(request._request)
        record_event(
            user=user,
            action=AuditLog.Action.LOGOUT,
            result=AuditLog.Result.ALLOWED,
            method=method,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [LoginRateThrottle, AccountLoginRateThrottle]

    def post(self, request):
        serializer = PasswordChangeSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        # Every OTHER session dies with the old password hash; this one is
        # kept, so changing your password does not sign you out mid-task.
        update_session_auth_hash(request, user)
        revoke_api_tokens(user)

        record_event(
            user=user,
            action=AuditLog.Action.PASSWORD_CHANGE,
            result=AuditLog.Result.ALLOWED,
            initiated_by="self",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class TokenView(APIView):
    """API token for scripts and service clients. Expires; see
    accounts.authentication."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginRateThrottle, AccountLoginRateThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = _authenticate(request, serializer.validated_data, method="token")
        token = issue_token(user)
        _audit_login(user, method="token")
        return Response({"token": token.key})


class DepartmentListView(APIView):
    """Active departments, for any signed-in user's filters and forms."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        departments = Department.objects.filter(is_active=True)
        return Response(PublicDepartmentSerializer(departments, many=True).data)


# --- Administration ---------------------------------------------------------


class UserAdminViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Accounts, scoped by accounts.permissions. There is no DELETE:
    deactivation is the offboarding path, because a deleted user's audit
    rows would lose the account they are evidence about."""

    serializer_class = AdminUserSerializer
    permission_classes = [permissions.IsAdministrator]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        queryset = User.objects.select_related("profile").order_by("username")
        queryset = permissions.administrable_users(self.request.user, queryset)

        params = self.request.query_params
        search = params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(username__icontains=search)
                | Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        if params.get("role"):
            queryset = queryset.filter(profile__role=params["role"])
        if params.get("department"):
            queryset = queryset.filter(profile__department=params["department"])
        if params.get("is_active") in ("true", "false"):
            queryset = queryset.filter(is_active=params["is_active"] == "true")
        return queryset

    def get_object(self):
        target = super().get_object()
        # administrable_users already hides accounts outside the actor's
        # department (they 404). This refuses the ones inside it that the
        # actor may still not edit - other admins - on writes only.
        if self.request.method not in ("GET", "HEAD", "OPTIONS"):
            if not permissions.can_administer(self.request.user, target):
                raise exceptions.PermissionDenied("You may not modify this account.")
        return target

    def perform_create(self, serializer):
        user = serializer.save()
        record_event(
            user=self.request.user,
            action=AuditLog.Action.USER_CREATE,
            result=AuditLog.Result.ALLOWED,
            target_user_id=user.id,
            role=user.profile.role,
            department=user.profile.department,
        )

    def perform_update(self, serializer):
        before = {
            "role": serializer.instance.profile.role,
            "department": serializer.instance.profile.department,
            "is_active": serializer.instance.is_active,
        }
        user = serializer.save()
        after = {
            "role": user.profile.role,
            "department": user.profile.department,
            "is_active": user.is_active,
        }
        if not user.is_active:
            revoke_api_tokens(user)
        changed = {
            key: [before[key], after[key]]
            for key in before
            if before[key] != after[key]
        }
        record_event(
            user=self.request.user,
            action=AuditLog.Action.USER_UPDATE,
            result=AuditLog.Result.ALLOWED,
            target_user_id=user.id,
            changed=changed,
        )

    @action(detail=True, methods=["post"], url_path="set-password")
    def set_password(self, request, pk=None):
        target = self.get_object()
        if target.pk == request.user.pk:
            raise exceptions.ValidationError(
                {"password": ["Change your own password from your account page."]}
            )
        serializer = SetPasswordSerializer(
            data=request.data, context={"target": target}
        )
        serializer.is_valid(raise_exception=True)

        target.set_password(serializer.validated_data["password"])
        target.save(update_fields=["password"])
        # The new hash already invalidates every existing session; tokens
        # have no hash to check, so they go explicitly.
        revoke_api_tokens(target)

        record_event(
            user=request.user,
            action=AuditLog.Action.PASSWORD_CHANGE,
            result=AuditLog.Result.ALLOWED,
            target_user_id=target.id,
            initiated_by="administrator",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def roles(self, request):
        """The roles this administrator may assign, for the role picker."""
        assignable = permissions.assignable_roles(request.user)
        return Response(
            [
                {"value": value, "label": label}
                for value, label in UserProfile.Role.choices
                if value in assignable
            ]
        )


class DepartmentAdminViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """The department registry. Writes are SYSTEM_ADMIN only; there is no
    DELETE, because documents and audit rows name departments and a
    department is retired by deactivating it."""

    serializer_class = DepartmentSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [permissions.IsAdministrator()]
        return [permissions.IsSystemAdministrator()]

    def get_queryset(self):
        from documents.models import Document

        member_counts = (
            UserProfile.objects.exclude(department="")
            .values("department")
            .annotate(total=Count("id"))
        )
        document_counts = (
            Document.objects.filter(archived_at__isnull=True)
            .values("department")
            .annotate(total=Count("id"))
        )
        self._members = {row["department"]: row["total"] for row in member_counts}
        self._documents = {row["department"]: row["total"] for row in document_counts}
        return Department.objects.all()

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        rows = (
            response.data["results"]
            if isinstance(response.data, dict)
            else response.data
        )
        # A department admin sees their OWN department's counts only. How
        # many people and documents every other department holds is exactly
        # the disclosure the document policy refuses to make.
        scope = (
            None
            if permissions.is_system_admin(request.user)
            else permissions.get_profile(request.user).department
        )
        for row in rows:
            if scope is not None and row["name"] != scope:
                row["member_count"] = None
                row["document_count"] = None
                continue
            row["member_count"] = self._members.get(row["name"], 0)
            row["document_count"] = self._documents.get(row["name"], 0)
        return response

    def perform_create(self, serializer):
        department = serializer.save()
        record_event(
            user=self.request.user,
            action=AuditLog.Action.DEPARTMENT_CREATE,
            result=AuditLog.Result.ALLOWED,
            department=department.name,
        )

    def perform_update(self, serializer):
        department = serializer.instance
        old_name = department.name
        new_name = serializer.validated_data.pop("name", old_name)
        try:
            with transaction.atomic():
                department = serializer.save()
                if new_name != old_name:
                    rename_department(department, new_name)
        except DepartmentNameInUse as exc:
            raise exceptions.ValidationError({"name": [str(exc)]}) from exc
        record_event(
            user=self.request.user,
            action=AuditLog.Action.DEPARTMENT_UPDATE,
            result=AuditLog.Result.ALLOWED,
            department=department.name,
            **({"renamed_from": old_name} if new_name != old_name else {}),
        )
