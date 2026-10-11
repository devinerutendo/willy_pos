from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import License, LicenseRenewalAudit, MachineRegistration
from .utils import LicenseKeyGenerator, MachineFingerprint


PLAN_MACHINE_LIMITS = {
    'free_trial': 1,
    'starter': 1,
    'business': 5,
    'enterprise': None,
}

SUBSCRIPTION_DURATIONS = {
    'trial': timedelta(days=30),
    '3_months': timedelta(days=90),
    'yearly': timedelta(days=365),
    'lifetime': None,
}


def is_exempt_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in getattr(settings, 'LICENSE_EXEMPT_PATH_PREFIXES', ()))


def resolve_machine_limit(plan: str) -> Optional[int]:
    return PLAN_MACHINE_LIMITS.get(plan)


def resolve_expires_at(subscription_duration: str):
    delta = SUBSCRIPTION_DURATIONS.get(subscription_duration)
    if delta is None:
        return None
    return timezone.now() + delta


def normalize_machine_name(machine_name: Optional[str]) -> str:
    value = (machine_name or MachineFingerprint.get_machine_name() or 'Unknown').strip()
    return value[:255]


def normalize_machine_fingerprint(machine_fingerprint: Optional[str]) -> str:
    return (machine_fingerprint or MachineFingerprint.get_machine_fingerprint()).strip()


def build_license_payload(
    license_key: str,
    plan: str,
    subscription_duration: str,
    machine_limit: Optional[int],
    customer_name: str = '',
    customer_email: str = '',
) -> Dict[str, Any]:
    expires_at = resolve_expires_at(subscription_duration)
    return {
        'license_key': license_key,
        'plan': plan,
        'subscription_duration': subscription_duration,
        'machine_limit': machine_limit,
        'customer_name': customer_name,
        'customer_email': customer_email,
        'issued_at': timezone.now().isoformat(),
        'expires_at': expires_at.isoformat() if expires_at else None,
    }


def create_trial_license(machine_fingerprint: str, machine_name: str) -> Tuple[License, MachineRegistration, bool]:
    machine_fingerprint = normalize_machine_fingerprint(machine_fingerprint)
    machine_name = normalize_machine_name(machine_name)

    existing_trial = (
        License.objects.filter(
            plan='free_trial',
            is_active=True,
            registered_machines__machine_fingerprint=machine_fingerprint,
            registered_machines__is_active=True,
        )
        .order_by('-issued_at')
        .first()
    )
    if existing_trial:
        registration = existing_trial.registered_machines.filter(machine_fingerprint=machine_fingerprint, is_active=True).first()
        return existing_trial, registration, False

    license_key = LicenseKeyGenerator().generate_license_key()
    expires_at = resolve_expires_at('trial')
    payload = build_license_payload(
        license_key=license_key,
        plan='free_trial',
        subscription_duration='trial',
        machine_limit=1,
        customer_name=machine_name,
    )

    with transaction.atomic():
        license_obj = License.objects.create(
            license_key=license_key,
            plan='free_trial',
            subscription_duration='trial',
            machine_limit=1,
            license_payload=payload,
            signature='',
            activated_at=timezone.now(),
            expires_at=expires_at,
            customer_name=machine_name,
            is_active=True,
        )
        registration = MachineRegistration.objects.create(
            license=license_obj,
            machine_fingerprint=machine_fingerprint,
            machine_name=machine_name,
            is_active=True,
        )

    return license_obj, registration, True


def verify_license_payload(license_data: Dict[str, Any], signature: str) -> bool:
    generator = LicenseKeyGenerator(public_key_path=settings.LICENSE_PUBLIC_KEY_PATH)
    return generator.verify_signature(license_data, signature)


