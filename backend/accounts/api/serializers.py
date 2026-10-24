from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.hashers import make_password
from django.contrib.auth.validators import ASCIIUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from accounts import permissions
from accounts.models import Department, SignupRequest, UserProfile
from accounts.services import (
    UnknownDepartment,
    resolve_department,
    username_unavailable,
)
from documents.api.serializers import BulkSelectionSerializer
from documents.authorization import (
    can_create_collection,
    get_profile,
    is_system_admin,
)

User = get_user_model()


def _validate_password(password, user, *, field):
    """Django's password validators, reported against the field they judged.

    Left to DRF, a validator's error lands in `non_field_errors`, which a
    form cannot attach to the input that caused it.
    """
    try:
        password_validation.validate_password(password, user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({field: list(exc.messages)}) from exc


class MeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    is_superuser = serializers.BooleanField()
    role = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()
    # What the interface should offer this person. Derived from the same
    # functions the endpoints enforce, so the two cannot disagree - but the
    # endpoints still enforce; this only decides which screens to show.
    capabilities = serializers.SerializerMethodField()

    def get_role(self, user):
        return getattr(user.profile, "role", None)

    def get_department(self, user):
        return getattr(user.profile, "department", "")

    def get_capabilities(self, user):
        system = is_system_admin(user)
        administrator = permissions.is_administrator(user)
        return {
            "administer_users": administrator,
            "administer_departments": system,
            "view_audit_log": administrator,
            "view_operations": administrator,
            "view_system_status": system,
            "upload_to_any_department": system,
            "create_collections": can_create_collection(user),
        }


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=128, trim_whitespace=False)


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(max_length=128, trim_whitespace=False)
    new_password = serializers.CharField(max_length=128, trim_whitespace=False)

    def validate(self, attrs):
        user = self.context["request"].user
        if not user.check_password(attrs["current_password"]):
            raise serializers.ValidationError(
                {"current_password": ["The current password is incorrect."]}
            )
        _validate_password(attrs["new_password"], user, field="new_password")
        return attrs


class DepartmentSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(read_only=True, default=None)
    document_count = serializers.IntegerField(read_only=True, default=None)

    class Meta:
        model = Department
        fields = [
            "id",
            "name",
            "code",
            "description",
            "is_active",
            "member_count",
            "document_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A department needs a name.")
        clash = Department.objects.filter(name__iexact=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            # Case-insensitive on purpose: "Physics" and "physics" as two
            # tenants is the accident the registry exists to prevent.
            raise serializers.ValidationError("A department with this name exists.")
        return value

    def validate_code(self, value):
        value = value.strip().upper()
        if value:
            clash = Department.objects.filter(code=value)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError("This code is already in use.")
        return value


class PublicDepartmentSerializer(serializers.ModelSerializer):
    """What any signed-in user may know about a department: that it exists."""

    class Meta:
        model = Department
        fields = ["id", "name", "code"]
        read_only_fields = fields


class AdminUserSerializer(serializers.ModelSerializer):
    role = serializers.ChoiceField(
        choices=UserProfile.Role.choices, source="profile.role", required=False
    )
    department = serializers.CharField(
        source="profile.department", required=False, allow_blank=True, max_length=255
    )
    # Write-only, create only. Never echoed, never logged.
    password = serializers.CharField(
        write_only=True, required=False, trim_whitespace=False, max_length=128
    )
    # Whether the CALLER may edit this account - the same rule the write
    # endpoints enforce, so the screen offers only what will succeed. A
    # department admin's list includes their own account, other admins and
    # superusers, which every write refuses.
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_superuser",
            "role",
            "department",
            "password",
            "last_login",
            "date_joined",
            "can_manage",
        ]
        read_only_fields = ["id", "is_superuser", "last_login", "date_joined"]

    def get_can_manage(self, user) -> bool:
        return permissions.can_administer(self.actor, user)

    # --- validation -------------------------------------------------------

    @property
    def actor(self):
        return self.context["request"].user

    def validate_username(self, value):
        if self.instance is not None and value != self.instance.username:
            # Renaming changes what the audit trail's past rows appear to
            # say about who did what, and the username is the login.
            raise serializers.ValidationError("Usernames cannot be changed.")
        return value

    def validate(self, attrs):
        profile_changes = attrs.get("profile", {})
        actor = self.actor
        target = self.instance

        role = profile_changes.get("role")
        if role is not None and role not in permissions.assignable_roles(actor):
            raise serializers.ValidationError(
                {"role": [f"You may not assign the {role} role."]}
            )

        if "department" in profile_changes:
            try:
                profile_changes["department"] = resolve_department(
                    profile_changes["department"]
                )
            except UnknownDepartment as exc:
                raise serializers.ValidationError({"department": [str(exc)]}) from exc

        if not is_system_admin(actor):
            # A department admin works inside their own department only:
            # they may not place anyone outside it, and every account they
            # create lands in it.
            own = get_profile(actor).department
            wanted = profile_changes.get("department", own)
            if wanted != own:
                raise serializers.ValidationError(
                    {"department": ["You may only assign your own department."]}
                )
            if target is None or "department" in profile_changes:
                # Their own department goes through the registry too: an
                # inactive department accepts no new assignments, and
                # assigning it unchecked let a deactivated department keep
                # growing.
                try:
                    own = resolve_department(own)
                except UnknownDepartment as exc:
                    raise serializers.ValidationError(
                        {"department": [str(exc)]}
                    ) from exc
            profile_changes["department"] = own

        if target is not None and target.pk == actor.pk:
            if role is not None and role != get_profile(target).role:
                raise serializers.ValidationError(
                    {"role": ["You cannot change your own role."]}
                )
            if attrs.get("is_active") is False:
                raise serializers.ValidationError(
                    {"is_active": ["You cannot deactivate your own account."]}
                )

        if target is None:
            password = attrs.get("password")
            if not password:
                raise serializers.ValidationError(
                    {"password": ["An initial password is required."]}
                )
            candidate = User(
                username=attrs.get("username", ""),
                email=attrs.get("email", ""),
                first_name=attrs.get("first_name", ""),
                last_name=attrs.get("last_name", ""),
            )
            _validate_password(password, candidate, field="password")
        elif "password" in attrs:
            raise serializers.ValidationError(
                {"password": ["Use the set-password action to change a password."]}
            )

        attrs["profile"] = profile_changes
        return attrs

    # --- persistence ------------------------------------------------------

    def create(self, validated_data):
        profile_data = validated_data.pop("profile", {})
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()

        # The post_save signal created the profile with defaults.
        profile = user.profile
        profile.role = profile_data.get("role", UserProfile.Role.STUDENT)
        profile.department = profile_data.get("department", "")
        profile.save()
        return user

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", {})
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()

        if profile_data:
            profile = get_profile(instance)
            for field, value in profile_data.items():
                setattr(profile, field, value)
            profile.save()
        return instance


class SetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(max_length=128, trim_whitespace=False)

    def validate_password(self, value):
        try:
            password_validation.validate_password(value, self.context.get("target"))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


# --- Self-service sign-up -----------------------------------------------------

USERNAME_UNAVAILABLE = "This username is taken. Choose another."

# What an applicant may ask to be. Administrative roles are granted, never
# requested: offering them would only put a "make me an administrator"
# button in front of every stranger, for an administrator to click by
# mistake.
SIGNUP_ROLE_CHOICES = [
    (value, label)
    for value, label in UserProfile.Role.choices
    if value in permissions.DEPARTMENT_ASSIGNABLE_ROLES
]


def email_domain_allowed(email: str) -> bool:
    """Whether SIGNUP_EMAIL_DOMAINS admits `email` - its domain or any
    subdomain of it, so "example.edu" admits "physics.example.edu". An
    empty setting admits every address."""
    domains = settings.SIGNUP_EMAIL_DOMAINS
    if not domains:
        return True
    domain = email.rsplit("@", 1)[-1].lower()
    return any(
        domain == allowed or domain.endswith(f".{allowed}") for allowed in domains
    )


class SignupSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=254)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(
        max_length=150, required=False, allow_blank=True, default=""
    )
    password = serializers.CharField(
        max_length=128, trim_whitespace=False, write_only=True
    )
    department = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    role = serializers.ChoiceField(
        choices=SIGNUP_ROLE_CHOICES, default=UserProfile.Role.STUDENT
    )
    note = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default=""
    )

    def validate_username(self, value):
        # NFKC first, as Django's own create_user and UsernameField do, and
        # what is checked is what is stored: "ｒｏｏｔ" is "root". Then ASCII
        # only. A stranger choosing a name is choosing what administrators
        # and audit rows will display, and a Cyrillic "а" in "phy_аdmin"
        # survives NFKC and every case rule while looking exactly like the
        # department administrator. Accounts an administrator creates may
        # still use any letters.
        value = User.normalize_username(value)
        try:
            ASCIIUsernameValidator()(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        # The same answer whether an account or a waiting request holds the
        # name: which of the two it is is nobody else's business.
        if username_unavailable(value):
            raise serializers.ValidationError(USERNAME_UNAVAILABLE)
        return value

    def validate_email(self, value):
        value = User.objects.normalize_email(value)
        if not email_domain_allowed(value):
            allowed = ", ".join(
                f"@{domain}" for domain in settings.SIGNUP_EMAIL_DOMAINS
            )
            raise serializers.ValidationError(
                f"Use your institution's email address ({allowed})."
            )
        return value

    def validate_department(self, value):
        try:
            return resolve_department(value)
        except UnknownDepartment as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        candidate = User(
            username=attrs["username"],
            email=attrs["email"],
            first_name=attrs["first_name"],
            last_name=attrs["last_name"],
        )
        _validate_password(attrs["password"], candidate, field="password")
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        return SignupRequest.objects.create(
            password=make_password(password), **validated_data
        )


class SignupRequestSerializer(serializers.ModelSerializer):
    """A request as its reviewers see it. Never the password hash.

    Everything an applicant typed is unverified - nothing confirms that an
    email address belongs to the person who typed it - so `email_in_use`
    names the accounts, among those the reviewer administers, that already
    have the same address: the likeliest sign of someone posing as a member,
    or of a member who forgot they have an account.
    """

    reviewed_by = serializers.SlugRelatedField(slug_field="username", read_only=True)
    email_in_use = serializers.SerializerMethodField()

    class Meta:
        model = SignupRequest
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "department",
            "role",
            "note",
            "status",
            "created_at",
            "reviewed_by",
            "reviewed_at",
            "decision_note",
            "user",
            "email_in_use",
        ]
        read_only_fields = fields

    def get_email_in_use(self, signup) -> list[str]:
        request = self.context.get("request")
        if request is None or signup.status != SignupRequest.Status.PENDING:
            return []
        # Scoped to what the reviewer administers: a department admin does
        # not learn which address another department's member uses.
        accounts = permissions.administrable_users(
            request.user, User.objects.filter(email__iexact=signup.email)
        )
        return list(
            accounts.order_by("username").values_list("username", flat=True)[:5]
        )


