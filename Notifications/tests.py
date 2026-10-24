from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from .models import Notification, NotificationPreference, NotificationType
from .services import create_notification, create_system_notification

User = get_user_model()
SETTINGS_URL = '/api/notifications/preferences/settings/'


def make_user(email='manager@example.com', role=None):
    return User.objects.create_user(
        username=email.split('@')[0],
        email=email,
        password='Password#123',
        first_name='Test',
        last_name='User',
        role=role or User.MANAGER,
    )


class NotificationSettingsApiTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(SETTINGS_URL).status_code, (401, 403))

    def test_defaults_when_nothing_saved(self):
        response = self.client.get(SETTINGS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data['categories'],
            {
                'order_alerts': True,
                'stock_alerts': True,
                'payment_reminders': True,
                'daily_report': True,
            },
        )
        self.assertEqual(response.data['channels'], {'email': False, 'sms': False})
        self.assertFalse(response.data['capabilities']['sms'])

    def test_save_then_read_roundtrip(self):
        response = self.client.put(
            SETTINGS_URL,
            {
                'categories': {'stock_alerts': False, 'daily_report': True},
                'channels': {'email': True},
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['categories']['stock_alerts'])
        self.assertTrue(response.data['channels']['email'])

        again = self.client.get(SETTINGS_URL).data
        self.assertEqual(again, response.data)

        low_stock = NotificationPreference.objects.get(user=self.user, notification_type='low_stock')
        self.assertFalse(low_stock.enabled)
        self.assertEqual(low_stock.channels, ['in_app', 'email'])

    def test_partial_update_keeps_other_values(self):
        self.client.put(SETTINGS_URL, {'categories': {'order_alerts': False}}, format='json')
        response = self.client.patch(SETTINGS_URL, {'channels': {'email': True}}, format='json')
        self.assertFalse(response.data['categories']['order_alerts'])
        self.assertTrue(response.data['channels']['email'])

    def test_string_false_is_parsed_as_false(self):
        response = self.client.put(
            SETTINGS_URL, {'categories': {'stock_alerts': 'false'}}, format='json'
        )
        self.assertFalse(response.data['categories']['stock_alerts'])

    def test_rejects_invalid_values(self):
        response = self.client.put(
            SETTINGS_URL, {'categories': {'stock_alerts': 'maybe'}}, format='json'
        )
        self.assertEqual(response.status_code, 400)

    def test_sms_rejected_when_unavailable(self):
        response = self.client.put(SETTINGS_URL, {'channels': {'sms': True}}, format='json')
        self.assertEqual(response.status_code, 400)

    @override_settings(NOTIFICATIONS_SMS_ENABLED=True)
    def test_sms_accepted_when_available(self):
        response = self.client.put(SETTINGS_URL, {'channels': {'sms': True}}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['channels']['sms'])

    def test_settings_are_per_user(self):
        other = make_user('other@example.com')
        self.client.put(SETTINGS_URL, {'categories': {'daily_report': False}}, format='json')
        self.client.force_authenticate(other)
        self.assertTrue(self.client.get(SETTINGS_URL).data['categories']['daily_report'])


class NotificationPreferenceEnforcementTests(APITestCase):
    def setUp(self):
        self.user = make_user()

    def test_notification_created_by_default(self):
        notification = create_notification(
            self.user, NotificationType.LOW_STOCK, 'Low stock'
        )
        self.assertIsNotNone(notification)
        self.assertEqual(notification.logs.count(), 1)

    def test_disabled_category_suppresses_notification(self):
        self.client.force_authenticate(self.user)
        self.client.put(SETTINGS_URL, {'categories': {'stock_alerts': False}}, format='json')

        self.assertIsNone(
            create_notification(self.user, NotificationType.LOW_STOCK, 'Low stock')
        )
        self.assertIsNone(
            create_notification(self.user, NotificationType.OUT_OF_STOCK, 'Out of stock')
        )
        self.assertEqual(Notification.objects.count(), 0)

        # Other categories are untouched.
        self.assertIsNotNone(
            create_notification(self.user, NotificationType.DAILY_SALES_SUMMARY, 'Summary')
        )

    def test_email_channel_adds_delivery_log(self):
        self.client.force_authenticate(self.user)
        self.client.put(SETTINGS_URL, {'channels': {'email': True}}, format='json')

        notification = create_notification(self.user, NotificationType.LOW_STOCK, 'Low stock')
        channels = set(notification.logs.values_list('channel', flat=True))
        self.assertEqual(channels, {'in_app', 'email'})

    def test_system_notifications_ignore_preferences(self):
        self.assertIsNotNone(
            create_system_notification(NotificationType.GENERIC, 'System notice')
        )
