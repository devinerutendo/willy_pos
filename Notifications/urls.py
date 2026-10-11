from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import NotificationViewSet, NotificationPreferenceViewSet, NotificationLogViewSet

router = DefaultRouter()
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'preferences', NotificationPreferenceViewSet, basename='notification-preference')
router.register(r'logs', NotificationLogViewSet, basename='notification-log')

urlpatterns = [
    path('', include(router.urls)),
]
