from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from Accounts.models import User
from Core.models import AuditLog, Branch, POSTerminal
from Inventory.models import Category, Location, Product, Stock
from Sales.models import Sale, SaleItem
from Sales.pricing import bundle_unit_floor
from Sales.views import SaleItemViewSet, SaleViewSet


class SellingPriceOverrideTests(TestCase):
    """Cart price edits: bundle floor, RBAC, server totals and audit."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.manager = User.objects.create_user(
            username='mgr', email='mgr@example.com', password='x', role=User.MANAGER,
        )
        self.cashier = User.objects.create_user(
            username='cash', email='cash@example.com', password='x', role=User.SALES,
        )
        self.branch = Branch.objects.create(
            name='Main', code='MN', created_by=self.manager, updated_by=self.manager,
        )
        for user in (self.manager, self.cashier):
            user.branch = self.branch
            user.save(update_fields=['branch'])
        self.terminal = POSTerminal.objects.create(
            branch=self.branch, name='T1', code='T1',
            created_by=self.manager, updated_by=self.manager,
        )
        category = Category.objects.create(name='Drinks', created_by=self.manager, updated_by=self.manager)
        self.location = Location.objects.create(
            branch=self.branch, name='Store', address='x',
            created_by=self.manager, updated_by=self.manager,
        )
        # 6 for 10.00 -> per-unit floor 1.666... rounds up to 1.67
        self.bundled = Product.objects.create(
            name='Energy Drink', category=category, cost_price=Decimal('1.00'),
            selling_price=Decimal('2.00'), bundle_quantity=6, bundle_price=Decimal('10.00'),
            created_by=self.manager, updated_by=self.manager,
        )
        # 4 for 10.00 -> exact floor 2.50
        self.exact = Product.objects.create(
            name='Soda', category=category, cost_price=Decimal('1.00'),
            selling_price=Decimal('3.00'), bundle_quantity=4, bundle_price=Decimal('10.00'),
            created_by=self.manager, updated_by=self.manager,
        )
        self.plain = Product.objects.create(
            name='Water', category=category, cost_price=Decimal('0.10'),
            selling_price=Decimal('1.00'),
            created_by=self.manager, updated_by=self.manager,
        )
        for product in (self.bundled, self.exact, self.plain):
            Stock.objects.create(product=product, location=self.location, quantity=100, reserved_quantity=0)

    def _post(self, user, items):
        request = self.factory.post(
            '/api/sales/sales/',
            {
                'branch': self.branch.id,
                'pos_terminal': self.terminal.id,
                'payment_method': 'CASH',
                'items': items,
            },
            format='json',
        )
        force_authenticate(request, user=user)
        return SaleViewSet.as_view({'post': 'create'})(request)

    def _item(self, product, price, quantity=1):
        return {'product': product.id, 'quantity': quantity, 'unit_price': price}

    def _assert_rejected(self, response, code, http_status=status.HTTP_400_BAD_REQUEST):
        self.assertEqual(response.status_code, http_status, response.data)
        self.assertEqual(response.data['code'], code)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(SaleItem.objects.count(), 0)
        for stock in Stock.objects.all():
            self.assertEqual(stock.reserved_quantity, 0)

    # --- floor math -------------------------------------------------------

    def test_floor_is_per_unit_and_rounded_up(self):
        self.assertEqual(bundle_unit_floor(self.bundled), Decimal('1.67'))
        self.assertEqual(bundle_unit_floor(self.exact), Decimal('2.50'))
        self.assertIsNone(bundle_unit_floor(self.plain))

    # --- minimum price ----------------------------------------------------

    def test_price_equal_to_minimum_is_accepted(self):
        response = self._post(self.manager, [self._item(self.exact, '2.50')])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_price_above_minimum_is_accepted(self):
        response = self._post(self.manager, [self._item(self.exact, '2.75')])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_price_below_minimum_is_rejected_with_structured_error(self):
        response = self._post(self.manager, [self._item(self.exact, '2.49')])
        self._assert_rejected(response, 'price_below_minimum')
        line = response.data['items'][0]
        self.assertEqual(line['index'], 0)
        self.assertEqual(line['product_id'], self.exact.id)
        self.assertEqual(line['minimum_price'], '2.50')
        self.assertEqual(line['submitted_price'], '2.49')
        self.assertIn('bundle price', response.data['detail'])

    def test_rounded_up_floor_boundary(self):
        self._assert_rejected(
            self._post(self.manager, [self._item(self.bundled, '1.66')]),
            'price_below_minimum',
        )
        self.assertEqual(
            self._post(self.manager, [self._item(self.bundled, '1.67')]).status_code,
            status.HTTP_201_CREATED,
        )

    def test_whole_bundle_price_is_not_the_per_unit_minimum(self):
        # 2.00 is far below the 10.00 bundle total but above the per-unit floor.
        response = self._post(self.manager, [self._item(self.bundled, '1.80', quantity=2)])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_product_without_bundle_has_no_minimum(self):
        response = self._post(self.manager, [self._item(self.plain, '0.01')])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_one_bad_line_rejects_the_whole_sale(self):
        response = self._post(self.manager, [
            self._item(self.plain, '1.00'),
            self._item(self.exact, '1.00'),
        ])
        self._assert_rejected(response, 'price_below_minimum')
        self.assertEqual([i['index'] for i in response.data['items']], [1])

    # --- malformed input --------------------------------------------------

    def test_invalid_prices_are_rejected(self):
        for bad in ('-1.00', '', None, 'abc', '3.456', '1e2x', '999999999999.00'):
            with self.subTest(price=bad):
                response = self._post(self.manager, [self._item(self.exact, bad)])
                self._assert_rejected(response, 'price_invalid')

    # --- permissions ------------------------------------------------------

    def test_unauthorised_user_cannot_override_price(self):
        response = self._post(self.cashier, [self._item(self.plain, '0.50')])
        self._assert_rejected(response, 'price_override_forbidden', status.HTTP_403_FORBIDDEN)

    def test_unauthorised_user_cannot_raise_price_either(self):
        response = self._post(self.cashier, [self._item(self.plain, '1.50')])
        self._assert_rejected(response, 'price_override_forbidden', status.HTTP_403_FORBIDDEN)

    def test_unauthorised_user_can_sell_at_catalogue_price(self):
        response = self._post(self.cashier, [self._item(self.exact, '3.00')])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_stored_line_price_cannot_be_patched_below_minimum(self):
        created = self._post(self.manager, [self._item(self.exact, '3.00')])
        item = SaleItem.objects.get(sale_id=created.data['id'])
        view = SaleItemViewSet.as_view({'patch': 'partial_update'})

        request = self.factory.patch(
            f'/api/sales/sale-items/{item.id}/', {'quantity': 1, 'unit_price': '1.00'}, format='json',
        )
        force_authenticate(request, user=self.manager)
        response = view(request, pk=item.id)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(response.data['code'], 'price_below_minimum')

        request = self.factory.patch(
            f'/api/sales/sale-items/{item.id}/', {'quantity': 1, 'unit_price': '2.00'}, format='json',
        )
        force_authenticate(request, user=self.cashier)
        response = view(request, pk=item.id)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal('3.00'))

    # --- totals, persistence and audit ------------------------------------

    def test_edited_price_is_used_for_server_totals_with_bundle(self):
        # 7 units: one 6-pack for 10.00 plus one loose unit at the edited price.
        response = self._post(self.manager, [self._item(self.bundled, '2.40', quantity=7)])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        sale = Sale.objects.get(id=response.data['id'])
        self.assertEqual(sale.items.get().unit_price, Decimal('2.40'))
        self.assertEqual(sale.total_amount, Decimal('12.40'))
        self.assertEqual(Decimal(str(response.data['total_amount'])), Decimal('12.40'))

    def test_below_bundle_quantity_uses_edited_price_per_unit(self):
        response = self._post(self.manager, [self._item(self.bundled, '1.90', quantity=3)])
        sale = Sale.objects.get(id=response.data['id'])
        self.assertEqual(sale.total_amount, Decimal('5.70'))

    def test_override_is_audited_with_original_and_new_price(self):
        response = self._post(self.manager, [self._item(self.exact, '2.75', quantity=2)])
        sale = Sale.objects.get(id=response.data['id'])
        item = sale.items.get()
        entry = AuditLog.objects.get(
            model_label='Sales.SaleItem', object_pk=str(item.pk),
            object_repr__startswith='Price override',
        )
        self.assertEqual(entry.actor, self.manager)
        self.assertEqual(entry.branch, self.branch)
        self.assertEqual(entry.changes['unit_price'], {'from': '3.00', 'to': '2.75'})
        self.assertEqual(entry.changes['sale_reference']['to'], sale.reference)
        self.assertEqual(entry.changes['minimum_unit_price']['to'], '2.50')
        self.assertIsNotNone(entry.created_at)

    def test_catalogue_price_sale_writes_no_override_audit(self):
        self._post(self.manager, [self._item(self.exact, '3.00')])
        self.assertFalse(
            AuditLog.objects.filter(object_repr__startswith='Price override').exists()
        )

    def test_rejected_sale_writes_no_override_audit(self):
        self._post(self.manager, [self._item(self.exact, '1.00')])
        self.assertFalse(
            AuditLog.objects.filter(object_repr__startswith='Price override').exists()
        )

    def test_confirm_keeps_edited_price_and_deducts_stock_once(self):
        response = self._post(self.manager, [self._item(self.exact, '2.75', quantity=2)])
        sale_id = response.data['id']
        confirm = self.factory.post(f'/api/sales/sales/{sale_id}/confirm/')
        force_authenticate(confirm, user=self.manager)
        confirm_response = SaleViewSet.as_view({'post': 'confirm'})(confirm, pk=sale_id)
        self.assertEqual(confirm_response.status_code, status.HTTP_200_OK, confirm_response.data)

        sale = Sale.objects.get(id=sale_id)
        self.assertEqual(sale.items.get().unit_price, Decimal('2.75'))
        self.assertEqual(sale.total_amount, Decimal('5.50'))
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Stock.objects.get(product=self.exact).quantity, 98)
