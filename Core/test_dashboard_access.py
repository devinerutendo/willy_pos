"""Server-side authorization for the endpoints behind the dashboard cards.

The dashboard hides or disables cards the user may not open, but that is only a
convenience. These tests pin down that the API itself rejects anonymous callers
and enforces the role permission matrix on every destination module.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from Accounts.models import User

# Endpoint behind each dashboard destination.
DESTINATIONS = {
    'purchases': '/api/inventory/purchases/',
    'sales': '/api/sales/sales/',
    'products': '/api/inventory/products/',
    'stock': '/api/inventory/stock/',
    'customers': '/api/sales/customers/',
    'suppliers': '/api/inventory/suppliers/',
}


def make_user(role):
    return User.objects.create_user(
        username=f'{role.lower()}-user',
        email=f'{role.lower()}@example.com',
        password='Password#123',
        first_name='Test',
        last_name='User',
        role=role,
    )


class DashboardDestinationAccessTests(TestCase):
    def test_anonymous_requests_are_rejected(self):
        client = APIClient()
        for name, url in DESTINATIONS.items():
            with self.subTest(destination=name):
                self.assertEqual(client.get(url).status_code, 401)

    def test_read_only_roles_cannot_write(self):
        # AUDITOR is read-only everywhere; SALES is read-only on inventory.
        cases = [
            (User.AUDITOR, ['purchases', 'sales', 'customers', 'suppliers']),
            (User.SALES, ['purchases', 'suppliers']),
            (User.INVENTORY, ['sales', 'customers']),
        ]
        for role, destinations in cases:
            client = APIClient()
            client.force_authenticate(make_user(role))
            for name in destinations:
                with self.subTest(role=role, destination=name):
                    response = client.post(DESTINATIONS[name], {}, format='json')
                    self.assertEqual(response.status_code, 403)

    def test_roles_can_read_their_permitted_destinations(self):
        readable = {
            User.ADMIN: DESTINATIONS.keys(),
            User.MANAGER: DESTINATIONS.keys(),
            User.INVENTORY: ['purchases', 'products', 'stock', 'suppliers'],
            User.SALES: ['sales', 'products', 'stock', 'customers'],
        }
        for role, destinations in readable.items():
            client = APIClient()
            client.force_authenticate(make_user(role))
            for name in destinations:
                with self.subTest(role=role, destination=name):
                    self.assertEqual(client.get(DESTINATIONS[name]).status_code, 200)
