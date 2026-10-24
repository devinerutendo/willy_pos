from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from Accounts.models import User, UserSecurity
from Accounts.security import (
    decrypt_secret,
    encrypt_secret,
    generate_totp_secret,
    verify_totp,
    _hotp,
)

PASSWORD = 'Password#123'


def make_user(email='sec@example.com'):
    return User.objects.create_user(
        username=email.split('@')[0],
        email=email,
        password=PASSWORD,
        first_name='Sec',
        last_name='User',
        role=User.MANAGER,
    )


def current_code(secret, offset=0):
    return _hotp(secret, int(timezone.now().timestamp() // 30) + offset)


class TotpHelperTests(TestCase):
    def test_rfc6238_reference_vector(self):
        # RFC 6238 appendix B, SHA1 secret "12345678901234567890", T=59 -> 94287082 (8 digits)
        import base64

        secret = base64.b32encode(b'12345678901234567890').decode().rstrip('=')
        self.assertEqual(_hotp(secret, 59 // 30)[-6:], '287082')

    def test_verify_accepts_current_and_rejects_replay_and_garbage(self):
        secret = generate_totp_secret()
        now = 1_700_000_000
        code = _hotp(secret, now // 30)
        step = verify_totp(secret, code, now=now)
        self.assertEqual(step, now // 30)
        self.assertIsNone(verify_totp(secret, code, last_step=step, now=now))
        self.assertIsNone(verify_totp(secret, 'abcdef', now=now))
        self.assertIsNone(verify_totp(secret, '12345', now=now))
        self.assertIsNone(verify_totp(secret, code, now=now + 300))

    def test_secret_encryption_roundtrip(self):
        secret = generate_totp_secret()
        encrypted = encrypt_secret(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(decrypt_secret(encrypted), secret)


class SecuritySettingsApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_defaults_and_update(self):
        response = self.client.get('/api/accounts/security/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['two_factor_enabled'])
        self.assertFalse(response.data['session_timeout_enabled'])
        self.assertIsNone(response.data['session_timeout_minutes'])
        self.assertEqual(response.data['recovery_codes_remaining'], 0)

        patched = self.client.patch(
            '/api/accounts/security/', {'session_timeout_minutes': 15}, format='json'
        )
        self.assertEqual(patched.status_code, status.HTTP_200_OK)
        self.assertTrue(patched.data['session_timeout_enabled'])
        self.assertEqual(patched.data['session_timeout_minutes'], 15)

        cleared = self.client.patch(
            '/api/accounts/security/', {'session_timeout_minutes': None}, format='json'
        )
        self.assertEqual(cleared.status_code, status.HTTP_200_OK)
        self.assertFalse(cleared.data['session_timeout_enabled'])
        self.assertIsNone(cleared.data['session_timeout_minutes'])

    def test_rejects_out_of_range_timeout(self):
        for minutes in (0, 4, 1441):
            response = self.client.patch(
                '/api/accounts/security/', {'session_timeout_minutes': minutes}, format='json'
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, minutes)

    def test_two_factor_cannot_be_toggled_via_patch(self):
        self.client.patch('/api/accounts/security/', {'two_factor_enabled': True}, format='json')
        self.assertFalse(UserSecurity.objects.get(user=self.user).two_factor_enabled)

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        response = self.client.get('/api/accounts/security/')
        self.assertIn(response.status_code, (401, 403))


class TwoFactorFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _enroll(self):
        setup = self.client.post('/api/accounts/security/2fa/setup/', {'password': PASSWORD}, format='json')
        self.assertEqual(setup.status_code, status.HTTP_200_OK)
        secret = setup.data['secret']
        enable = self.client.post(
            '/api/accounts/security/2fa/enable/', {'code': current_code(secret)}, format='json'
        )
        self.assertEqual(enable.status_code, status.HTTP_200_OK, enable.data)
        self.recovery_codes = enable.data['recovery_codes']
        return secret

    def test_setup_requires_correct_password(self):
        response = self.client.post('/api/accounts/security/2fa/setup/', {'password': 'wrong'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_secret_is_never_stored_in_plaintext(self):
        setup = self.client.post('/api/accounts/security/2fa/setup/', {'password': PASSWORD}, format='json')
        stored = UserSecurity.objects.get(user=self.user).pending_totp_secret
        self.assertTrue(stored)
        self.assertNotIn(setup.data['secret'], stored)
        self.assertTrue(setup.data['otpauth_uri'].startswith('otpauth://totp/'))

    def test_enable_rejects_bad_code_and_stays_disabled(self):
        self.client.post('/api/accounts/security/2fa/setup/', {'password': PASSWORD}, format='json')
        response = self.client.post('/api/accounts/security/2fa/enable/', {'code': '000000'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(UserSecurity.objects.get(user=self.user).two_factor_enabled)

    def test_enable_requires_setup_first(self):
        response = self.client.post('/api/accounts/security/2fa/enable/', {'code': '123456'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_requires_otp_once_enabled(self):
        secret = self._enroll()
        anon = APIClient()

        missing = anon.post('/api/accounts/login/', {'email': self.user.email, 'password': PASSWORD}, format='json')
        self.assertEqual(missing.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(missing.data['code'], 'two_factor_required')
        self.assertEqual(Token.objects.filter(user=self.user).count(), 0)

        wrong = anon.post(
            '/api/accounts/login/',
            {'email': self.user.email, 'password': PASSWORD, 'otp': '000000'},
            format='json',
        )
        self.assertEqual(wrong.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(wrong.data['code'], 'two_factor_invalid')

        # The enrolment code was already consumed; use the next step's code.
        ok = anon.post(
            '/api/accounts/login/',
            {'email': self.user.email, 'password': PASSWORD, 'otp': current_code(secret, 1)},
            format='json',
        )
        self.assertEqual(ok.status_code, status.HTTP_200_OK, ok.data)
        self.assertIn('token', ok.data)

    def test_wrong_password_never_reveals_two_factor_state(self):
        self._enroll()
        response = APIClient().post(
            '/api/accounts/login/', {'email': self.user.email, 'password': 'nope'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn('code', response.data)

    def test_used_code_cannot_be_replayed(self):
        secret = self._enroll()
        replay = APIClient().post(
            '/api/accounts/login/',
            {'email': self.user.email, 'password': PASSWORD, 'otp': current_code(secret)},
            format='json',
        )
        self.assertEqual(replay.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_disable_needs_password_and_code(self):
        secret = self._enroll()
        bad_password = self.client.post(
            '/api/accounts/security/2fa/disable/',
            {'password': 'wrong', 'code': current_code(secret, 1)},
            format='json',
        )
        self.assertEqual(bad_password.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(UserSecurity.objects.get(user=self.user).two_factor_enabled)

        ok = self.client.post(
            '/api/accounts/security/2fa/disable/',
            {'password': PASSWORD, 'code': current_code(secret, 1)},
            format='json',
        )
        self.assertEqual(ok.status_code, status.HTTP_200_OK, ok.data)
        security = UserSecurity.objects.get(user=self.user)
        self.assertFalse(security.two_factor_enabled)
        self.assertEqual(security.totp_secret, '')
        self.assertEqual(security.recovery_code_hashes, [])

    def test_enrolment_issues_recovery_codes_stored_only_as_hashes(self):
        self._enroll()
        self.assertEqual(len(self.recovery_codes), 8)
        security = UserSecurity.objects.get(user=self.user)
        self.assertEqual(len(security.recovery_code_hashes), 8)
        for code in self.recovery_codes:
            self.assertNotIn(code, security.recovery_code_hashes)
            self.assertNotIn(code.replace('-', ''), security.recovery_code_hashes)
        # Subsequent reads never include the codes again.
        again = self.client.get('/api/accounts/security/')
        self.assertNotIn('recovery_codes', again.data)
        self.assertEqual(again.data['recovery_codes_remaining'], 8)

    def test_recovery_code_logs_in_once(self):
        self._enroll()
        code = self.recovery_codes[0]
        payload = {'email': self.user.email, 'password': PASSWORD, 'otp': code.upper()}

        first = APIClient().post('/api/accounts/login/', payload, format='json')
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        self.assertEqual(UserSecurity.objects.get(user=self.user).recovery_code_hashes.__len__(), 7)

        second = APIClient().post('/api/accounts/login/', payload, format='json')
        self.assertEqual(second.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_recovery_code_can_disable_two_factor(self):
        self._enroll()
        response = self.client.post(
            '/api/accounts/security/2fa/disable/',
            {'password': PASSWORD, 'code': self.recovery_codes[1]},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data['two_factor_enabled'])


class SessionTimeoutEnforcementTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.token = Token.objects.create(user=self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def _security(self, **fields):
        security, _ = UserSecurity.objects.get_or_create(user=self.user)
        for key, value in fields.items():
            setattr(security, key, value)
        security.save()
        return security

    def test_idle_session_is_rejected_and_token_revoked(self):
        self._security(
            session_timeout_minutes=15,
            last_activity_at=timezone.now() - timedelta(minutes=16),
        )
        response = self.client.get('/api/accounts/profile/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(Token.objects.filter(pk=self.token.pk).exists())

    def test_active_session_is_accepted_and_activity_refreshed(self):
        self._security(
            session_timeout_minutes=15,
            last_activity_at=timezone.now() - timedelta(minutes=10),
        )
        response = self.client.get('/api/accounts/profile/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refreshed = UserSecurity.objects.get(user=self.user).last_activity_at
        self.assertGreater(refreshed, timezone.now() - timedelta(minutes=1))

    def test_timeout_disabled_never_expires(self):
        self._security(
            session_timeout_minutes=None,
            last_activity_at=timezone.now() - timedelta(days=30),
        )
        self.assertEqual(self.client.get('/api/accounts/profile/').status_code, status.HTTP_200_OK)

    def test_new_login_starts_fresh_idle_period(self):
        self._security(
            session_timeout_minutes=5,
            last_activity_at=timezone.now() - timedelta(hours=3),
        )
        login = APIClient().post(
            '/api/accounts/login/', {'email': self.user.email, 'password': PASSWORD}, format='json'
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        authed = APIClient()
        authed.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        self.assertEqual(authed.get('/api/accounts/profile/').status_code, status.HTTP_200_OK)
