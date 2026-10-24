from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AuditLogViewSet,
    BackupViewSet,
    BranchViewSet,
    CompanyProfileView,
    HealthCheckView,
    POSTerminalViewSet,
)

router = DefaultRouter()
router.register(r'branches', BranchViewSet, basename='branch')
router.register(r'pos-terminals', POSTerminalViewSet, basename='pos-terminal')
router.register(r'audit-logs', AuditLogViewSet, basename='audit-log')
router.register(r'backups', BackupViewSet, basename='backup')

urlpatterns = [
    path('healthz/', HealthCheckView.as_view(), name='health-check'),
    path('company-profile/', CompanyProfileView.as_view(), name='company-profile'),
    path('', include(router.urls)),
]