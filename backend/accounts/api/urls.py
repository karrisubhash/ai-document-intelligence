from django.urls import path

from .views import (
    CsrfView,
    DemoView,
    LoginView,
    LogoutView,
    MeView,
    PasswordChangeView,
    SignupView,
    TokenView,
)

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    path("csrf/", CsrfView.as_view(), name="auth-csrf"),
    path("login/", LoginView.as_view(), name="auth-login"),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
    path("password/", PasswordChangeView.as_view(), name="auth-password"),
    path("token/", TokenView.as_view(), name="api-token-auth"),
    path("signup/", SignupView.as_view(), name="auth-signup"),
    # The one-click demo; the name is on GuestReadOnlyMiddleware's list of
    # public doors.
    path("demo/", DemoView.as_view(), name="auth-demo"),
]
