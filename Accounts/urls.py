from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    UserViewSet, LoginView, LogoutView, UserProfileView, RegistrationStatusView, ChangePasswordView,
    SecuritySettingsView, TwoFactorSetupView, TwoFactorEnableView, TwoFactorDisableView,
)

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')

urlpatterns = [
    path('', include(router.urls)),
    path('registration-status/', RegistrationStatusView.as_view(), name='registration-status'),
    path('login/', LoginView.as_view(), name='api-login'),
    path('logout/', LogoutView.as_view(), name='api-logout'),
    path('profile/', UserProfileView.as_view(), name='api-profile'),
    path('change-password/', ChangePasswordView.as_view(), name='api-change-password'),
    path('security/', SecuritySettingsView.as_view(), name='api-security'),
    path('security/2fa/setup/', TwoFactorSetupView.as_view(), name='api-2fa-setup'),
    path('security/2fa/enable/', TwoFactorEnableView.as_view(), name='api-2fa-enable'),
    path('security/2fa/disable/', TwoFactorDisableView.as_view(), name='api-2fa-disable'),
]
