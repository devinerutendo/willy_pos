from django.db import transaction
from rest_framework import serializers

from .models import AuditLog, Branch, CompanyProfile, POSTerminal

# Logo upload limits. Receipts and PDFs embed the logo, so keep it small.
LOGO_MAX_BYTES = 1024 * 1024
LOGO_MAX_DIMENSION = 4096
TERMS_MAX_LENGTH = 4000
# Detected image format -> canonical file extension. The stored extension is
# derived from the real content, never from the client-supplied filename, so a
# file cannot be stored (and later served) under a misleading extension.
LOGO_FORMATS = {'PNG': 'png', 'JPEG': 'jpg', 'GIF': 'gif', 'WEBP': 'webp'}


# Registration and payment fields are printed as single lines on documents.
SINGLE_LINE_FIELDS = (
    'zimra_tin', 'local_authority_licence', 'praz_registration_number',
    'payment_account_name', 'payment_bank', 'payment_account_number', 'payment_phone',
)


def validate_single_line(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise serializers.ValidationError('This value must be a single line of text.')


class CompanyProfileSerializer(serializers.ModelSerializer):
    """Business identity printed on receipts, quotations and other documents."""

    # Declared explicitly: the model default would otherwise make it optional,
    # letting a PUT (or an empty multipart value) silently skip the name.
    name = serializers.CharField(max_length=150)
    logo = serializers.ImageField(required=False, allow_null=False)
    remove_logo = serializers.BooleanField(write_only=True, required=False, default=False)
    updated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CompanyProfile
        fields = [
            'name', 'tagline', 'terms', 'address', 'city', 'phone', 'email',
            'zimra_tin', 'local_authority_licence', 'praz_registration_number',
            'payment_account_name', 'payment_bank', 'payment_account_number',
            'payment_phone',
            'logo', 'remove_logo', 'updated_at', 'updated_by_name',
        ]
        read_only_fields = ['updated_at']
        extra_kwargs = {
            field: {'validators': [validate_single_line]} for field in SINGLE_LINE_FIELDS
        }

    def get_updated_by_name(self, obj):
        user = obj.updated_by
        if not user:
            return None
        return user.get_full_name() or user.email

    def validate_terms(self, value):
        if len(value) > TERMS_MAX_LENGTH:
            raise serializers.ValidationError(
                f'Terms must be at most {TERMS_MAX_LENGTH} characters.'
            )
        return value

    def validate_logo(self, value):
        if value.size > LOGO_MAX_BYTES:
            raise serializers.ValidationError(
                f'Logo must be {LOGO_MAX_BYTES // (1024 * 1024)} MB or smaller.'
            )

        image = getattr(value, 'image', None)
        extension = LOGO_FORMATS.get(getattr(image, 'format', None))
        if extension is None:
            raise serializers.ValidationError('Logo must be a PNG, JPEG, GIF or WebP image.')

        width, height = image.size
        if max(width, height) > LOGO_MAX_DIMENSION:
            raise serializers.ValidationError(
                f'Logo must be at most {LOGO_MAX_DIMENSION}x{LOGO_MAX_DIMENSION} pixels.'
            )

        value.name = f'logo.{extension}'
        return value

    def validate(self, attrs):
        if attrs.get('remove_logo') and attrs.get('logo') is not None:
            raise serializers.ValidationError(
                {'logo': 'Upload a new logo or remove the current one, not both.'}
            )
        return attrs

    def update(self, instance, validated_data):
        remove_logo = validated_data.pop('remove_logo', False)
        new_logo = validated_data.get('logo')
        previous_logo = instance.logo.name if instance.logo else None
        if remove_logo:
            validated_data['logo'] = None

        instance = super().update(instance, validated_data)

        # Delete the replaced file only once the new state is committed, so a
        # rolled-back request never loses the logo that is still referenced.
        if previous_logo and (remove_logo or new_logo is not None):
            storage = CompanyProfile._meta.get_field('logo').storage
            transaction.on_commit(lambda: storage.delete(previous_logo))
        return instance


class BranchSerializer(serializers.ModelSerializer):
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Branch
        fields = [
            'id', 'name', 'code', 'address', 'phone', 'email', 'is_active',
            'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by']


class POSTerminalSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    assigned_user_name = serializers.CharField(source='assigned_user.full_name', read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = POSTerminal
        fields = [
            'id', 'name', 'code', 'branch', 'branch_name', 'description', 'assigned_user',
            'assigned_user_name', 'is_active', 'last_seen_at', 'created_at', 'updated_at',
            'created_by', 'updated_by'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by', 'last_seen_at']


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source='actor.full_name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            'id', 'action', 'model_label', 'object_pk', 'object_repr', 'changes',
            'actor', 'actor_name', 'branch', 'branch_name', 'created_at'
        ]
        read_only_fields = fields