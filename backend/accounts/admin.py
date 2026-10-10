from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from .models import UserProfile

User = get_user_model()


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False


class ProfileAwareUserAdmin(UserAdmin):
    inlines = [UserProfileInline]
    list_display = UserAdmin.list_display + ("role", "department")

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


admin.site.unregister(User)
admin.site.register(User, ProfileAwareUserAdmin)
