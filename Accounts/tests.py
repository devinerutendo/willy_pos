from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory
from rest_framework.test import APIClient

from rest_framework.authtoken.models import Token

from Accounts.models import User
from Accounts.views import LoginView


class UserPermissionMatrixTests(TestCase):
	def test_inventory_role_can_adjust_stock_but_cannot_manage_users(self):
		user = User.objects.create_user(
			username='inventory-user',
			email='inventory@example.com',
			password='secret123',
			first_name='Inventory',
			last_name='User',
			role=User.INVENTORY,
		)

		self.assertTrue(user.has_permission_for('inventory', 'write'))
		self.assertTrue(user.has_permission_for('stock_adjustments', 'write'))
		self.assertTrue(user.has_permission_for('audit', 'read'))
		self.assertFalse(user.has_permission_for('users', 'write'))


class SingleSessionLoginTests(TestCase):
	def setUp(self):
		self.factory = APIRequestFactory()
		self.user = User.objects.create_user(
			username='single-session-user',
			email='single.session@example.com',
			password='secret123',
			first_name='Single',
			last_name='Session',
			role=User.SALES,
		)

	def test_login_rotates_token_and_invalidates_previous_session(self):
		request = self.factory.post(
			'/api/accounts/login/',
			{'email': self.user.email, 'password': 'secret123'},
			format='json',
		)

		first_response = LoginView.as_view()(request)
		self.assertEqual(first_response.status_code, status.HTTP_200_OK)
		first_token = first_response.data['token']

		request = self.factory.post(
			'/api/accounts/login/',
			{'email': self.user.email, 'password': 'secret123'},
			format='json',
		)

		second_response = LoginView.as_view()(request)
		self.assertEqual(second_response.status_code, status.HTTP_200_OK)
		second_token = second_response.data['token']

		self.assertNotEqual(first_token, second_token)
		self.assertEqual(Token.objects.filter(user=self.user).count(), 1)


class BootstrapRegistrationFlowTests(TestCase):
	def setUp(self):
		self.client = APIClient()

	def test_public_signup_allowed_until_superuser_exists(self):
		response = self.client.get('/api/accounts/registration-status/')
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertTrue(response.data['allow_public_signup'])

		payload = {
			'username': 'bootstrap-admin',
			'email': 'bootstrap.admin@example.com',
			'phone': '1234567890',
			'first_name': 'Bootstrap',
			'last_name': 'Admin',
			'password': 'SecurePass123!',
			'password_confirm': 'SecurePass123!',
		}
		create_response = self.client.post('/api/accounts/users/', payload, format='json')
		self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)

		created_user = User.objects.get(email='bootstrap.admin@example.com')
		self.assertTrue(created_user.is_superuser)
		self.assertTrue(created_user.is_staff)
		self.assertEqual(created_user.role, User.ADMIN)

		status_after_bootstrap = self.client.get('/api/accounts/registration-status/')
		self.assertEqual(status_after_bootstrap.status_code, status.HTTP_200_OK)
		self.assertFalse(status_after_bootstrap.data['allow_public_signup'])
		self.assertTrue(status_after_bootstrap.data['has_superuser'])

	def test_public_signup_forbidden_after_superuser_exists(self):
		User.objects.create_superuser(
			username='existing-admin',
			email='existing.admin@example.com',
			password='SecurePass123!',
			first_name='Existing',
			last_name='Admin',
		)

		payload = {
			'username': 'second-user',
			'email': 'second.user@example.com',
			'phone': '9876543210',
			'first_name': 'Second',
			'last_name': 'User',
			'password': 'SecurePass123!',
			'password_confirm': 'SecurePass123!',
		}
		response = self.client.post('/api/accounts/users/', payload, format='json')
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
		self.assertEqual(User.objects.filter(email='second.user@example.com').count(), 0)
