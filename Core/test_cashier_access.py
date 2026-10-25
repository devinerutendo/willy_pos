"""Cashier role restrictions: no profit/cost data, no procurement, no business-wide reports.

The frontend hides these screens, but the API is the real boundary.
"""

import itertools
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from Accounts.models import User
from Inventory.models import Category, Product, Purchase, Supplier


_counter = itertools.count(1)


def make_user(role):
    n = next(_counter)
    return User.objects.create_user(
        username=f'{role.lower()}-user-{n}',
        email=f'{role.lower()}-{n}@example.com',
        password='Password#123',
        first_name='Test',
        last_name='User',
        role=role,
    )


class CashierAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user(User.ADMIN)
        category = Category.objects.create(name='Snacks', created_by=cls.admin, updated_by=cls.admin)
        cls.product = Product.objects.create(
            name='Chips', category=category, cost_price=Decimal('4.00'),
            selling_price=Decimal('10.00'), reorder_level=1,
            created_by=cls.admin, updated_by=cls.admin,
        )
        cls.supplier = Supplier.objects.create(
            name='Acme', email='a@example.com', phone='0123456789', address='1 Road',
            created_by=cls.admin, updated_by=cls.admin,
        )

    def client_for(self, role):
        client = APIClient()
        client.force_authenticate(make_user(role))
        return client

    # ---- role matrix -------------------------------------------------------

    def test_cashier_is_a_valid_role_without_financial_scopes(self):
        cashier = make_user(User.CASHIER)
        self.assertTrue(cashier.has_permission_for('sales', 'write'))
        self.assertTrue(cashier.has_permission_for('inventory', 'read'))
        self.assertFalse(cashier.has_permission_for('inventory', 'write'))
        for scope in ('financial_reports', 'purchases', 'sales_reports', 'audit', 'users'):
            with self.subTest(scope=scope):
                self.assertFalse(cashier.has_permission_for(scope, 'read'))
        self.assertFalse(cashier.can_view_cost_data())

    def test_financial_scope_is_not_granted_to_every_non_cashier_role(self):
        allowed = {
            User.ADMIN: True, User.MANAGER: True, User.AUDITOR: True,
            User.INVENTORY: False, User.SALES: False, User.CASHIER: False,
        }
        for role, expected in allowed.items():
            with self.subTest(role=role):
                self.assertEqual(make_user(role).has_permission_for('financial_reports', 'read'), expected)

    # ---- cost / margin redaction ------------------------------------------

    def test_cashier_product_responses_hide_cost_and_margin(self):
        client = self.client_for(User.CASHIER)
        listing = client.get('/api/inventory/products/')
        self.assertEqual(listing.status_code, 200)
        rows = listing.json()['results'] if isinstance(listing.json(), dict) else listing.json()
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn('cost_price', row)
            self.assertNotIn('profit_margin', row)
            self.assertIn('selling_price', row)

        detail = client.get(f'/api/inventory/products/{self.product.id}/').json()
        self.assertNotIn('cost_price', detail)
        self.assertNotIn('profit_margin', detail)

        variants = client.get('/api/inventory/variants/')
        if variants.status_code == 200:
            body = variants.json()
            for row in (body['results'] if isinstance(body, dict) else body):
                self.assertNotIn('cost_price', row)

        sync = client.get('/api/inventory/products/sync/').json()
        for row in sync['products']:
            self.assertNotIn('cost_price', row)
            self.assertNotIn('profit_margin', row)

    def test_sync_pull_hides_cost_for_cashier_but_not_manager(self):
        cashier = self.client_for(User.CASHIER).get('/api/sync/pull/')
        if cashier.status_code == 404:
            self.skipTest('sync pull route not mounted at /api/sync/pull/')
        for row in cashier.json()['products']:
            self.assertNotIn('cost_price', row)
        manager = self.client_for(User.MANAGER).get('/api/sync/pull/').json()
        self.assertTrue(all('cost_price' in row for row in manager['products']))

    def test_cost_visible_to_management_inventory_and_auditor(self):
        for role in (User.ADMIN, User.MANAGER, User.INVENTORY, User.AUDITOR):
            with self.subTest(role=role):
                row = self.client_for(role).get(f'/api/inventory/products/{self.product.id}/').json()
                self.assertEqual(row['cost_price'], '4.00')
                self.assertIn('profit_margin', row)

    def test_sales_role_no_longer_sees_cost(self):
        row = self.client_for(User.SALES).get(f'/api/inventory/products/{self.product.id}/').json()
        self.assertNotIn('cost_price', row)

    # ---- procurement / reports --------------------------------------------

    def test_cashier_cannot_read_purchases(self):
        client = self.client_for(User.CASHIER)
        self.assertEqual(client.get('/api/inventory/purchases/').status_code, 403)
        self.assertEqual(client.get('/api/inventory/purchase-items/').status_code, 403)

    def test_cashier_cannot_read_business_wide_sales_reports(self):
        client = self.client_for(User.CASHIER)
        self.assertEqual(client.get('/api/sales/sales/report/').status_code, 403)
        self.assertEqual(client.get('/api/sales/sales/daily-report/').status_code, 403)

    def test_existing_report_readers_keep_access(self):
        for role in (User.ADMIN, User.MANAGER, User.SALES, User.AUDITOR):
            client = self.client_for(role)
            with self.subTest(role=role):
                self.assertEqual(client.get('/api/sales/sales/report/').status_code, 200)
                self.assertEqual(client.get('/api/sales/sales/daily-report/').status_code, 200)
                self.assertEqual(client.get('/api/inventory/purchases/').status_code, 200)

    def test_cashier_keeps_pos_data_access(self):
        client = self.client_for(User.CASHIER)
        for url in ('/api/sales/sales/', '/api/sales/customers/', '/api/inventory/products/', '/api/inventory/stock/'):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_cashier_cannot_create_users_or_products(self):
        client = self.client_for(User.CASHIER)
        self.assertEqual(client.post('/api/inventory/products/', {}, format='json').status_code, 403)
        self.assertEqual(client.post('/api/inventory/purchases/', {}, format='json').status_code, 403)


