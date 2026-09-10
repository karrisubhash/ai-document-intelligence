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

    @admin.display(description="Role")
    def role(self, user):
        return getattr(user.profile, "role", "")

    @admin.display(description="Department")
    def department(self, user):
        return getattr(user.profile, "department", "")


admin.site.unregister(User)
admin.site.register(User, ProfileAwareUserAdmin)
