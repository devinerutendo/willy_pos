from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from Accounts.models import User
from Inventory.models import Category, Product
from Sales.models import Customer, Return, Sale, SaleItem


class SalesReportTests(TestCase):
    """The report endpoint must apply the documented sales/refund definitions."""

    URL = '/api/sales/sales/report/'

    def setUp(self):
        self.admin = User.objects.create_user(
            username='boss', email='boss@example.com', password='secret123',
            first_name='Big', last_name='Boss', role=User.ADMIN,
        )
        self.cashier = User.objects.create_user(
            username='till', email='till@example.com', password='secret123',
            first_name='Tilly', last_name='Cash', role=User.SALES,
        )
        category = Category.objects.create(name='Snacks', created_by=self.admin, updated_by=self.admin)
        self.product = Product.objects.create(
            name='Chips', category=category, cost_price=Decimal('1.00'),
            selling_price=Decimal('10.00'), reorder_level=1,
            created_by=self.admin, updated_by=self.admin,
        )
        self.customer = Customer.objects.create(
            name='Alice Moyo', email='alice@example.com', created_by=self.admin, updated_by=self.admin,
        )
        self.today = timezone.localdate()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _sale(self, reference, *, status=Sale.COMPLETED, method='CASH', qty=2, unit='10.00',
              discount_pct='0', customer=None, sold_by=None, days_ago=0):
        sale = Sale.objects.create(
            reference=reference, status=status, payment_method=method,
            customer=customer, sold_by=sold_by or self.admin,
            created_by=self.admin, updated_by=self.admin,
        )
        SaleItem.objects.create(
            sale=sale, product=self.product, quantity=qty,
            unit_price=Decimal(unit), discount_percentage=Decimal(discount_pct),
        )
        if days_ago:
            Sale.objects.filter(pk=sale.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return sale

    def _report(self, **params):
        response = self.client.get(self.URL, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_only_completed_sales_count_and_discounts_are_separated(self):
        self._sale('S-1', qty=2, discount_pct='10')            # gross 20, net 18
        self._sale('S-2', qty=1)                                # gross 10, net 10
        self._sale('S-3', status=Sale.PENDING, qty=5)           # excluded
        self._sale('S-4', status=Sale.CANCELLED, qty=5)         # excluded

        data = self._report(start_date=self.today.isoformat(), end_date=self.today.isoformat())
        summary = data['summary']
        self.assertEqual(summary['gross_sales'], '30.00')
        self.assertEqual(summary['discounts'], '2.00')
        self.assertEqual(summary['net_sales'], '28.00')
        self.assertEqual(summary['transactions'], 2)
        self.assertEqual(summary['units_sold'], 3)
        self.assertEqual(summary['average_sale'], '14.00')
        self.assertEqual(summary['status_counts'], {'COMPLETED': 2, 'PENDING': 1, 'CANCELLED': 1})
        self.assertEqual(len(data['rows']), 4)
        self.assertEqual(data['period']['granularity'], 'hour')
        self.assertEqual(len(data['trend']), 24)

    def test_approved_refunds_reduce_net_sales_but_pending_do_not(self):
        sale = self._sale('S-1', qty=3, customer=self.customer)
        for number, status, amount in (('R-1', 'APPROVED', '10.00'), ('R-2', 'PENDING', '5.00')):
            Return.objects.create(
                return_number=number, return_type='SALES', sale=sale, customer=self.customer,
                status=status, reason='Damaged', total_amount=Decimal(amount),
                refund_amount=Decimal(amount), created_by=self.admin, updated_by=self.admin,
            )
        summary = self._report(start_date=self.today.isoformat(), end_date=self.today.isoformat())['summary']
        self.assertEqual(summary['gross_sales'], '30.00')
        self.assertEqual(summary['refunds'], '10.00')
        self.assertEqual(summary['net_sales'], '20.00')

    def test_filters_and_search(self):
        self._sale('RCPT-100', method='CARD', customer=self.customer, sold_by=self.cashier)
        self._sale('RCPT-200', method='CASH')

        by_method = self._report(payment_method='CARD')
        self.assertEqual([r['reference'] for r in by_method['rows']], ['RCPT-100'])
        self.assertEqual(by_method['rows'][0]['cashier_name'], 'Tilly Cash')

        by_cashier = self._report(sold_by=self.cashier.id)
        self.assertEqual(by_cashier['summary']['transactions'], 1)

        by_customer = self._report(search='alice')
        self.assertEqual([r['reference'] for r in by_customer['rows']], ['RCPT-100'])

        by_receipt = self._report(search='rcpt-200')
        self.assertEqual([r['reference'] for r in by_receipt['rows']], ['RCPT-200'])

        by_status = self._report(status='PENDING')
        self.assertEqual(by_status['rows'], [])

        self.assertEqual({c['name'] for c in by_method['cashiers']}, {'Big Boss', 'Tilly Cash'})

    def test_range_trend_and_previous_period(self):
        self._sale('NOW', qty=1)                       # 10.00 today
        self._sale('OLD', qty=2, days_ago=3)           # 20.00 three days ago
        start = self.today - timedelta(days=1)
        data = self._report(start_date=start.isoformat(), end_date=self.today.isoformat())
        self.assertEqual(data['period']['granularity'], 'day')
        self.assertEqual([t['key'] for t in data['trend']], [start.isoformat(), self.today.isoformat()])
        self.assertEqual(data['summary']['net_sales'], '10.00')
        self.assertEqual(data['previous']['end'], (start - timedelta(days=1)).isoformat())
        self.assertEqual(data['previous']['net_sales'], '20.00')

    def test_all_time_uses_monthly_trend_for_long_history(self):
        self._sale('NOW', qty=1)
        self._sale('ANCIENT', qty=1, days_ago=200)
        data = self._report()
        self.assertEqual(data['period']['granularity'], 'month')
        self.assertIsNone(data['previous'])
        self.assertEqual(data['summary']['transactions'], 2)
        self.assertGreaterEqual(len(data['trend']), 6)

    def test_invalid_range_is_rejected(self):
        response = self.client.get(self.URL, {'start_date': '2026-02-01', 'end_date': '2026-01-01'})
        self.assertEqual(response.status_code, 400)
        response = self.client.get(self.URL, {'start_date': 'nonsense'})
        self.assertEqual(response.status_code, 400)
