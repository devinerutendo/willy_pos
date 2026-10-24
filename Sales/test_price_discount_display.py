from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from Core.models import Branch, POSTerminal
from Inventory.models import Category, Location, Product, Stock
from Sales import invoicing
from Sales.models import Invoice, Sale, SaleItem
from Sales.test_invoice_approval import InvoiceFixtureMixin


class SaleHistoryDiscountTests(TestCase):
    """Edited selling prices are kept against the catalogue price and shown as a discount."""

    def setUp(self):
        from Accounts.models import User

        self.manager = User.objects.create_user(
            username='mgr', email='mgr@example.com', password='x', role=User.MANAGER,
        )
        self.branch = Branch.objects.create(
            name='Main', code='MN', created_by=self.manager, updated_by=self.manager,
        )
        self.manager.branch = self.branch
        self.manager.save(update_fields=['branch'])
        self.terminal = POSTerminal.objects.create(
            branch=self.branch, name='T1', code='T1',
            created_by=self.manager, updated_by=self.manager,
        )
        category = Category.objects.create(name='Drinks', created_by=self.manager, updated_by=self.manager)
        self.location = Location.objects.create(
            branch=self.branch, name='Store', address='x',
            created_by=self.manager, updated_by=self.manager,
        )
        # 4 for 10.00 -> minimum unit price 2.50
        self.bundled = Product.objects.create(
            name='Soda', category=category, cost_price=Decimal('1.00'),
            selling_price=Decimal('3.00'), bundle_quantity=4, bundle_price=Decimal('10.00'),
            created_by=self.manager, updated_by=self.manager,
        )
        self.plain = Product.objects.create(
            name='Water', category=category, cost_price=Decimal('0.10'),
            selling_price=Decimal('1.00'),
            created_by=self.manager, updated_by=self.manager,
        )
        for product in (self.bundled, self.plain):
            Stock.objects.create(product=product, location=self.location, quantity=100, reserved_quantity=0)
        self.client = APIClient()
        self.client.force_authenticate(user=self.manager)

    def _sell(self, items):
        response = self.client.post(reverse('sale-list'), {
            'branch': self.branch.id,
            'pos_terminal': self.terminal.id,
            'payment_method': 'CASH',
            'items': items,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return self.client.get(reverse('sale-detail', args=[response.data['id']])).data

    def test_edited_price_records_catalogue_price_and_discount(self):
        data = self._sell([{'product': self.bundled.id, 'quantity': 2, 'unit_price': '2.60'}])
        line = data['items'][0]
        self.assertEqual(Decimal(line['list_price']), Decimal('3.00'))
        self.assertEqual(Decimal(line['list_total_price']), Decimal('6.00'))
        self.assertEqual(Decimal(line['total_price']), Decimal('5.20'))
        self.assertEqual(Decimal(line['price_discount']), Decimal('0.80'))
        self.assertEqual(Decimal(data['subtotal_amount']), Decimal('6.00'))
        self.assertEqual(Decimal(data['total_amount']), Decimal('5.20'))
        self.assertEqual(Decimal(data['discount_total']), Decimal('0.80'))

    def test_discount_is_bundle_aware(self):
        # 5 units: one bundle of 4 for 10.00 plus 1 at the edited price.
        data = self._sell([{'product': self.bundled.id, 'quantity': 5, 'unit_price': '2.60'}])
        line = data['items'][0]
        self.assertEqual(Decimal(line['total_price']), Decimal('12.60'))
        self.assertEqual(Decimal(line['list_total_price']), Decimal('13.00'))
        self.assertEqual(Decimal(line['price_discount']), Decimal('0.40'))

    def test_unedited_price_has_no_discount(self):
        data = self._sell([{'product': self.plain.id, 'quantity': 3, 'unit_price': '1.00'}])
        self.assertEqual(Decimal(data['items'][0]['price_discount']), Decimal('0.00'))
        self.assertEqual(Decimal(data['discount_total']), Decimal('0.00'))
        self.assertEqual(Decimal(data['subtotal_amount']), Decimal(data['total_amount']))

    def test_raised_price_is_not_hidden_as_a_discount(self):
        data = self._sell([{'product': self.plain.id, 'quantity': 2, 'unit_price': '1.50'}])
        self.assertEqual(Decimal(data['items'][0]['price_discount']), Decimal('-1.00'))

    def test_totals_combine_every_line(self):
        data = self._sell([
            {'product': self.bundled.id, 'quantity': 2, 'unit_price': '2.60'},
            {'product': self.plain.id, 'quantity': 2, 'unit_price': '0.50'},
        ])
        self.assertEqual(Decimal(data['subtotal_amount']), Decimal('8.00'))
        self.assertEqual(Decimal(data['total_amount']), Decimal('6.20'))
        self.assertEqual(Decimal(data['discount_total']), Decimal('1.80'))

    def test_legacy_line_without_catalogue_price_shows_no_edit_discount(self):
        sale = Sale.objects.create(
            reference='LEGACY-1', branch=self.branch, pos_terminal=self.terminal, sold_by=self.manager,
            status=Sale.COMPLETED, inventory_completed=True,
            created_by=self.manager, updated_by=self.manager,
        )
        SaleItem.objects.create(sale=sale, product=self.plain, quantity=2, unit_price=Decimal('0.80'))
        data = self.client.get(reverse('sale-detail', args=[sale.pk])).data
        line = data['items'][0]
        self.assertIsNone(line['list_price'])
        self.assertEqual(Decimal(line['price_discount']), Decimal('0.00'))
        self.assertEqual(Decimal(data['discount_total']), Decimal('0.00'))

    def test_admin_discount_on_legacy_line_is_still_counted(self):
        sale = Sale.objects.create(
            reference='LEGACY-2', branch=self.branch, pos_terminal=self.terminal, sold_by=self.manager,
            status=Sale.COMPLETED, inventory_completed=True,
            created_by=self.manager, updated_by=self.manager,
        )
        SaleItem.objects.create(
            sale=sale, product=self.plain, quantity=2, unit_price=Decimal('1.00'),
            discount_amount=Decimal('0.25'),
        )
        data = self.client.get(reverse('sale-detail', args=[sale.pk])).data
        self.assertEqual(Decimal(data['discount_total']), Decimal('0.50'))


class InvoiceDiscountTests(InvoiceFixtureMixin, TestCase):
    def setUp(self):
        self.build_fixture()

    def _invoice_at(self, price):
        return invoicing.create_invoice(
            customer=self.customer,
            items=[{'product': self.product, 'quantity': 4, 'unit_price': Decimal(price)}],
            user=self.sales_user, branch=self.branch,
        )

    def _detail(self, invoice):
        self.client.force_authenticate(user=self.manager)
        return self.client.get(reverse('invoice-detail', args=[invoice.pk])).data

    def test_invoice_records_catalogue_price_and_discount(self):
        invoice = self._invoice_at('9.00')
        data = self._detail(invoice)
        line = data['items'][0]
        self.assertEqual(Decimal(line['list_price']), Decimal('10.00'))
        self.assertEqual(Decimal(line['price_discount']), Decimal('4.00'))
        self.assertEqual(Decimal(data['subtotal_amount']), Decimal('40.00'))
        self.assertEqual(Decimal(data['total_amount']), Decimal('36.00'))
        self.assertEqual(Decimal(data['discount_total']), Decimal('4.00'))

    def test_invoice_at_catalogue_price_has_no_discount(self):
        data = self._detail(self._invoice_at('10.00'))
        self.assertEqual(Decimal(data['discount_total']), Decimal('0.00'))

    def test_editing_an_invoice_keeps_the_original_catalogue_price(self):
        invoice = self._invoice_at('9.00')
        self.product.selling_price = Decimal('12.00')
        self.product.save(update_fields=['selling_price'])
        invoicing.update_invoice(
            invoice.pk, self.sales_user,
            items=[{'product': self.product, 'quantity': 5, 'unit_price': Decimal('9.00')}],
        )
        line = self._detail(invoice)['items'][0]
        self.assertEqual(Decimal(line['list_price']), Decimal('10.00'))
        self.assertEqual(Decimal(line['price_discount']), Decimal('5.00'))

    def test_approved_sale_carries_the_discount(self):
        invoice = self._invoice_at('9.00')
        self.approve(invoice)
        invoice.refresh_from_db()
        data = self.client.get(reverse('sale-detail', args=[invoice.sale_id])).data
        self.assertEqual(Decimal(data['items'][0]['list_price']), Decimal('10.00'))
        self.assertEqual(Decimal(data['subtotal_amount']), Decimal('40.00'))
        self.assertEqual(Decimal(data['discount_total']), Decimal('4.00'))
        self.assertEqual(Decimal(data['total_amount']), Decimal('36.00'))

    def test_legacy_invoice_line_without_catalogue_price(self):
        invoice = self._invoice_at('9.00')
        invoice.items.update(list_price=None)
        data = self._detail(invoice)
        self.assertIsNone(data['items'][0]['list_price'])
        self.assertEqual(Decimal(data['discount_total']), Decimal('0.00'))
        self.assertEqual(invoice.status, Invoice.PENDING_APPROVAL)
