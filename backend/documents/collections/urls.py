from rest_framework.routers import DefaultRouter

from .views import CollectionViewSet

router = DefaultRouter()
router.register("", CollectionViewSet, basename="collection")

urlpatterns = router.urls
