from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import LicenseViewSet

router = DefaultRouter()
router.register(r'', LicenseViewSet, basename='license')

urlpatterns = router.urls
