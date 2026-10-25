from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient

from Accounts.models import User
from Inventory.models import Category, Product
from Sales.models import Customer, Return, Sale, SaleItem


class DailySalesReportTests(TestCase):
    """The daily report must list every receipt with every line, using stored prices."""

    URL = '/api/sales/sales/daily-report/'
    REPORT_URL = '/api/sales/sales/report/'

    def setUp(self):
        self.admin = User.objects.create_user(
            username='boss', email='boss@example.com', password='secret123',
            first_name='Big', last_name='Boss', role=User.ADMIN,
        )
        self.cashier = User.objects.create_user(
            username='till', email='till@example.com', password='secret123',
            first_name='Tilly', last_name='Cash', role=User.SALES,
        )
        self.auditor = User.objects.create_user(
            username='audit', email='audit@example.com', password='secret123',
            first_name='Audi', last_name='Tor', role=User.AUDITOR,
        )
        category = Category.objects.create(name='Snacks', created_by=self.admin, updated_by=self.admin)
        self.chips = Product.objects.create(
            name='Chips', category=category, cost_price=Decimal('1.00'),
            selling_price=Decimal('10.00'), reorder_level=1,
            created_by=self.admin, updated_by=self.admin,
        )
        self.juice = Product.objects.create(
            name='Apple Juice', category=category, cost_price=Decimal('1.00'),
            selling_price=Decimal('2.00'), reorder_level=1,
            created_by=self.admin, updated_by=self.admin,
        )
        self.customer = Customer.objects.create(
            name='Alice Moyo', email='alice@example.com', created_by=self.admin, updated_by=self.admin,
        )
        self.today = timezone.localdate()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _sale(self, reference, lines, *, status=Sale.COMPLETED, method='CASH',
              customer=None, sold_by=None, days_ago=0, payment_status=Sale.PAID):
        sale = Sale.objects.create(
            reference=reference, status=status, payment_method=method,
            payment_status=payment_status, customer=customer,
            sold_by=sold_by or self.admin, created_by=self.admin, updated_by=self.admin,
        )
        for product, qty, unit, discount_pct in lines:
            SaleItem.objects.create(
                sale=sale, product=product, quantity=qty,
                unit_price=Decimal(unit), discount_percentage=Decimal(discount_pct),
            )
        if days_ago:
            Sale.objects.filter(pk=sale.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return sale

    def _daily(self, user=None, **params):
        if user is not None:
            self.client.force_authenticate(user)
        response = self.client.get(self.URL, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_defaults_to_today_and_returns_every_line_with_stored_prices(self):
        self._sale('S-1', [(self.chips, 2, '9.00', '10'), (self.juice, 3, '2.00', '0')],
                   customer=self.customer)
        # Catalogue price changes later must not alter the stored sale prices.
        Product.objects.filter(pk=self.chips.pk).update(selling_price=Decimal('99.00'))
        self._sale('S-OLD', [(self.chips, 1, '10.00', '0')], days_ago=3)

        data = self._daily()
        self.assertEqual(data['date'], self.today.isoformat())
        self.assertEqual(data['count'], 1)
        sale = data['results'][0]
        self.assertEqual(sale['reference'], 'S-1')
        self.assertEqual(sale['customer_name'], 'Alice Moyo')
        self.assertEqual(sale['cashier_name'], 'Big Boss')
        self.assertEqual(sale['units'], 5)
        self.assertEqual(len(sale['items']), 2)
        names = {line['product_name']: line for line in sale['items']}
        chips = names['Chips']
        self.assertEqual(chips['unit_price'], '9.00')
        self.assertEqual(chips['quantity'], 2)
        self.assertEqual(chips['gross'], '18.00')
        self.assertEqual(chips['discount'], '1.80')
        self.assertEqual(chips['line_total'], '16.20')
        self.assertEqual(names['Apple Juice']['line_total'], '6.00')
        self.assertEqual(sale['subtotal'], '24.00')
        self.assertEqual(sale['discount'], '1.80')
        self.assertEqual(sale['total'], '22.20')
        self.assertEqual(sale['amount_paid'], '22.20')
        self.assertEqual(sale['outstanding'], '0.00')

    def test_totals_match_the_sales_report_for_the_same_day(self):
        self._sale('S-1', [(self.chips, 2, '10.00', '10')])
        self._sale('S-2', [(self.juice, 4, '2.00', '0')], method='CARD')
        self._sale('S-3', [(self.chips, 5, '10.00', '0')], status=Sale.PENDING)
        self._sale('S-4', [(self.chips, 5, '10.00', '0')], status=Sale.CANCELLED)

        daily = self._daily()['summary']
        today = self.today.isoformat()
        report = self.client.get(
            self.REPORT_URL, {'start_date': today, 'end_date': today},
        ).json()['summary']
        for key in ('gross_sales', 'discounts', 'refunds', 'net_sales',
                    'transactions', 'units_sold', 'status_counts'):
            self.assertEqual(daily[key], report[key], key)
        self.assertEqual(daily['transactions'], 2)
        self.assertEqual(daily['net_sales'], '26.00')
        self.assertEqual(daily['status_counts'], {'COMPLETED': 2, 'PENDING': 1, 'CANCELLED': 1})

    def test_pending_and_cancelled_are_listed_but_not_counted(self):
        self._sale('S-1', [(self.chips, 1, '10.00', '0')])
        pending = self._sale('S-2', [(self.chips, 1, '10.00', '0')], status=Sale.PENDING,
                             payment_status=Sale.UNPAID)
        data = self._daily()
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['summary']['transactions'], 1)
        row = next(r for r in data['results'] if r['id'] == pending.id)
        self.assertIsNone(row['amount_paid'])
        self.assertIsNone(row['outstanding'])

    def test_specific_date_and_invalid_date(self):
        self._sale('S-OLD', [(self.chips, 1, '10.00', '0')], days_ago=2)
        day = (self.today - timedelta(days=2)).isoformat()
        data = self._daily(date=day)
        self.assertEqual([r['reference'] for r in data['results']], ['S-OLD'])
        bad = self.client.get(self.URL, {'date': '25-10-2026'})
        self.assertEqual(bad.status_code, 400)

    def test_filters_and_search(self):
        self._sale('S-CASH', [(self.chips, 1, '10.00', '0')], customer=self.customer)
        self._sale('S-CARD', [(self.juice, 1, '2.00', '0')], method='CARD', sold_by=self.cashier)

        self.assertEqual(self._daily(payment_method='CARD')['count'], 1)
        self.assertEqual(self._daily(sold_by=self.cashier.id)['results'][0]['reference'], 'S-CARD')
        self.assertEqual(self._daily(status='PENDING')['count'], 0)
        self.assertEqual(self._daily(search='alice')['results'][0]['reference'], 'S-CASH')
        self.assertEqual(self._daily(search='S-CARD')['count'], 1)
        # Product name search finds the receipt once, and still lists all its lines.
        self._sale('S-MIX', [(self.chips, 1, '10.00', '0'), (self.juice, 1, '2.00', '0')])
        found = self._daily(search='apple')
        refs = sorted(r['reference'] for r in found['results'])
        self.assertEqual(refs, ['S-CARD', 'S-MIX'])
        mix = next(r for r in found['results'] if r['reference'] == 'S-MIX')
        self.assertEqual(len(mix['items']), 2)
        self.assertEqual(self._daily(search='no-such-thing')['count'], 0)

    def test_pagination_and_ordering_cover_every_receipt_once(self):
        for index in range(5):
            sale = self._sale(f'S-{index}', [(self.chips, 1, '10.00', '0')])
            Sale.objects.filter(pk=sale.pk).update(
                created_at=timezone.now() - timedelta(minutes=10 * (5 - index)),
            )
        first = self._daily(limit=2, offset=0)
        second = self._daily(limit=2, offset=2)
        third = self._daily(limit=2, offset=4)
        refs = [r['reference'] for page in (first, second, third) for r in page['results']]
        self.assertEqual(refs, ['S-4', 'S-3', 'S-2', 'S-1', 'S-0'])
        self.assertTrue(first['has_more'])
        self.assertFalse(third['has_more'])
        self.assertEqual(first['count'], 5)
        # Summary describes the whole day, not just the page.
        self.assertEqual(first['summary']['transactions'], 5)
        ascending = self._daily(order='asc')
        self.assertEqual([r['reference'] for r in ascending['results']],
                         ['S-0', 'S-1', 'S-2', 'S-3', 'S-4'])

    def test_payment_breakdown_and_collections(self):
        self._sale('S-1', [(self.chips, 1, '10.00', '0')])
        self._sale('S-2', [(self.juice, 2, '2.00', '0')], method='CARD')
        self._sale('S-3', [(self.chips, 1, '10.00', '0')], payment_status=Sale.UNPAID)
        self._sale('S-4', [(self.chips, 1, '10.00', '0')], payment_status=Sale.PARTIALLY_PAID)
        data = self._daily()
        by_method = {entry['method']: entry for entry in data['payment_breakdown']}
        self.assertEqual(by_method['CASH']['count'], 3)
        self.assertEqual(by_method['CASH']['amount'], '30.00')
        self.assertEqual(by_method['CARD']['amount'], '4.00')
        summary = data['summary']
        self.assertEqual(summary['collected'], '14.00')
        self.assertEqual(summary['outstanding'], '10.00')
        self.assertEqual(summary['partially_paid_count'], 1)
        partial = next(r for r in data['results'] if r['reference'] == 'S-4')
        self.assertIsNone(partial['amount_paid'])

    def test_refunds_reduce_net_and_are_listed_on_the_receipt(self):
        sale = self._sale('S-1', [(self.chips, 3, '10.00', '0')], customer=self.customer)
        Return.objects.create(
            return_number='R-1', return_type='SALES', sale=sale, customer=self.customer,
            status='APPROVED', reason='Damaged', total_amount=Decimal('10.00'),
            refund_amount=Decimal('10.00'), refund_method='CASH',
            approved_at=timezone.now(), created_by=self.admin, updated_by=self.admin,
        )
        Return.objects.create(
            return_number='R-2', return_type='SALES', sale=sale, customer=self.customer,
            status='PENDING', reason='Maybe', total_amount=Decimal('10.00'),
            created_by=self.admin, updated_by=self.admin,
        )
        data = self._daily()
        self.assertEqual(data['summary']['refunds'], '10.00')
        self.assertEqual(data['summary']['net_sales'], '20.00')
        refunds = data['results'][0]['refunds']
        self.assertEqual([r['return_number'] for r in refunds], ['R-1'])
        self.assertEqual(refunds[0]['amount'], '10.00')

    def test_cash_received_and_change_are_reported(self):
        sale = self._sale('S-1', [(self.chips, 1, '10.00', '0')])
        Sale.objects.filter(pk=sale.pk).update(amount_received=Decimal('20.00'),
                                               change_amount=Decimal('10.00'))
        row = self._daily()['results'][0]
        self.assertEqual(row['cash_received'], '20.00')
        self.assertEqual(row['change'], '10.00')

    def test_query_count_does_not_grow_with_receipts(self):
        for index in range(3):
            self._sale(f'A-{index}', [(self.chips, 1, '10.00', '0'), (self.juice, 1, '2.00', '0')])
        with self.assertNumQueries(self._query_count()):
            self._daily()
        for index in range(10):
            self._sale(f'B-{index}', [(self.chips, 1, '10.00', '0'), (self.juice, 1, '2.00', '0')])
        with self.assertNumQueries(self._query_count()):
            self._daily()

    def _query_count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.URL)
        return len(ctx)

    def test_branch_scoping_hides_other_branch_sales(self):
        from Core.models import Branch
        mine = Branch.objects.create(name='Main', code='MAIN', created_by=self.admin, updated_by=self.admin)
        other = Branch.objects.create(name='Other', code='OTH', created_by=self.admin, updated_by=self.admin)
        self.cashier.branch = mine
        self.cashier.save(update_fields=['branch'])
        visible = self._sale('S-MINE', [(self.chips, 1, '10.00', '0')])
        hidden = self._sale('S-OTHER', [(self.chips, 1, '10.00', '0')])
        Sale.objects.filter(pk=visible.pk).update(branch=mine)
        Sale.objects.filter(pk=hidden.pk).update(branch=other)
        data = self._daily(user=self.cashier)
        self.assertEqual([r['reference'] for r in data['results']], ['S-MINE'])

    def test_requires_authentication_and_sales_read_scope(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.get(self.URL).status_code, 401)
        # Auditors have read access to sales, so they may open the report.
        response = APIClient()
        response.force_authenticate(self.auditor)
        self.assertEqual(response.get(self.URL).status_code, 200)
        # A role without the sales scope is refused.
        outsider = User.objects.create_user(
            username='x', email='x@example.com', password='secret123',
            first_name='No', last_name='Access', role=User.SALES,
        )
        User.objects.filter(pk=outsider.pk).update(role='NONE')
        blocked = APIClient()
        blocked.force_authenticate(User.objects.get(pk=outsider.pk))
        self.assertEqual(blocked.get(self.URL).status_code, 403)
