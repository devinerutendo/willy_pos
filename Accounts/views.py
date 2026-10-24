from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied
from rest_framework.throttling import ScopedRateThrottle
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.utils import timezone
from Core.access import branch_scoped_queryset
from Core.permissions import ScopePermission
from .models import User, UserSecurity
from .security import (
    encrypt_secret,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_code,
    provisioning_uri,
    verify_second_factor,
    verify_totp,
    decrypt_secret,
)
from .serializers import (
    UserSerializer,
    RegistrationSerializer,
    ChangePasswordSerializer,
    AdminResetPasswordSerializer,
    SecuritySettingsSerializer,
    OtpCodeSerializer,
    PasswordConfirmSerializer,
    DisableTwoFactorSerializer,
)

User = get_user_model()
DEFAULT_RESET_PASSWORD = 'Willypos#2026'


def superuser_exists() -> bool:
    return User.objects.filter(is_superuser=True).exists()


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated, ScopePermission]
    permission_scope = 'users'

    def get_permissions(self):
        """
        Allow unauthenticated registration until a bootstrap superuser exists.
        """
        if self.action == 'create':
            if not superuser_exists():
                return [permissions.AllowAny()]
            return [permissions.IsAuthenticated()]
        return [permission() for permission in self.permission_classes]

    def get_queryset(self):
        user = self.request.user
        queryset = User.objects.select_related('branch').all()
        if user.is_authenticated and user.is_manager:
            return queryset
        if user.is_authenticated and user.branch_id:
            return branch_scoped_queryset(queryset, user)
        if user.is_authenticated:
            return queryset.filter(id=user.id)
        return queryset.none()

    def get_serializer_class(self):
        if self.action == 'create':
            return RegistrationSerializer
        return self.serializer_class

    def perform_create(self, serializer):
        # Bootstrap flow: first public account becomes system administrator.
        if not superuser_exists():
            user = serializer.save(
                role=User.ADMIN,
                is_staff=True,
                is_superuser=True,
            )
            Token.objects.create(user=user)
            return

        # After bootstrap, only authenticated administrators can create users.
        request_user = self.request.user
        if not request_user.is_authenticated:
            raise PermissionDenied('Public registration is disabled. Please contact the system administrator.')

        is_system_admin = bool(request_user.is_superuser) or request_user.role == User.ADMIN
        if not is_system_admin:
            raise PermissionDenied('Only the system administrator can register new users.')

        user = serializer.save()
        Token.objects.create(user=user)

    @action(detail=True, methods=['post'])
    def admin_reset_password(self, request, pk=None):
        target_user = self.get_object()

        is_system_admin = bool(request.user.is_superuser) or request.user.role == User.ADMIN
        if not is_system_admin:
            raise PermissionDenied('Only the system administrator can reset user passwords.')

        serializer = AdminResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        target_user.set_password(DEFAULT_RESET_PASSWORD)
        target_user.save(update_fields=['password'])

        # Revoke existing sessions so the reset takes immediate effect.
        Token.objects.filter(user=target_user).delete()

        return Response(
            {
                'message': 'Password reset approved and applied.',
                'user_id': target_user.id,
                'temporary_password': DEFAULT_RESET_PASSWORD,
            },
            status=status.HTTP_200_OK,
        )


class RegistrationStatusView(APIView):
    """
    Public endpoint used by frontend to determine if bootstrap registration is available.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from django.db import DatabaseError
        try:
            has_users = User.objects.exists()
            has_superuser = superuser_exists()
        except DatabaseError:
            has_users = False
            has_superuser = False

        return Response(
            {
                'allow_public_signup': not has_superuser,
                'has_users': has_users,
                'has_superuser': has_superuser,
            },
            status=status.HTTP_200_OK,
        )


class LoginView(APIView):
    """
    Custom login view that accepts email instead of username
    """
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')
        otp = request.data.get('otp')

        if not email or not password:
            return Response(
                {'error': 'Email and password are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Try to find user by email
        try:
            user = User.objects.select_related('branch').get(email=email)
        except User.DoesNotExist:
            return Response(
                {'error': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Directly check password (more reliable with custom user model)
        if not check_password(password, user.password):
            return Response(
                {'error': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Check if user is active
        if not user.is_active:
            return Response(
                {'error': 'User account is disabled.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Second factor (only reached once the password is correct).
        security = UserSecurity.objects.filter(user=user, two_factor_enabled=True).first()
        if security:
            if not otp:
                return Response(
                    {'error': 'Enter the 6-digit code from your authenticator app.', 'code': 'two_factor_required'},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            with transaction.atomic():
                # Lock the row so one code cannot be accepted by two concurrent logins.
                locked = UserSecurity.objects.select_for_update().get(pk=security.pk)
                valid = verify_second_factor(locked, str(otp))
            if not valid:
                return Response(
                    {'error': 'Invalid or expired authentication code.', 'code': 'two_factor_invalid'},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

        # Enforce one active session per user by rotating the token on each login.
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)
        _reset_session_activity(user)

        return Response({
            'token': token.key,
            'user': UserSerializer(user).data
        })


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Delete the token to logout
        if hasattr(request.user, 'auth_token') and request.user.auth_token:
            request.user.auth_token.delete()
        
        # Also delete all tokens for this user (for multiple devices)
        Token.objects.filter(user=request.user).delete()
        
        return Response(
            {"message": "Successfully logged out."},
            status=status.HTTP_200_OK
        )


class UserProfileView(APIView):
    """
    Get and update current user profile
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        old_password = serializer.validated_data['old_password']
        new_password = serializer.validated_data['new_password']

        if not check_password(old_password, request.user.password):
            return Response({'error': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)

        request.user.set_password(new_password)
        request.user.save(update_fields=['password'])

        Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)
        _reset_session_activity(request.user)

        return Response(
            {
                'message': 'Password updated successfully.',
                'token': token.key,
            },
            status=status.HTTP_200_OK,
        )