def activate_purchased_license(
    license_key: str,
    license_data: Dict[str, Any],
    signature: str,
    machine_fingerprint: str,
    machine_name: str,
) -> Tuple[License, MachineRegistration, bool]:
    machine_fingerprint = normalize_machine_fingerprint(machine_fingerprint)
    machine_name = normalize_machine_name(machine_name)

    if not verify_license_payload(license_data, signature):
        raise ValidationError('The license signature is invalid.')

    if license_data.get('license_key') != license_key:
        raise ValidationError('The supplied license key does not match the signed payload.')

    plan = license_data.get('plan')
    subscription_duration = license_data.get('subscription_duration')
    machine_limit = license_data.get('machine_limit')
    expires_at = license_data.get('expires_at')

    if plan not in dict(License.PLAN_CHOICES):
        raise ValidationError('Unsupported plan.')
    if subscription_duration not in dict(License.SUBSCRIPTION_DURATION_CHOICES):
        raise ValidationError('Unsupported subscription duration.')

    resolved_limit = resolve_machine_limit(plan)
    if resolved_limit is not None and machine_limit not in (None, resolved_limit):
        raise ValidationError('Machine limit does not match the selected plan.')

    parsed_expires_at = None
    if expires_at:
        parsed_expires_at = datetime.fromisoformat(expires_at)
        if timezone.is_naive(parsed_expires_at):
            parsed_expires_at = timezone.make_aware(parsed_expires_at, timezone.get_current_timezone())

    with transaction.atomic():
        license_obj, _ = License.objects.update_or_create(
            license_key=license_key,
            defaults={
                'plan': plan,
                'subscription_duration': subscription_duration,
                'machine_limit': resolved_limit,
                'license_payload': copy.deepcopy(license_data),
                'signature': signature,
                'is_active': True,
                'expires_at': parsed_expires_at,
                'customer_name': license_data.get('customer_name', ''),
                'customer_email': license_data.get('customer_email', ''),
                'activated_at': timezone.now(),
            },
        )

        if license_obj.machine_limit is not None and not license_obj.can_register_more_machines:
            existing_registration = license_obj.registered_machines.filter(
                machine_fingerprint=machine_fingerprint,
                is_active=True,
            ).first()
            if not existing_registration:
                raise ValidationError('Machine limit reached for this license.')

        registration, _ = MachineRegistration.objects.update_or_create(
            license=license_obj,
            machine_fingerprint=machine_fingerprint,
            defaults={
                'machine_name': machine_name,
                'is_active': True,
            },
        )

    return license_obj, registration, True


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    parsed = datetime.fromisoformat(value)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def apply_signed_license_renewal(
    license_key: str,
    renewal_payload: Dict[str, Any],
    signature: str,
    actor: str = '',
    machine_fingerprint: str = '',
) -> License:
    if not verify_license_payload(renewal_payload, signature):
        raise ValidationError('The renewal signature is invalid.')

    payload_license_key = renewal_payload.get('license_key')
    if payload_license_key != license_key:
        raise ValidationError('The supplied license key does not match the signed renewal payload.')

    expires_at = _parse_iso_datetime(renewal_payload.get('expires_at'))
    if expires_at is None:
        raise ValidationError('Renewal payload must include a valid expires_at timestamp.')

    with transaction.atomic():
        try:
            license_obj = License.objects.select_for_update().get(license_key=license_key)
        except License.DoesNotExist as exc:
            raise ValidationError('License not found.') from exc

        old_expires_at = license_obj.expires_at

        if license_obj.subscription_duration == 'lifetime':
            raise ValidationError('Lifetime licenses do not require renewal.')

        if expires_at <= timezone.now():
            raise ValidationError('Renewal expiration must be a future date.')

        # Refresh signed payload fields while preserving unrelated metadata.
        updated_payload = copy.deepcopy(license_obj.license_payload or {})
        updated_payload.update(copy.deepcopy(renewal_payload))

        license_obj.expires_at = expires_at
        license_obj.signature = signature
        license_obj.license_payload = updated_payload
        license_obj.is_active = True
        license_obj.save(update_fields=['expires_at', 'signature', 'license_payload', 'is_active'])

        LicenseRenewalAudit.objects.create(
            license=license_obj,
            action=LicenseRenewalAudit.ACTION_SIGNED_RENEWAL,
            old_expires_at=old_expires_at,
            new_expires_at=license_obj.expires_at,
            actor=actor,
            machine_fingerprint=machine_fingerprint,
            metadata={
                'payload_keys': sorted(list(renewal_payload.keys())),
            },
        )

    return license_obj


def manually_extend_license_expiry(
    license_key: str,
    new_expires_at: datetime,
    reason: str,
    actor: str,
    machine_fingerprint: str = '',
) -> License:
    if not reason.strip():
        raise ValidationError('Reason is required for manual expiry extension.')

    if not actor.strip():
        raise ValidationError('Actor is required for manual expiry extension.')

    if timezone.is_naive(new_expires_at):
        new_expires_at = timezone.make_aware(new_expires_at, timezone.get_current_timezone())

    if new_expires_at <= timezone.now():
        raise ValidationError('New expiration must be in the future.')

    with transaction.atomic():
        try:
            license_obj = License.objects.select_for_update().get(license_key=license_key)
        except License.DoesNotExist as exc:
            raise ValidationError('License not found.') from exc

        if license_obj.subscription_duration == 'lifetime':
            raise ValidationError('Lifetime licenses do not require expiry extension.')

        old_expires_at = license_obj.expires_at
        license_obj.expires_at = new_expires_at
        license_obj.is_active = True

        updated_payload = copy.deepcopy(license_obj.license_payload or {})
        updated_payload['expires_at'] = new_expires_at.isoformat()
        license_obj.license_payload = updated_payload

        license_obj.save(update_fields=['expires_at', 'is_active', 'license_payload'])

        LicenseRenewalAudit.objects.create(
            license=license_obj,
            action=LicenseRenewalAudit.ACTION_MANUAL_EXTENSION,
            old_expires_at=old_expires_at,
            new_expires_at=license_obj.expires_at,
            reason=reason.strip(),
            actor=actor.strip(),
            machine_fingerprint=machine_fingerprint,
            metadata={'mode': 'manual_db_update_path'},
        )

    return license_obj


