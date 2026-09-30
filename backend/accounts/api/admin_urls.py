from rest_framework.routers import DefaultRouter

from .views import DepartmentAdminViewSet, UserAdminViewSet

router = DefaultRouter(trailing_slash=True)
router.include_root_view = False
router.register("users", UserAdminViewSet, basename="admin-user")
router.register("departments", DepartmentAdminViewSet, basename="admin-department")

urlpatterns = router.urls
