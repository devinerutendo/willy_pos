import json
import tempfile
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from Accounts.models import User

BASE = '/api/core/backups'


def make_user(email, role):
    return User.objects.create_user(
        username=email.split('@')[0],
        email=email,
        password='Password#123',
        first_name='Test',
        last_name='User',
        role=role,
    )


def fake_call_command(name, *args, **kwargs):
    """Stand-in for the real management commands (they need a full database dump)."""
    if name == 'backup_system':
        Path(kwargs['output']).write_text('[]', encoding='utf-8')
    elif name == 'restore_system':
        fake_call_command.restored.append((args, kwargs))


class BackupApiTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings_override = override_settings(BASE_DIR=self._tmp.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        fake_call_command.restored = []
        patcher = mock.patch('Core.views.call_command', side_effect=fake_call_command)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.backups = Path(self._tmp.name) / 'backups'
        self.manager = make_user('mgr@example.com', User.MANAGER)
        self.client = APIClient()
        self.client.force_authenticate(self.manager)

    # -- permissions -------------------------------------------------------
    def test_non_managers_are_forbidden(self):
        self.client.force_authenticate(make_user('sales@example.com', User.SALES))
        self.assertEqual(self.client.get(f'{BASE}/status/').status_code, 403)
        self.assertEqual(self.client.post(f'{BASE}/backup_now/', {}, format='json').status_code, 403)
        self.assertEqual(self.client.post(f'{BASE}/schedule/', {}, format='json').status_code, 403)
        self.assertEqual(self.client.get(f'{BASE}/download/?file=x.json').status_code, 403)

    # -- backup now / status ----------------------------------------------
    def test_backup_now_creates_file_and_appears_in_status(self):
        response = self.client.post(f'{BASE}/backup_now/', {}, format='json')
        self.assertEqual(response.status_code, 201)
        name = response.data['file']
        self.assertTrue(name.startswith('manual-backup-'))

        status = self.client.get(f'{BASE}/status/').data
        self.assertEqual([b['name'] for b in status['backups']], [name])
        self.assertEqual(status['events'][0]['type'], 'MANUAL_BACKUP')
        self.assertIsNotNone(status['schedule']['last_backup_at'])

    def test_status_lists_only_real_backups(self):
        self.backups.mkdir(parents=True, exist_ok=True)
        (self.backups / 'manual-backup-20250101-000000.json').write_text('[]')
        (self.backups / 'restore-abc.json').write_text('[]')
        (self.backups / 'notes.json').write_text('[]')
        names = [b['name'] for b in self.client.get(f'{BASE}/status/').data['backups']]
        self.assertEqual(names, ['manual-backup-20250101-000000.json'])

    # -- schedule ----------------------------------------------------------
    def test_schedule_saves_and_computes_next_run(self):
        response = self.client.post(
            f'{BASE}/schedule/',
            {'enabled': True, 'automatic': True, 'frequency': 'weekly', 'time': '03:30', 'retain_count': 7},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        schedule = response.data['schedule']
        self.assertEqual(
            (schedule['enabled'], schedule['automatic'], schedule['frequency'], schedule['time'], schedule['retain_count']),
            (True, True, 'weekly', '03:30', 7),
        )
        self.assertIsNotNone(schedule['next_backup_at'])

    def test_schedule_string_false_is_false(self):
        response = self.client.post(
            f'{BASE}/schedule/', {'enabled': 'false', 'automatic': 'false'}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['schedule']['enabled'])
        self.assertIsNone(response.data['schedule']['next_backup_at'])

    def test_schedule_validation_errors_are_400_not_500(self):
        bad_payloads = [
            {'retain_count': 'abc'},
            {'retain_count': 0},
            {'retain_count': 100000},
            {'frequency': 'yearly'},
            {'time': '25:99'},
            {'enabled': 'maybe'},
        ]
        for payload in bad_payloads:
            response = self.client.post(f'{BASE}/schedule/', payload, format='json')
            self.assertEqual(response.status_code, 400, payload)

    # -- retention ----------------------------------------------------------
    def test_retention_only_prunes_backup_files(self):
        self.client.post(
            f'{BASE}/schedule/',
            {'enabled': False, 'automatic': False, 'frequency': 'daily', 'time': '02:00', 'retain_count': 1},
            format='json',
        )
        for _ in range(3):
            self.client.post(f'{BASE}/backup_now/', {}, format='json')
        self.assertEqual(len(self.client.get(f'{BASE}/status/').data['backups']), 1)
        # Metadata must survive pruning.
        self.assertTrue((self.backups / 'backup_schedule.json').exists())
        self.assertTrue((self.backups / 'backup_events.json').exists())

    # -- download -----------------------------------------------------------
    def test_download_serves_backup_and_blocks_other_files(self):
        name = self.client.post(f'{BASE}/backup_now/', {}, format='json').data['file']
        ok = self.client.get(f'{BASE}/download/', {'file': name})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(b''.join(ok.streaming_content), b'[]')

        for blocked in ('backup_schedule.json', '../../manage.py', 'missing.json'):
            self.assertEqual(self.client.get(f'{BASE}/download/', {'file': blocked}).status_code, 404, blocked)

    # -- restore -------------------------------------------------------------
    def _upload(self, content=b'[]', name='backup.json'):
        return {'file': SimpleUploadedFile(name, content, content_type='application/json')}

    def test_restore_rejects_missing_non_json_and_corrupt_files(self):
        self.assertEqual(self.client.post(f'{BASE}/restore_backup/', {}, format='multipart').status_code, 400)
        self.assertEqual(
            self.client.post(f'{BASE}/restore_backup/', self._upload(name='backup.txt'), format='multipart').status_code,
            400,
        )
        corrupt = self.client.post(
            f'{BASE}/restore_backup/', self._upload(b'not json at all'), format='multipart'
        )
        self.assertEqual(corrupt.status_code, 400)
        self.assertEqual(fake_call_command.restored, [])

    def test_restore_without_clear_does_not_take_safety_backup(self):
        response = self.client.post(f'{BASE}/restore_backup/', self._upload(), format='multipart')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['safety_backup'])
        self.assertEqual(len(fake_call_command.restored), 1)
        self.assertFalse(fake_call_command.restored[0][1]['clear'])

    def test_restore_with_clear_takes_safety_backup_first(self):
        response = self.client.post(
            f'{BASE}/restore_backup/?clear_existing=true', self._upload(), format='multipart'
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['safety_backup'].startswith('pre-restore-backup-'))
        self.assertTrue((self.backups / response.data['safety_backup']).exists())
        self.assertTrue(fake_call_command.restored[0][1]['clear'])

    def test_restore_failure_is_reported_and_temp_file_removed(self):
        def explode(name, *args, **kwargs):
            if name == 'restore_system':
                raise RuntimeError('bad data')
            return fake_call_command(name, *args, **kwargs)

        with mock.patch('Core.views.call_command', side_effect=explode):
            response = self.client.post(f'{BASE}/restore_backup/', self._upload(), format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertIn('bad data', response.data['detail'])
        self.assertEqual(list(self.backups.glob('restore-*')), [])
        events = json.loads((self.backups / 'backup_events.json').read_text())
        self.assertEqual(events[0]['outcome'], 'FAILED')

    def test_invalid_clear_existing_value_is_rejected(self):
        response = self.client.post(
            f'{BASE}/restore_backup/?clear_existing=perhaps', self._upload(), format='multipart'
        )
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# Company profile (business info + logo)
# ---------------------------------------------------------------------------
COMPANY_URL = '/api/core/company-profile/'


def image_upload(fmt='PNG', size=(40, 20), name='logo.png', content_type='image/png'):
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new('RGB', size, (10, 120, 200)).save(buffer, format=fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=content_type)


class CompanyProfileApiTests(TestCase):
    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        override = override_settings(MEDIA_ROOT=self._media.name)
        override.enable()
        self.addCleanup(override.disable)

        self.manager = make_user('mgr@example.com', User.MANAGER)
        self.client = APIClient()
        self.client.force_authenticate(self.manager)

    def logo_files(self):
        folder = Path(self._media.name) / 'company'
        return sorted(p.name for p in folder.glob('*')) if folder.exists() else []

    # -- access control ----------------------------------------------------
    def test_requires_authentication(self):
        self.assertEqual(APIClient().get(COMPANY_URL).status_code, 401)

    def test_every_role_can_read_defaults(self):
        for role in (User.ADMIN, User.MANAGER, User.INVENTORY, User.SALES, User.AUDITOR):
            client = APIClient()
            client.force_authenticate(make_user(f'{role.lower()}@example.com', role))
            response = client.get(COMPANY_URL)
            self.assertEqual(response.status_code, 200, role)
            self.assertEqual(response.data['name'], 'Willy POS')
            self.assertIsNone(response.data['logo'])

    def test_only_manager_and_admin_can_write(self):
        for role in (User.INVENTORY, User.SALES, User.AUDITOR):
            client = APIClient()
            client.force_authenticate(make_user(f'{role.lower()}@example.com', role))
            response = client.patch(COMPANY_URL, {'name': 'Hacked'}, format='json')
            self.assertEqual(response.status_code, 403, role)
        self.assertEqual(self.client.get(COMPANY_URL).data['name'], 'Willy POS')

        admin = APIClient()
        admin.force_authenticate(make_user('admin@example.com', User.ADMIN))
        self.assertEqual(admin.patch(COMPANY_URL, {'name': 'Acme'}, format='json').status_code, 200)

    def test_user_permissions_expose_company_scope(self):
        sales = make_user('perm@example.com', User.SALES)
        self.assertTrue(sales.has_permission_for('company', 'read'))
        self.assertFalse(sales.has_permission_for('company', 'write'))
        self.assertTrue(self.manager.has_permission_for('company', 'write'))

    # -- text fields -------------------------------------------------------
    def test_patch_updates_fields_and_records_editor(self):
        response = self.client.patch(
            COMPANY_URL,
            {'name': '  Acme Traders  ', 'phone': '+263 77 000 0000', 'city': 'Harare'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['name'], 'Acme Traders')
        self.assertEqual(response.data['city'], 'Harare')
        self.assertEqual(response.data['updated_by_name'], 'Test User')
        # Unspecified fields are untouched.
        self.assertTrue(response.data['terms'])
        self.assertEqual(self.client.get(COMPANY_URL).data['phone'], '+263 77 000 0000')

    def test_put_requires_name(self):
        response = self.client.put(COMPANY_URL, {'tagline': 'x'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.data)

    def test_validation_errors(self):
        for payload, field in (
            ({'name': '   '}, 'name'),
            ({'name': 'x' * 151}, 'name'),
            ({'email': 'not-an-email'}, 'email'),
            ({'phone': '1' * 41}, 'phone'),
            ({'terms': 'x' * 4001}, 'terms'),
        ):
            response = self.client.patch(COMPANY_URL, payload, format='json')
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn(field, response.data)

    # -- registration and payment details ----------------------------------
    REGISTRATION_PAYMENT = {
        'zimra_tin': '2000123456',
        'local_authority_licence': 'HCC/2026/0457',
        'praz_registration_number': 'PRAZ-778899',
        'payment_account_name': 'Acme Traders (Pvt) Ltd',
        'payment_bank': 'CBZ Bank',
        'payment_account_number': '0123456789012',
        'payment_phone': '+263 77 000 1111',
    }

    def test_registration_and_payment_details_default_to_empty(self):
        data = self.client.get(COMPANY_URL).data
        for field in self.REGISTRATION_PAYMENT:
            self.assertEqual(data[field], '', field)

    def test_registration_and_payment_details_round_trip(self):
        response = self.client.patch(COMPANY_URL, self.REGISTRATION_PAYMENT, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        fetched = self.client.get(COMPANY_URL).data
        for field, value in self.REGISTRATION_PAYMENT.items():
            self.assertEqual(fetched[field], value, field)

    def test_registration_and_payment_details_can_be_cleared(self):
        self.client.patch(COMPANY_URL, self.REGISTRATION_PAYMENT, format='json')
        cleared = self.client.patch(
            COMPANY_URL, {field: '' for field in self.REGISTRATION_PAYMENT}, format='json'
        )
        self.assertEqual(cleared.status_code, 200)
        for field in self.REGISTRATION_PAYMENT:
            self.assertEqual(cleared.data[field], '', field)

    def test_registration_and_payment_details_are_trimmed(self):
        response = self.client.patch(COMPANY_URL, {'payment_bank': '  CBZ Bank  '}, format='json')
        self.assertEqual(response.data['payment_bank'], 'CBZ Bank')

    def test_registration_and_payment_details_validation(self):
        limits = {
            'zimra_tin': 40, 'local_authority_licence': 60, 'praz_registration_number': 60,
            'payment_account_name': 150, 'payment_bank': 100,
            'payment_account_number': 50, 'payment_phone': 40,
        }
        for field, limit in limits.items():
            too_long = self.client.patch(COMPANY_URL, {field: 'x' * (limit + 1)}, format='json')
            self.assertEqual(too_long.status_code, 400, field)
            self.assertIn(field, too_long.data)
            multiline = self.client.patch(COMPANY_URL, {field: 'line one\nline two'}, format='json')
            self.assertEqual(multiline.status_code, 400, field)
            self.assertIn(field, multiline.data)

    def test_non_managers_cannot_change_payment_details(self):
        client = APIClient()
        client.force_authenticate(make_user('cashier@example.com', User.SALES))
        response = client.patch(COMPANY_URL, {'payment_bank': 'Evil Bank'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(COMPANY_URL).data['payment_bank'], '')

    def test_profile_stays_a_singleton(self):
        from Core.models import CompanyProfile

        self.client.patch(COMPANY_URL, {'name': 'One'}, format='json')
        self.client.patch(COMPANY_URL, {'name': 'Two'}, format='json')
        self.assertEqual(CompanyProfile.objects.count(), 1)

    # -- logo --------------------------------------------------------------
    def test_logo_upload_replace_and_remove(self):
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.patch(COMPANY_URL, {'logo': image_upload()}, format='multipart')
        self.assertEqual(first.status_code, 200, first.data)
        self.assertRegex(first.data['logo'], r'^/media/company/logo-[0-9a-f]{32}\.png$')
        self.assertEqual(len(self.logo_files()), 1)

        with self.captureOnCommitCallbacks(execute=True):
            second = self.client.patch(
                COMPANY_URL,
                {'logo': image_upload('JPEG', name='photo.jpeg', content_type='image/jpeg')},
                format='multipart',
            )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data['logo'].endswith('.jpg'))
        self.assertNotEqual(first.data['logo'], second.data['logo'])
        self.assertEqual(len(self.logo_files()), 1, 'replaced logo file must be deleted')

        with self.captureOnCommitCallbacks(execute=True):
            removed = self.client.patch(COMPANY_URL, {'remove_logo': 'true'}, format='multipart')
        self.assertEqual(removed.status_code, 200)
        self.assertIsNone(removed.data['logo'])
        self.assertEqual(self.logo_files(), [])

    def test_logo_extension_comes_from_content_not_filename(self):
        # PNG bytes uploaded under a .jpg name are stored with the real extension.
        response = self.client.patch(
            COMPANY_URL,
            {'logo': image_upload(name='logo.jpg', content_type='image/jpeg')},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['logo'].endswith('.png'))

    def test_rejects_non_image_file_extension(self):
        response = self.client.patch(
            COMPANY_URL,
            {'logo': image_upload(name='evil.html', content_type='text/html')},
            format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.logo_files(), [])

    def test_rejects_non_image_and_oversized_logo(self):
        fake = SimpleUploadedFile('logo.png', b'<script>alert(1)</script>', content_type='image/png')
        response = self.client.patch(COMPANY_URL, {'logo': fake}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertIn('logo', response.data)

        with mock.patch('Core.serializers.LOGO_MAX_BYTES', 10):
            response = self.client.patch(COMPANY_URL, {'logo': image_upload()}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.logo_files(), [])

    def test_rejects_unsupported_format_and_huge_dimensions(self):
        response = self.client.patch(
            COMPANY_URL,
            {'logo': image_upload('BMP', name='logo.bmp', content_type='image/bmp')},
            format='multipart',
        )
        self.assertEqual(response.status_code, 400)

        with mock.patch('Core.serializers.LOGO_MAX_DIMENSION', 10):
            response = self.client.patch(COMPANY_URL, {'logo': image_upload()}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.logo_files(), [])

    def test_cannot_upload_and_remove_in_one_request(self):
        response = self.client.patch(
            COMPANY_URL,
            {'logo': image_upload(), 'remove_logo': 'true'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.logo_files(), [])

    def test_failed_validation_keeps_existing_logo(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(COMPANY_URL, {'logo': image_upload()}, format='multipart')
        before = self.logo_files()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                COMPANY_URL,
                {'logo': image_upload(), 'name': ''},
                format='multipart',
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.logo_files(), before)
