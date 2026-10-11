from django.contrib import admin
from .models import License, LicenseRenewalAudit, MachineRegistration


@admin.register(License)
class LicenseAdmin(admin.ModelAdmin):
    list_display = [
        'license_key',
        'plan',
        'subscription_duration',
        'customer_name',
        'is_active',
        'is_valid',
        'days_remaining',
        'machine_limit_display',
        'registered_machine_count',
        'issued_at',
        'expires_at'
    ]
    list_filter = [
        'plan',
        'subscription_duration',
        'is_active',
        'issued_at',
        'activated_at'
    ]
    search_fields = [
        'license_key',
        'machine_fingerprint',
        'customer_name',
        'customer_email'
    ]
    readonly_fields = [
        'license_key',
        'signature',
        'license_payload',
        'issued_at',
        'activated_at',
    ]

    fieldsets = (
        ('License Information', {
            'fields': ('license_key', 'plan', 'subscription_duration', 'machine_limit')
        }),
        ('Status', {
            'fields': ('is_active', 'issued_at', 'activated_at', 'expires_at')
        }),
        ('Payload', {
            'fields': ('license_payload',)
        }),
        ('Customer', {
            'fields': ('customer_name', 'customer_email')
        }),
        ('Security', {
            'fields': ('signature',),
            'classes': ('collapse',)
        }),
        ('Notes', {
            'fields': ('notes',),
            'classes': ('collapse',)
        })
    )

    def is_valid(self, obj):
        return obj.is_valid
    is_valid.boolean = True
    is_valid.short_description = 'Valid'

    def days_remaining(self, obj):
        return obj.days_remaining
    days_remaining.short_description = 'Days Remaining'


@admin.register(MachineRegistration)
class MachineRegistrationAdmin(admin.ModelAdmin):
    list_display = [
        'machine_name',
        'license',
        'first_activation_date',
        'last_activation_date',
        'is_active'
    ]
    list_filter = [
        'is_active',
        'first_activation_date',
        'last_activation_date'
    ]
    search_fields = [
        'machine_name',
        'machine_fingerprint',
        'license__license_key'
    ]
    readonly_fields = [
        'machine_fingerprint',
        'first_activation_date',
        'last_activation_date'
    ]


@admin.register(LicenseRenewalAudit)
class LicenseRenewalAuditAdmin(admin.ModelAdmin):
    list_display = [
        'license',
        'action',
        'old_expires_at',
        'new_expires_at',
        'actor',
        'created_at',
    ]
    list_filter = [
        'action',
        'created_at',
    ]
    search_fields = [
        'license__license_key',
        'actor',
        'machine_fingerprint',
        'reason',
    ]
    readonly_fields = [
        'license',
        'action',
        'old_expires_at',
        'new_expires_at',
        'reason',
        'actor',
        'machine_fingerprint',
        'metadata',
        'created_at',
    ]
