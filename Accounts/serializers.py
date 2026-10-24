from rest_framework import serializers
from django.contrib.auth.hashers import make_password
from Core.models import Branch
from .models import User, UserSecurity


class UserSerializer(serializers.ModelSerializer):
    branch = serializers.PrimaryKeyRelatedField(
        queryset=Branch.objects.filter(is_active=True),
        required=False,
        allow_null=True
    )
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    permissions = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'phone', 'first_name', 
            'last_name', 'role', 'branch', 'branch_name', 'profile_image', 'permissions', 'is_active', 'is_staff', 
            'is_superuser', 'date_joined', 'last_login', 'password'
        ]
        extra_kwargs = {
            'password': {'write_only': True},
            'is_staff': {'read_only': True},
            'is_superuser': {'read_only': True},
            'date_joined': {'read_only': True},
            'last_login': {'read_only': True},
        }
        read_only_fields = ['is_staff', 'is_superuser', 'date_joined', 'last_login']

    def get_permissions(self, obj):
        return obj.permission_matrix

    def create(self, validated_data):
        validated_data['password'] = make_password(validated_data.get('password'))
        return super().create(validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        if password:
            validated_data['password'] = make_password(password)
        return super().update(instance, validated_data)


class UserSimpleSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'role', 'branch', 'branch_name', 'profile_image']


class LoginSerializer(serializers.Serializer):
    """
    Serializer for user login
    """
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class RegistrationSerializer(serializers.ModelSerializer):
    """
    Serializer for user registration
    """
    password_confirm = serializers.CharField(write_only=True)

    branch = serializers.PrimaryKeyRelatedField(
        queryset=Branch.objects.filter(is_active=True),
        required=False,
        allow_null=True
    )

    class Meta:
        model = User
        fields = [
            'username', 'email', 'phone', 'first_name', 
            'last_name', 'role', 'branch', 'password', 'password_confirm'
        ]
        extra_kwargs = {
            'password': {'write_only': True},
        }

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError({
                'password_confirm': 'Passwords do not match.'
            })
        return attrs

    def create(self, validated_data):
        validated_data.pop('password_confirm')
        validated_data['password'] = make_password(validated_data.get('password'))
        return super().create(validated_data)


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)
    new_password_confirm = serializers.CharField(write_only=True, min_length=8)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError({'new_password_confirm': 'New passwords do not match.'})
        return attrs


class AdminResetPasswordSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)


class SecuritySettingsSerializer(serializers.ModelSerializer):
    """
    Caller's security preferences. ``session_timeout_minutes`` of ``null`` turns
    the idle timeout off. Secrets and recovery-code hashes are never exposed.
    """
    session_timeout_minutes = serializers.IntegerField(
        min_value=UserSecurity.MIN_TIMEOUT_MINUTES,
        max_value=UserSecurity.MAX_TIMEOUT_MINUTES,
        required=False,
        allow_null=True,
    )
    session_timeout_enabled = serializers.BooleanField(read_only=True)
    recovery_codes_remaining = serializers.SerializerMethodField()

    class Meta:
        model = UserSecurity
        fields = [
            'two_factor_enabled',
            'recovery_codes_remaining',
            'session_timeout_enabled',
            'session_timeout_minutes',
            'last_activity_at',
        ]
        read_only_fields = ['two_factor_enabled', 'last_activity_at']

    def get_recovery_codes_remaining(self, obj):
        return len(obj.recovery_code_hashes or [])


class OtpCodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=12)


class PasswordConfirmSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True)


class DisableTwoFactorSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True)
    code = serializers.CharField(max_length=12)
