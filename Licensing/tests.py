from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from unittest.mock import patch

from .middleware import LicenseEnforcementMiddleware
from .models import License, LicenseRenewalAudit
from .services import (
    activate_purchased_license,
    apply_signed_license_renewal,
    create_trial_license,
    manually_extend_license_expiry,
)
from .views import LicenseViewSet


class TrialLicenseTests(TestCase):
    def test_trial_license_is_one_machine_for_one_month(self):
        license_obj, registration, created = create_trial_license(
            machine_fingerprint='machine-a',
            machine_name='POS-01',
        )

        self.assertTrue(created)
        self.assertEqual(license_obj.plan, 'free_trial')
        self.assertEqual(license_obj.subscription_duration, 'trial')
        self.assertEqual(license_obj.machine_limit, 1)
        self.assertEqual(license_obj.registered_machine_count, 1)
        self.assertEqual(registration.machine_fingerprint, 'machine-a')
        self.assertTrue(license_obj.is_valid)

    def test_trial_license_reuses_existing_machine_registration(self):
        first_license, first_registration, first_created = create_trial_license(
            machine_fingerprint='machine-a',
            machine_name='POS-01',
        )
        second_license, second_registration, second_created = create_trial_license(
            machine_fingerprint='machine-a',
            machine_name='POS-01-renamed',
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_license.pk, second_license.pk)
        self.assertEqual(first_registration.pk, second_registration.pk)
        self.assertEqual(License.objects.count(), 1)


class PurchasedLicenseMachineLimitTests(TestCase):
    def setUp(self):
        self.license_data = {
            'license_key': 'LIC-TEST-0001',
            'plan': 'business',
            'subscription_duration': 'yearly',
            'machine_limit': 5,
            'customer_name': 'Acme Ltd',
            'customer_email': 'owner@example.com',
            'issued_at': '2026-06-23T00:00:00+00:00',
            'expires_at': '2027-06-23T00:00:00+00:00',
        }

    @patch('Licensing.services.verify_license_payload', return_value=True)
    def test_business_plan_allows_five_registered_machines(self, mock_verify):
        for index in range(5):
            license_obj, registration, created = activate_purchased_license(
                license_key=self.license_data['license_key'],
                license_data=self.license_data,
                signature='signature',
                machine_fingerprint=f'machine-{index}',
                machine_name=f'POS-{index}',
            )
            self.assertEqual(license_obj.machine_limit, 5)
            self.assertTrue(registration.is_active)
            self.assertTrue(created)

        license_obj = License.objects.get(license_key=self.license_data['license_key'])
        self.assertEqual(license_obj.registered_machine_count, 5)
        self.assertFalse(license_obj.can_register_more_machines)

    @patch('Licensing.services.verify_license_payload', return_value=True)
    def test_sixth_machine_is_rejected_for_business_plan(self, mock_verify):
        for index in range(5):
            activate_purchased_license(
                license_key=self.license_data['license_key'],
                license_data=self.license_data,
                signature='signature',
                machine_fingerprint=f'machine-{index}',
                machine_name=f'POS-{index}',
            )

        with self.assertRaises(ValidationError):
            activate_purchased_license(
                license_key=self.license_data['license_key'],
                license_data=self.license_data,
                signature='signature',
                machine_fingerprint='machine-6',
                machine_name='POS-6',
            )


