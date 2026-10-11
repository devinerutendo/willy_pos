from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class License(models.Model):
    PLAN_CHOICES = [
        ('free_trial', 'Free Trial'),
        ('starter', 'Starter'),
        ('business', 'Business'),
        ('enterprise', 'Enterprise'),
    ]

    SUBSCRIPTION_DURATION_CHOICES = [
        ('trial', '1 Month Trial'),
        ('3_months', '3 Months'),
        ('yearly', 'Yearly'),
        ('lifetime', 'Lifetime'),
    ]

    PLAN_MACHINE_LIMITS = {
        'free_trial': 1,
        'starter': 1,
        'business': 5,
        'enterprise': None,
    }

    VALID_DURATIONS = {
        'free_trial': {'trial'},
        'starter': {'3_months', 'yearly', 'lifetime'},
        'business': {'3_months', 'yearly', 'lifetime'},
        'enterprise': {'3_months', 'yearly', 'lifetime'},
    }

    license_key = models.CharField(max_length=255, unique=True, db_index=True)
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES)
    subscription_duration = models.CharField(max_length=20, choices=SUBSCRIPTION_DURATION_CHOICES)
    machine_limit = models.PositiveIntegerField(null=True, blank=True)
    license_payload = models.JSONField(default=dict, blank=True)
    signature = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    customer_name = models.CharField(max_length=255, blank=True)
    customer_email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'licensing_license'
        ordering = ['-issued_at']
        indexes = [
            models.Index(fields=['license_key', 'is_active']),
            models.Index(fields=['plan', 'is_active']),
            models.Index(fields=['expires_at', 'is_active']),
        ]

    def __str__(self):
        return f'{self.get_plan_display()} - {self.license_key[:8]}...'

    @property
    def is_expired(self):
        if self.subscription_duration == 'lifetime':
            return False
        return bool(self.expires_at and timezone.now() > self.expires_at)

    @property
    def days_remaining(self):
        if self.subscription_duration == 'lifetime' or self.is_expired or not self.expires_at:
            return 0
        return max(0, (self.expires_at - timezone.now()).days)

    @property
    def days_since_expired(self):
        if self.subscription_duration == 'lifetime' or not self.expires_at or not self.is_expired:
            return 0
        return max(0, (timezone.now() - self.expires_at).days)

    @property
    def is_valid(self):
        return self.is_active and not self.is_expired

    @property
    def machine_limit_display(self):
        return 'Unlimited' if self.machine_limit is None else str(self.machine_limit)

    @property
    def registered_machine_count(self):
        return self.registered_machines.filter(is_active=True).count()

    @property
    def machines_remaining(self):
        if self.machine_limit is None:
            return None
        return max(0, self.machine_limit - self.registered_machine_count)

    @property
    def can_register_more_machines(self):
        return self.machine_limit is None or self.registered_machine_count < self.machine_limit

    def clean(self):
        allowed_durations = self.VALID_DURATIONS.get(self.plan, set())
        if self.subscription_duration not in allowed_durations:
            raise ValidationError('Selected subscription duration is not allowed for this plan.')

        plan_limit = self.PLAN_MACHINE_LIMITS.get(self.plan)
        if self.plan == 'enterprise':
            self.machine_limit = None
        elif self.machine_limit is None:
            self.machine_limit = plan_limit
        elif plan_limit is not None and self.machine_limit != plan_limit:
            raise ValidationError('Machine limit does not match the selected plan.')

        if self.subscription_duration == 'lifetime':
            self.expires_at = None
        elif not self.expires_at:
            raise ValidationError('Non-lifetime licenses must have an expiration date.')

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class MachineRegistration(models.Model):
    license = models.ForeignKey(License, on_delete=models.CASCADE, related_name='registered_machines')
    machine_fingerprint = models.CharField(max_length=256)
    machine_name = models.CharField(max_length=255)
    first_activation_date = models.DateTimeField(auto_now_add=True)
    last_activation_date = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'licensing_machine_registration'
        unique_together = ('license', 'machine_fingerprint')
        indexes = [
            models.Index(fields=['license', 'is_active']),
            models.Index(fields=['machine_fingerprint', 'is_active']),
        ]

    def __str__(self):
        return f'{self.machine_name} ({self.machine_fingerprint[:8]}...)'


class LicenseRenewalAudit(models.Model):
    ACTION_SIGNED_RENEWAL = 'signed_renewal'
    ACTION_MANUAL_EXTENSION = 'manual_extension'

    ACTION_CHOICES = [
        (ACTION_SIGNED_RENEWAL, 'Signed Renewal'),
        (ACTION_MANUAL_EXTENSION, 'Manual Extension'),
    ]

    license = models.ForeignKey(License, on_delete=models.CASCADE, related_name='renewal_audits')
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True)
    old_expires_at = models.DateTimeField(null=True, blank=True)
    new_expires_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    actor = models.CharField(max_length=255, blank=True)
    machine_fingerprint = models.CharField(max_length=256, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'licensing_license_renewal_audit'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['license', 'action']),
            models.Index(fields=['created_at', 'action']),
        ]

    def __str__(self):
        return f'{self.license.license_key} - {self.action} - {self.created_at.isoformat()}'
