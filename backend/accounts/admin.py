from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from .models import SignupRequest, UserProfile

User = get_user_model()


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    # Set only by accounts.demo when it makes a guest. Ticking it here would
    # make a member read-only; unticking it would leave an anonymous,
    # passwordless account outside the guest guard and the purge.
    readonly_fields = ("is_guest",)


class ProfileAwareUserAdmin(UserAdmin):
    inlines = [UserProfileInline]
    list_display = UserAdmin.list_display + ("role", "department", "is_guest")
    list_filter = UserAdmin.list_filter + ("profile__is_guest",)

    def get_inlines(self, request, obj):
        # Not on the add page. Saving the new user creates its profile (the
        # post_save signal in accounts.signals), and the inline then inserted
        # a second one for the same user - an IntegrityError whenever a role
        # or department was filled in. Django's add flow continues on the
        # change page, where the inline edits the profile the signal made.
        return self.inlines if obj is not None else []

    @admin.display(description="Role")
    def role(self, user):
        return getattr(user.profile, "role", "")

    @admin.display(description="Department")
    def department(self, user):
        return getattr(user.profile, "department", "")

    @admin.display(description="Demo guest", boolean=True)
    def is_guest(self, user):
        return getattr(user.profile, "is_guest", False)


admin.site.unregister(User)
admin.site.register(User, ProfileAwareUserAdmin)


@admin.register(SignupRequest)
class SignupRequestAdmin(admin.ModelAdmin):
    """Read-only. Approval creates the account and audits the decision
    (accounts.services.approve_signup); flipping the status here would do
    neither, and would leave an "approved" applicant with no account.
    Deleting is refused too: a deleted request leaves no SIGNUP_REJECT row,
    frees its name and turns the applicant's "declined" into "wrong
    password". A flood is cleared with the application's bulk decline."""

    list_display = ("username", "email", "department", "role", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("username", "email", "first_name", "last_name")
    exclude = ("password",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