class LicenseRenewalTests(TestCase):
    def setUp(self):
        self.license = License.objects.create(
            license_key='LIC-RENEW-0001',
            plan='starter',
            subscription_duration='yearly',
            machine_limit=1,
            license_payload={
                'license_key': 'LIC-RENEW-0001',
                'expires_at': '2027-01-01T00:00:00+00:00',
            },
            signature='old-signature',
            is_active=True,
            expires_at=timezone.now() + timezone.timedelta(days=30),
        )

    @patch('Licensing.services.verify_license_payload', return_value=True)
    def test_signed_renewal_updates_expiry_and_audits(self, _mock_verify):
        new_expiry = timezone.now() + timezone.timedelta(days=180)
        payload = {
            'license_key': self.license.license_key,
            'expires_at': new_expiry.isoformat(),
            'subscription_duration': 'yearly',
        }

        updated = apply_signed_license_renewal(
            license_key=self.license.license_key,
            renewal_payload=payload,
            signature='new-signature',
            actor='support@vendor.local',
            machine_fingerprint='machine-renew',
        )

        self.assertEqual(updated.signature, 'new-signature')
        self.assertGreater(updated.expires_at, timezone.now())
        self.assertTrue(
            LicenseRenewalAudit.objects.filter(
                license=updated,
                action=LicenseRenewalAudit.ACTION_SIGNED_RENEWAL,
                actor='support@vendor.local',
            ).exists()
        )

    def test_manual_extension_requires_audit_fields(self):
        with self.assertRaises(ValidationError):
            manually_extend_license_expiry(
                license_key=self.license.license_key,
                new_expires_at=timezone.now() + timezone.timedelta(days=90),
                reason='',
                actor='ops',
            )

    def test_manual_extension_updates_expiry_and_logs(self):
        new_expiry = timezone.now() + timezone.timedelta(days=120)
        updated = manually_extend_license_expiry(
            license_key=self.license.license_key,
            new_expires_at=new_expiry,
            reason='Client contract renewed for Q4.',
            actor='ops@vendor.local',
            machine_fingerprint='machine-manual',
        )

        self.assertEqual(updated.expires_at, new_expiry)
        self.assertTrue(
            LicenseRenewalAudit.objects.filter(
                license=updated,
                action=LicenseRenewalAudit.ACTION_MANUAL_EXTENSION,
                actor='ops@vendor.local',
            ).exists()
        )


class LicenseRenewalApiPermissionTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.User = get_user_model()

        self.admin_user = self.User.objects.create_user(
            username='lic-admin',
            email='lic-admin@example.com',
            password='secret123',
            first_name='Lic',
            last_name='Admin',
            role=self.User.ADMIN,
        )
        self.sales_user = self.User.objects.create_user(
            username='lic-sales',
            email='lic-sales@example.com',
            password='secret123',
            first_name='Lic',
            last_name='Sales',
            role=self.User.SALES,
        )

    def test_apply_signed_renewal_blocks_non_privileged_role(self):
        view = LicenseViewSet.as_view({'post': 'apply_signed_renewal'})
        request = self.factory.post(
            '/api/licensing/apply_signed_renewal/',
            {
                'license_key': 'LIC-RENEW-0001',
                'renewal_payload': {'license_key': 'LIC-RENEW-0001', 'expires_at': '2027-01-01T00:00:00+00:00'},
                'signature': 'signature',
            },
            format='json',
        )
        force_authenticate(request, user=self.sales_user)

        response = view(request)

        self.assertEqual(response.status_code, 403)

    @patch('Licensing.views.apply_signed_license_renewal')
    def test_apply_signed_renewal_allows_admin_role(self, mock_apply_signed):
        license_obj = License.objects.create(
            license_key='LIC-RENEW-0009',
            plan='starter',
            subscription_duration='yearly',
            machine_limit=1,
            license_payload={'license_key': 'LIC-RENEW-0009'},
            signature='sig',
            is_active=True,
            expires_at=timezone.now() + timezone.timedelta(days=10),
        )
        mock_apply_signed.return_value = license_obj

        view = LicenseViewSet.as_view({'post': 'apply_signed_renewal'})
        request = self.factory.post(
            '/api/licensing/apply_signed_renewal/',
            {
                'license_key': 'LIC-RENEW-0009',
                'renewal_payload': {'license_key': 'LIC-RENEW-0009', 'expires_at': '2027-01-01T00:00:00+00:00'},
                'signature': 'signature',
            },
            format='json',
        )
        force_authenticate(request, user=self.admin_user)

        response = view(request)

        self.assertEqual(response.status_code, 200)
        mock_apply_signed.assert_called_once()

    def test_extend_expiry_requires_authentication(self):
        view = LicenseViewSet.as_view({'post': 'extend_expiry'})
        request = self.factory.post(
            '/api/licensing/extend_expiry/',
            {
                'license_key': 'LIC-RENEW-0001',
                'new_expires_at': (timezone.now() + timezone.timedelta(days=90)).isoformat(),
                'reason': 'Support intervention',
                'actor': 'support@example.com',
            },
            format='json',
        )

        response = view(request)

        self.assertEqual(response.status_code, 403)