class SignupApprovalSerializer(serializers.Serializer):
    """The role and department the new account gets - what was asked for,
    unless the reviewer says otherwise - held to the rules that bind the
    reviewer when they create an account by hand (AdminUserSerializer), and
    never an administrative role, even from a system administrator.

    An approval makes an account for someone nobody has verified, from a
    dialog whose role list a slip of the hand can move; making a stranger an
    administrator should be the separate, deliberate edit it is for anyone
    else, audited as a change to an existing account.
    """

    role = serializers.ChoiceField(choices=UserProfile.Role.choices, required=False)
    department = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate(self, attrs):
        actor = self.context["request"].user
        signup = self.context["signup"]

        role = attrs.get("role", signup.role)
        # This is also every reviewer's own limit: exactly what a department
        # admin may assign (accounts.permissions), and less than a system
        # admin may.
        if role not in permissions.DEPARTMENT_ASSIGNABLE_ROLES:
            raise serializers.ValidationError(
                {
                    "role": [
                        "An approval grants FACULTY, STAFF or STUDENT. Promote the "
                        "account from Users afterwards if it needs more."
                    ]
                }
            )

        try:
            department = resolve_department(attrs.get("department", signup.department))
        except UnknownDepartment as exc:
            raise serializers.ValidationError({"department": [str(exc)]}) from exc
        if not is_system_admin(actor) and department != get_profile(actor).department:
            raise serializers.ValidationError(
                {"department": ["You may only assign your own department."]}
            )

        return {"role": role, "department": department}


class SignupRejectionSerializer(serializers.Serializer):
    # Shown to the applicant the next time they try to sign in.
    note = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default=""
    )


class SignupBulkRejectSerializer(BulkSelectionSerializer):
    """Body of POST /api/v1/admin/signup-requests/bulk-reject/.

    The bulk deletes' selection (BulkSelectionSerializer): `ids`, or `all`
    waiting requests the reviewer decides, optionally narrowed by `search`
    exactly as the listing narrows them, within the `as_of` snapshot.
    `preview: true` declines nothing and answers the count and `as_of`, for
    the confirmation. `note` goes on every request declined.

    This is how a queue flooded with junk is cleared: one at a time, the
    500 requests SIGNUP_MAX_PENDING allows are 500 dialogs.
    """

    search = serializers.CharField(required=False, allow_blank=True, default="")
    preview = serializers.BooleanField(required=False, default=False)
    note = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        for name in ("search", "preview"):
            if attrs.get(name) and not attrs["all"]:
                raise serializers.ValidationError(
                    {name: ["Only applies with all: true."]}
                )
        return attrs
