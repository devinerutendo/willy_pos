from rest_framework import serializers

from .models import License, LicenseRenewalAudit, MachineRegistration


class LicenseSerializer(serializers.ModelSerializer):
    """Serialize license information for API responses"""
    plan_display = serializers.CharField(source='get_plan_display', read_only=True)
    duration_display = serializers.CharField(source='get_subscription_duration_display', read_only=True)
    machine_limit_display = serializers.CharField(read_only=True)
    is_valid = serializers.BooleanField(read_only=True)
    is_expired = serializers.BooleanField(read_only=True)
    days_remaining = serializers.IntegerField(read_only=True)
    days_since_expired = serializers.IntegerField(read_only=True)
    registered_machine_count = serializers.IntegerField(read_only=True)
    machines_remaining = serializers.IntegerField(read_only=True, allow_null=True)
    license_state = serializers.SerializerMethodField()
    license_state_display = serializers.SerializerMethodField()
    allowed_capabilities = serializers.SerializerMethodField()
    blocked_capabilities = serializers.SerializerMethodField()

    class Meta:
        model = License
        fields = [
            'license_key',
            'plan',
            'plan_display',
            'subscription_duration',
            'duration_display',
            'machine_limit',
            'machine_limit_display',
            'is_active',
            'is_valid',
            'is_expired',
            'days_remaining',
            'days_since_expired',
            'registered_machine_count',
            'machines_remaining',
            'issued_at',
            'activated_at',
            'expires_at',
            'customer_name',
            'customer_email',
            'license_payload',
            'signature',
            'license_state',
            'license_state_display',
            'allowed_capabilities',
            'blocked_capabilities',
        ]
        read_only_fields = fields

    def get_license_state(self, obj):
        if not obj.is_expired:
            return 'active'

        days_since_expired = obj.days_since_expired
        if days_since_expired <= 15:
            return 'grace_period'
        if days_since_expired <= 45:
            return 'restricted_mode'
        return 'emergency_mode'

    def get_license_state_display(self, obj):
        state = self.get_license_state(obj)
        if state == 'active':
            return 'Active License'
        if state == 'grace_period':
            return 'Expired (0–15 days) — Grace Period'
        if state == 'restricted_mode':
            return 'Expired (16–45 days) — Restricted Mode'
        return 'Expired (45+ days) — Emergency Mode'

    def get_allowed_capabilities(self, obj):
        state = self.get_license_state(obj)
        if state == 'active':
            return ['Full access to all features']
        if state == 'grace_period':
            return ['Everything continues to work']
        if state == 'restricted_mode':
            return ['Sales', 'Returns', 'Receipt printing', 'Basic product lookup']
        return ['Sales', 'Returns', 'Receipt printing', 'View products']

    def get_blocked_capabilities(self, obj):
        state = self.get_license_state(obj)
        if state in {'active', 'grace_period'}:
            return []
        if state == 'restricted_mode':
            return [
                'Add users',
                'Add terminals',
                'Add branches',
                'Change system settings',
                'Access advanced reports',
                'Receive software updates',
            ]
        return [
            'Add users',
            'Add terminals',
            'Add branches',
            'Change system settings',
            'Access advanced reports',
            'Receive software updates',
        ]


class MachineRegistrationSerializer(serializers.ModelSerializer):
    """Serialize machine registration information"""
    class Meta:
        model = MachineRegistration
        fields = [
            'machine_fingerprint',
            'machine_name',
            'first_activation_date',
            'last_activation_date',
            'is_active',
        ]
        read_only_fields = fields


class LicenseActivationSerializer(serializers.Serializer):
    """Validate and deserialize license activation request"""
    license_key = serializers.CharField(max_length=255)
    machine_fingerprint = serializers.CharField(max_length=256)
    machine_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    signature = serializers.CharField()
    license_data = serializers.JSONField()

    def validate_license_key(self, value):
        if not value.startswith('LIC-'):
            raise serializers.ValidationError("Invalid license key format")
        return value


class TrialGenerationSerializer(serializers.Serializer):
    """Response for trial license generation"""
    license_key = serializers.CharField()
    plan = serializers.CharField()
    machine_fingerprint = serializers.CharField()
    days_remaining = serializers.IntegerField()
    expires_at = serializers.DateTimeField()
    message = serializers.CharField()


class LicenseStatusSerializer(serializers.Serializer):
    """Current license status for display on frontend"""
    has_license = serializers.BooleanField()
    license = LicenseSerializer(required=False, allow_null=True)
    registered_machines = MachineRegistrationSerializer(many=True)
    trial_active = serializers.BooleanField()
    can_add_machines = serializers.BooleanField()
    machines_used = serializers.IntegerField()
    machines_available = serializers.IntegerField(allow_null=True)
    machine_fingerprint = serializers.CharField()


class SignedRenewalSerializer(serializers.Serializer):
    license_key = serializers.CharField(max_length=255)
    renewal_payload = serializers.JSONField()
    signature = serializers.CharField()
    actor = serializers.CharField(max_length=255, required=False, allow_blank=True)
    machine_fingerprint = serializers.CharField(max_length=256, required=False, allow_blank=True)


class ManualExpiryExtensionSerializer(serializers.Serializer):
    license_key = serializers.CharField(max_length=255)
    new_expires_at = serializers.DateTimeField()
    reason = serializers.CharField()
    actor = serializers.CharField(max_length=255)
    machine_fingerprint = serializers.CharField(max_length=256, required=False, allow_blank=True)


class LicenseRenewalAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = LicenseRenewalAudit
        fields = [
            'id',
            'action',
            'old_expires_at',
            'new_expires_at',
            'reason',
            'actor',
            'machine_fingerprint',
            'metadata',
            'created_at',
        ]
        read_only_fields = fields
