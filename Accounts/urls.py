from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import UserViewSet, LoginView, LogoutView, UserProfileView, RegistrationStatusView, ChangePasswordView

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')

urlpatterns = [
    path('', include(router.urls)),
    path('registration-status/', RegistrationStatusView.as_view(), name='registration-status'),
    path('login/', LoginView.as_view(), name='api-login'),
    path('logout/', LogoutView.as_view(), name='api-logout'),
    path('profile/', UserProfileView.as_view(), name='api-profile'),
    path('change-password/', ChangePasswordView.as_view(), name='api-change-password'),
]