@override_settings(LICENSE_ENFORCEMENT_ENABLED=True)
class LicenseEnforcementMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = LicenseEnforcementMiddleware(lambda request: JsonResponse({'ok': True}, status=200))

    @patch('Licensing.middleware.build_license_status')
    def test_restricted_mode_blocks_user_creation(self, mock_status):
        mock_status.return_value = {
            'has_license': True,
            'license_obj': object(),
            'license_state': 'restricted_mode',
            'license_state_display': 'Expired (16-45 days) - Restricted Mode',
        }
        request = self.factory.post('/api/accounts/users/', {}, HTTP_X_MACHINE_FINGERPRINT='machine-a')

        response = self.middleware(request)

        self.assertEqual(response.status_code, 403)
        self.assertIn(b'Restricted mode', response.content)
        self.assertIn(b'Add users', response.content)

    @patch('Licensing.middleware.build_license_status')
    def test_restricted_mode_allows_sales(self, mock_status):
        mock_status.return_value = {
            'has_license': True,
            'license_obj': object(),
            'license_state': 'restricted_mode',
            'license_state_display': 'Expired (16-45 days) - Restricted Mode',
        }
        request = self.factory.post('/api/sales/sales/', {}, HTTP_X_MACHINE_FINGERPRINT='machine-a')

        response = self.middleware(request)

        self.assertEqual(response.status_code, 200)

    @patch('Licensing.middleware.build_license_status')
    def test_emergency_mode_blocks_non_critical_endpoints(self, mock_status):
        mock_status.return_value = {
            'has_license': True,
            'license_obj': object(),
            'license_state': 'emergency_mode',
            'license_state_display': 'Expired (45+ days) - Emergency Mode',
        }
        request = self.factory.get('/api/core/branches/', HTTP_X_MACHINE_FINGERPRINT='machine-a')

        response = self.middleware(request)

        self.assertEqual(response.status_code, 403)
        self.assertIn(b'Emergency mode', response.content)

    @patch('Licensing.middleware.build_license_status')
    def test_emergency_mode_allows_product_view_get(self, mock_status):
        mock_status.return_value = {
            'has_license': True,
            'license_obj': object(),
            'license_state': 'emergency_mode',
            'license_state_display': 'Expired (45+ days) - Emergency Mode',
        }
        request = self.factory.get('/api/inventory/products/', HTTP_X_MACHINE_FINGERPRINT='machine-a')

        response = self.middleware(request)

        self.assertEqual(response.status_code, 200)

    @patch('Licensing.middleware.build_license_status')
    def test_missing_machine_fingerprint_is_rejected(self, mock_status):
        request = self.factory.get('/api/sales/sales/')

        response = self.middleware(request)

        self.assertEqual(response.status_code, 428)

    @patch('Licensing.middleware.build_license_status')
    def test_no_license_is_rejected(self, mock_status):
        mock_status.return_value = {
            'has_license': False,
            'license_obj': None,
            'license_state': 'none',
            'license_state_display': 'No Active License',
        }
        request = self.factory.get('/api/sales/sales/', HTTP_X_MACHINE_FINGERPRINT='machine-a')

        response = self.middleware(request)

        self.assertEqual(response.status_code, 403)

    def test_licensing_endpoints_remain_exempt(self):
        request = self.factory.post('/api/licensing/status/', {})

        response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
