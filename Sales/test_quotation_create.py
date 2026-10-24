from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from Accounts.models import User
from Core.models import Branch
from Inventory.models import Category, Product
from Sales.models import Customer, Quotation, QuotationItem


class QuotationCreateTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin', email='admin@example.com', password='secret123',
            role=User.ADMIN, is_superuser=True,
        )
        self.branch = Branch.objects.create(
            name='Downtown', code='DTN', created_by=self.admin, updated_by=self.admin,
        )
        self.sales_user = User.objects.create_user(
            username='sales', email='sales@example.com', password='secret123',
            role=User.SALES,
        )
        self.sales_user.branch = self.branch
        self.sales_user.save(update_fields=['branch'])

        category = Category.objects.create(
            name='Snacks', description='x', created_by=self.admin, updated_by=self.admin,
        )
        self.product_a = Product.objects.create(
            name='Potato Chips', category=category, cost_price=Decimal('1.00'),
            selling_price=Decimal('10.00'), created_by=self.admin, updated_by=self.admin,
        )
        self.product_b = Product.objects.create(
            name='Cola', category=category, cost_price=Decimal('1.00'),
            selling_price=Decimal('4.00'), created_by=self.admin, updated_by=self.admin,
        )
        self.customer = Customer.objects.create(
            name='Acme Ltd', email='acme@example.com', phone='123', address='1 Road',
            created_by=self.admin, updated_by=self.admin,
        )
        self.client = APIClient()
        self.url = reverse('quotation-list')

    def _payload(self, **overrides):
        payload = {
            'customer': self.customer.pk,
            'valid_until': (timezone.now().date() + timedelta(days=14)).isoformat(),
            'notes': 'Delivery within 3 days',
            'items': [
                {'product': self.product_a.pk, 'quantity': 2, 'unit_price': 10.0},
                {'product': self.product_b.pk, 'quantity': 3, 'unit_price': 4.5},
            ],
        }
        payload.update(overrides)
        return payload

    def test_sales_user_creates_quotation(self):
        self.client.force_authenticate(user=self.sales_user)
        response = self.client.post(self.url, self._payload(), format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        quotation = Quotation.objects.get(pk=response.data['id'])
        self.assertEqual(quotation.items.count(), 2)
        self.assertEqual(quotation.branch, self.branch)
        self.assertEqual(quotation.created_by, self.sales_user)
        self.assertEqual(len(response.data['items']), 2)

    def test_admin_creates_quotation_with_discount(self):
        self.client.force_authenticate(user=self.admin)
        payload = self._payload()
        for item in payload['items']:
            item['discount_percentage'] = 12.5
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        item = QuotationItem.objects.filter(quotation_id=response.data['id']).first()
        self.assertEqual(item.discount_percentage, Decimal('12.50'))

    def test_quotation_with_inactive_customer_is_rejected(self):
        self.customer.is_active = False
        self.customer.save()
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.url, self._payload(), format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer', response.data)

    def test_failed_item_creation_leaves_no_orphan_quotation(self):
        self.client.force_authenticate(user=self.admin)
        payload = self._payload(items=[
            {'product': self.product_a.pk, 'quantity': 1, 'unit_price': 10.0},
            {'product': 999999, 'quantity': 1, 'unit_price': 1.0},
        ])
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Quotation.objects.count(), 0)

    def test_duplicate_product_lines_are_rejected_cleanly(self):
        self.client.force_authenticate(user=self.admin)
        payload = self._payload(items=[
            {'product': self.product_a.pk, 'quantity': 1, 'unit_price': 10.0},
            {'product': self.product_a.pk, 'quantity': 2, 'unit_price': 10.0},
        ])
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Quotation.objects.count(), 0)