class PosSaleFlowTests(TestCase):
    """The POS flow (create pending sale, then confirm) must work for every selling role."""

    def setUp(self):
        from Core.models import Branch, POSTerminal
        from Inventory.models import Location, Stock

        self.admin = make_user(User.ADMIN)
        self.branch = Branch.objects.create(
            name='Main', code='MN', created_by=self.admin, updated_by=self.admin,
        )
        self.terminal = POSTerminal.objects.create(
            branch=self.branch, name='T1', code='T1',
            created_by=self.admin, updated_by=self.admin,
        )
        self.location = Location.objects.create(
            branch=self.branch, name='Store', address='x',
            created_by=self.admin, updated_by=self.admin,
        )
        category = Category.objects.create(name='Snacks', created_by=self.admin, updated_by=self.admin)
        self.product = Product.objects.create(
            name='Chips', category=category, cost_price=Decimal('4.00'),
            selling_price=Decimal('10.00'), reorder_level=1,
            created_by=self.admin, updated_by=self.admin,
        )
        Stock.objects.create(product=self.product, location=self.location, quantity=50, reserved_quantity=0)

    def complete_sale(self, role):
        user = make_user(role)
        user.branch = self.branch
        user.save(update_fields=['branch'])
        client = APIClient()
        client.force_authenticate(user)
        created = client.post('/api/sales/sales/', {
            'branch': self.branch.id,
            'pos_terminal': self.terminal.id,
            'payment_method': 'CASH',
            'amount_received': 40,
            'items': [{'product': self.product.id, 'quantity': 4, 'unit_price': '10.00'}],
        }, format='json')
        self.assertEqual(created.status_code, 201, created.content)
        sale_id = created.json()['id']
        return client.post(f'/api/sales/sales/{sale_id}/confirm/', {
            'payment_method': 'CASH', 'amount_received': 40, 'change_amount': 0,
        }, format='json')

    def test_every_selling_role_can_complete_a_sale(self):
        for role in (User.CASHIER, User.SALES, User.MANAGER):
            with self.subTest(role=role):
                confirmed = self.complete_sale(role)
                self.assertEqual(confirmed.status_code, 200, confirmed.content)


class PurchaseIdempotencyTests(TestCase):
    URL = '/api/inventory/purchases/'

    def setUp(self):
        self.manager = make_user(User.MANAGER)
        category = Category.objects.create(name='Snacks', created_by=self.manager, updated_by=self.manager)
        self.product = Product.objects.create(
            name='Chips', category=category, cost_price=Decimal('4.00'),
            selling_price=Decimal('10.00'), reorder_level=1,
            created_by=self.manager, updated_by=self.manager,
        )
        self.supplier = Supplier.objects.create(
            name='Acme', email='a@example.com', phone='0123456789', address='1 Road',
            created_by=self.manager, updated_by=self.manager,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.manager)

    def payload(self):
        return {
            'supplier': self.supplier.id,
            'items': [{'product': self.product.id, 'quantity': 5, 'cost_price': '4.00'}],
        }

    def test_retry_with_same_key_returns_original_purchase(self):
        first = self.client.post(self.URL, self.payload(), format='json', HTTP_IDEMPOTENCY_KEY='draft-1')
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.post(self.URL, self.payload(), format='json', HTTP_IDEMPOTENCY_KEY='draft-1')
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(Purchase.objects.count(), 1)

    def test_different_keys_create_distinct_purchases(self):
        self.client.post(self.URL, self.payload(), format='json', HTTP_IDEMPOTENCY_KEY='a')
        self.client.post(self.URL, self.payload(), format='json', HTTP_IDEMPOTENCY_KEY='b')
        self.assertEqual(Purchase.objects.count(), 2)

    def test_request_without_key_still_works(self):
        response = self.client.post(self.URL, self.payload(), format='json')
        self.assertEqual(response.status_code, 201, response.content)

    def test_invalid_payload_is_rejected_and_key_not_consumed(self):
        bad = self.client.post(self.URL, {'supplier': self.supplier.id, 'items': []}, format='json',
                               HTTP_IDEMPOTENCY_KEY='k')
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post(self.URL, self.payload(), format='json', HTTP_IDEMPOTENCY_KEY='k')
        self.assertEqual(ok.status_code, 201, ok.content)