def get_active_license_for_machine(machine_fingerprint: str) -> Optional[License]:
    machine_fingerprint = normalize_machine_fingerprint(machine_fingerprint)
    return (
        License.objects.filter(
            is_active=True,
            registered_machines__machine_fingerprint=machine_fingerprint,
            registered_machines__is_active=True,
        )
        .order_by('-activated_at', '-issued_at')
        .first()
    )


def build_license_status(machine_fingerprint: str) -> Dict[str, Any]:
    machine_fingerprint = normalize_machine_fingerprint(machine_fingerprint)
    license_obj = get_active_license_for_machine(machine_fingerprint)

    if not license_obj or license_obj.is_expired:
        if not license_obj:
            return {
                'has_license': False,
                'license': None,
                'license_obj': None,
                'registered_machines': [],
                'trial_active': False,
                'can_add_machines': False,
                'machines_used': 0,
                'machines_available': None,
                'machine_fingerprint': machine_fingerprint,
                'license_state': 'none',
                'license_state_display': 'No Active License',
                'allowed_capabilities': [],
                'blocked_capabilities': [
                    'Sales',
                    'Returns',
                    'Receipt printing',
                    'View products',
                    'Add users',
                    'Add terminals',
                    'Add branches',
                    'Change system settings',
                    'Access advanced reports',
                    'Receive software updates',
                ],
            }

        if license_obj.days_since_expired <= 15:
            license_state = 'grace_period'
            license_state_display = 'Expired (0–15 days) — Grace Period'
            allowed_capabilities = ['Everything continues to work']
            blocked_capabilities = []
        elif license_obj.days_since_expired <= 45:
            license_state = 'restricted_mode'
            license_state_display = 'Expired (16–45 days) — Restricted Mode'
            allowed_capabilities = ['Sales', 'Returns', 'Receipt printing', 'Basic product lookup']
            blocked_capabilities = [
                'Add users',
                'Add terminals',
                'Add branches',
                'Change system settings',
                'Access advanced reports',
                'Receive software updates',
            ]
        else:
            license_state = 'emergency_mode'
            license_state_display = 'Expired (45+ days) — Emergency Mode'
            allowed_capabilities = ['Sales', 'Returns', 'Receipt printing', 'View products']
            blocked_capabilities = [
                'Add users',
                'Add terminals',
                'Add branches',
                'Change system settings',
                'Access advanced reports',
                'Receive software updates',
            ]

        return {
            'has_license': True,
            'license': license_obj,
            'license_obj': license_obj,
            'registered_machines': license_obj.registered_machines.filter(is_active=True),
            'trial_active': license_obj.plan == 'free_trial',
            'can_add_machines': False,
            'machines_used': license_obj.registered_machine_count,
            'machines_available': license_obj.machines_remaining,
            'machine_fingerprint': machine_fingerprint,
            'license_state': license_state,
            'license_state_display': license_state_display,
            'allowed_capabilities': allowed_capabilities,
            'blocked_capabilities': blocked_capabilities,
        }

    registrations = license_obj.registered_machines.filter(is_active=True)
    machines_used = registrations.count()
    machines_available = None if license_obj.machine_limit is None else max(0, license_obj.machine_limit - machines_used)

    return {
        'has_license': True,
        'license': license_obj,
        'license_obj': license_obj,
        'registered_machines': registrations,
        'trial_active': license_obj.plan == 'free_trial',
        'can_add_machines': license_obj.machine_limit is None or machines_available > 0,
        'machines_used': machines_used,
        'machines_available': machines_available,
        'machine_fingerprint': machine_fingerprint,
        'license_state': 'active',
        'license_state_display': 'Active License',
        'allowed_capabilities': ['Full access to all features'],
        'blocked_capabilities': [],
    }