def _reset_session_activity(user):
    """A freshly issued token starts a new idle period."""
    UserSecurity.objects.filter(user=user).update(last_activity_at=timezone.now())


def _get_security(user) -> UserSecurity:
    security, _ = UserSecurity.objects.get_or_create(user=user)
    return security


class SecuritySettingsView(APIView):
    """Read and update the caller's security preferences."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(SecuritySettingsSerializer(_get_security(request.user)).data)

    def patch(self, request):
        security = _get_security(request.user)
        serializer = SecuritySettingsSerializer(security, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class _PasswordProtectedView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _bad_password():
        return Response({'error': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)


class TwoFactorSetupView(_PasswordProtectedView):
    """Start enrolment: generate a pending secret and return it for the authenticator app."""

    def post(self, request):
        serializer = PasswordConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not request.user.check_password(serializer.validated_data['password']):
            return self._bad_password()

        security = _get_security(request.user)
        if security.two_factor_enabled:
            return Response(
                {'error': 'Two-factor authentication is already enabled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        secret = generate_totp_secret()
        security.pending_totp_secret = encrypt_secret(secret)
        security.save(update_fields=['pending_totp_secret'])
        return Response(
            {
                'secret': secret,
                'otpauth_uri': provisioning_uri(secret, request.user.email),
            }
        )


class TwoFactorEnableView(_PasswordProtectedView):
    """Finish enrolment by proving the authenticator app produces valid codes."""

    def post(self, request):
        serializer = OtpCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            security = UserSecurity.objects.select_for_update().get_or_create(user=request.user)[0]
            if security.two_factor_enabled:
                return Response(
                    {'error': 'Two-factor authentication is already enabled.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not security.pending_totp_secret:
                return Response(
                    {'error': 'Start setup first.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            step = verify_totp(
                decrypt_secret(security.pending_totp_secret),
                serializer.validated_data['code'],
            )
            if step is None:
                return Response(
                    {'error': 'Invalid or expired authentication code.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            recovery_codes = generate_recovery_codes()
            security.totp_secret = security.pending_totp_secret
            security.pending_totp_secret = ''
            security.two_factor_enabled = True
            security.last_totp_step = step
            security.recovery_code_hashes = [hash_recovery_code(code) for code in recovery_codes]
            security.save()

        data = SecuritySettingsSerializer(security).data
        # Shown exactly once; only hashes are kept.
        data['recovery_codes'] = recovery_codes
        return Response(data)


class TwoFactorDisableView(_PasswordProtectedView):
    """Turning 2FA off needs both the password and a current authenticator or recovery code."""

    def post(self, request):
        serializer = DisableTwoFactorSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not request.user.check_password(serializer.validated_data['password']):
            return self._bad_password()

        with transaction.atomic():
            security = UserSecurity.objects.select_for_update().get_or_create(user=request.user)[0]
            if not security.two_factor_enabled:
                return Response(
                    {'error': 'Two-factor authentication is not enabled.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if not verify_second_factor(security, serializer.validated_data['code']):
                return Response(
                    {'error': 'Invalid or expired authentication code.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            security.two_factor_enabled = False
            security.totp_secret = ''
            security.pending_totp_secret = ''
            security.last_totp_step = 0
            security.recovery_code_hashes = []
            security.save()

        return Response(SecuritySettingsSerializer(security).data)
