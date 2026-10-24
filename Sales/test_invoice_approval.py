import threading
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from Accounts.models import User
from Core.models import Branch
from Inventory.models import Category, Location, Product, Stock, StockTransaction
from Sales import invoicing
from Sales.models import Customer, Invoice, InvoiceStatusHistory, Quotation, QuotationItem, Sale
from Sales.reporting import build_sales_report


class InvoiceFixtureMixin:
    def build_fixture(self):
        self.admin = User.objects.create_user(
            username='admin', email='admin@example.com', password='x', role=User.ADMIN,
            is_superuser=True, first_name='Ada', last_name='Admin',
        )
        self.branch = Branch.objects.create(
            name='Downtown', code='DTN', created_by=self.admin, updated_by=self.admin,
        )
        self.users = {}
        for role in (User.SALES, User.MANAGER, User.AUDITOR, User.INVENTORY):
            user = User.objects.create_user(
                username=role.lower(), email=f'{role.lower()}@example.com', password='x',
                role=role, first_name=role.title(), last_name='User',
            )
            user.branch = self.branch
            user.save(update_fields=['branch'])
            self.users[role] = user
        self.sales_user = self.users[User.SALES]
        self.manager = self.users[User.MANAGER]

        category = Category.objects.create(
            name='Snacks', description='x', created_by=self.admin, updated_by=self.admin,
        )
        self.location = Location.objects.create(
            name='Shelf A', created_by=self.admin, updated_by=self.admin,
        )
        self.product = self._product(category, 'Chips', '10.00', 10)
        self.product_b = self._product(category, 'Cola', '4.00', 5)
        self.customer = Customer.objects.create(
            name='Acme', email='acme@example.com', phone='1', address='1 Road',
            created_by=self.admin, updated_by=self.admin,
        )
        self.client = APIClient()

    def _product(self, category, name, price, stock):
        product = Product.objects.create(
            name=name, category=category, cost_price=Decimal('1.00'),
            selling_price=Decimal(price), created_by=self.admin, updated_by=self.admin,
        )
        Stock.objects.create(product=product, location=self.location, quantity=stock)
        return product

    def make_invoice(self, qty=4, qty_b=0, user=None, submit=True):
        items = [{'product': self.product, 'quantity': qty, 'unit_price': Decimal('10.00')}]
        if qty_b:
            items.append({'product': self.product_b, 'quantity': qty_b, 'unit_price': Decimal('4.00')})
        return invoicing.create_invoice(
            customer=self.customer, items=items, user=user or self.sales_user,
            branch=self.branch, submit=submit,
        )

    def stock(self, product=None):
        return Stock.objects.get(product=product or self.product, location=self.location)

    def approve(self, invoice, user=None):
        self.client.force_authenticate(user=user or self.manager)
        return self.client.post(reverse('invoice-approve', args=[invoice.pk]))


class InvoiceApprovalTests(InvoiceFixtureMixin, TestCase):
    def setUp(self):
        self.build_fixture()

    # 1: creating an invoice is not a sale -------------------------------
    def test_creating_invoice_creates_no_sale_and_no_deduction(self):
        self.client.force_authenticate(user=self.sales_user)
        response = self.client.post(reverse('invoice-list'), {
            'customer': self.customer.pk,
            'items': [{'product': self.product.pk, 'quantity': 4, 'unit_price': '10.00'}],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['status'], Invoice.PENDING_APPROVAL)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.stock().quantity, 10)
        self.assertEqual(self.stock().reserved_quantity, 0)
        self.assertFalse(StockTransaction.objects.exists())

    def test_draft_invoice_is_not_a_sale(self):
        invoice = self.make_invoice(submit=False)
        self.assertEqual(invoice.status, Invoice.DRAFT)
        self.assertEqual(Sale.objects.count(), 0)

    # 2: pending excluded from reports -----------------------------------
    def test_pending_invoice_is_excluded_from_daily_totals(self):
        self.make_invoice()
        self.client.force_authenticate(user=self.manager)
        today = timezone.localdate().isoformat()
        response = self.client.get(reverse('sale-report'), {'start_date': today, 'end_date': today})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data['summary']['gross_sales']), Decimal('0.00'))
        self.assertEqual(response.data['summary']['transactions'], 0)

    # 3-5: authorized approval -------------------------------------------
    def test_authorized_approval_creates_one_sale_and_deducts_once(self):
        invoice = self.make_invoice(qty=4, qty_b=2)
        response = self.approve(invoice)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data['created'])

        invoice.refresh_from_db()
        sale = Sale.objects.get()
        self.assertEqual(invoice.status, Invoice.APPROVED)
        self.assertEqual(invoice.sale_id, sale.pk)
        self.assertEqual(invoice.approved_by_id, self.manager.pk)
        self.assertIsNotNone(invoice.approved_at)
        self.assertEqual(sale.status, Sale.COMPLETED)
        self.assertEqual(sale.total_amount, Decimal('48.00'))
        self.assertEqual(sale.items.count(), 2)
        self.assertEqual(self.stock().quantity, 6)
        self.assertEqual(self.stock(self.product_b).quantity, 3)

        moves = StockTransaction.objects.filter(reference=invoice.reference)
        self.assertEqual(moves.count(), 2)
        move = moves.get(product=self.product)
        self.assertEqual(move.transaction_type, StockTransaction.OUT)
        self.assertEqual(move.quantity, -4)
        self.assertEqual(move.created_by_id, self.manager.pk)
        self.assertIn(sale.reference, move.notes)
        self.assertIn(str(sale.pk), move.notes)

    def test_approved_sale_is_unpaid_and_completed(self):
        invoice = self.make_invoice()
        self.approve(invoice)
        sale = Sale.objects.get()
        self.assertEqual(sale.status, Sale.COMPLETED)
        self.assertEqual(sale.payment_status, Sale.UNPAID)

    def test_payment_status_is_independent_of_sale_status(self):
        invoice = self.make_invoice()
        self.approve(invoice)
        response = self.client.post(reverse('invoice-mark-paid', args=[invoice.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        sale = Sale.objects.get()
        self.assertEqual((sale.status, sale.payment_status), (Sale.COMPLETED, Sale.PAID))
        self.assertEqual(response.data['payment_status'], Sale.PAID)
        self.assertEqual(response.data['status'], Invoice.APPROVED)

    def test_admin_can_approve_and_sale_serializer_links_back(self):
        invoice = self.make_invoice()
        response = self.approve(invoice, user=self.admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        sale = Sale.objects.get()
        self.client.force_authenticate(user=self.manager)
        data = self.client.get(reverse('sale-detail', args=[sale.pk])).data
        self.assertEqual(data['invoice_reference'], invoice.reference)
        self.assertEqual(data['payment_status'], Sale.UNPAID)

    # 6: report day is approval day --------------------------------------
    def test_sale_is_reported_on_the_approval_day_not_the_invoice_day(self):
        invoice = self.make_invoice()
        Invoice.objects.filter(pk=invoice.pk).update(created_at=timezone.now() - timedelta(days=3))
        self.approve(invoice)
        today = timezone.localdate().isoformat()
        created_day = (timezone.localdate() - timedelta(days=3)).isoformat()

        report = build_sales_report(self.manager, {'start_date': today, 'end_date': today})
        self.assertEqual(report['summary']['transactions'], 1)
        report = build_sales_report(self.manager, {'start_date': created_day, 'end_date': created_day})
        self.assertEqual(report['summary']['transactions'], 0)

    # 7: unauthorized ----------------------------------------------------
    def test_roles_without_approval_permission_are_blocked_everywhere(self):
        for role in (User.SALES, User.AUDITOR, User.INVENTORY):
            invoice = self.make_invoice(qty=1)
            response = self.approve(invoice, user=self.users[role])
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, role)
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, Invoice.PENDING_APPROVAL)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.stock().quantity, 10)

    def test_unauthorized_attempts_are_logged_in_history(self):
        invoice = self.make_invoice(qty=1)
        self.approve(invoice, user=self.sales_user)
        entry = invoice.history.get(action=InvoiceStatusHistory.APPROVAL_FAILED)
        self.assertEqual(entry.detail['code'], 'permission_denied')
        self.assertEqual(entry.actor_id, self.sales_user.pk)

    def test_unauthorized_users_cannot_reject_and_see_no_approve_flag(self):
        invoice = self.make_invoice()
        self.client.force_authenticate(user=self.sales_user)
        response = self.client.post(reverse('invoice-reject', args=[invoice.pk]), {'reason': 'no'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        data = self.client.get(reverse('invoice-detail', args=[invoice.pk])).data
        self.assertFalse(data['can_approve'])
        self.client.force_authenticate(user=self.manager)
        self.assertTrue(self.client.get(reverse('invoice-detail', args=[invoice.pk])).data['can_approve'])

    def test_anonymous_cannot_approve(self):
        invoice = self.make_invoice()
        self.client.force_authenticate(user=None)
        response = self.client.post(reverse('invoice-approve', args=[invoice.pk]))
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    # 8: insufficient stock ----------------------------------------------
    def test_insufficient_stock_blocks_approval_and_changes_nothing(self):
        invoice = self.make_invoice(qty=4, qty_b=9)  # cola only has 5
        response = self.approve(invoice)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'insufficient_stock')
        shortage = response.data['shortages'][0]
        self.assertEqual(shortage['product_id'], self.product_b.pk)
        self.assertEqual((shortage['requested'], shortage['available']), (9, 5))

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.PENDING_APPROVAL)
        self.assertIsNone(invoice.sale_id)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.stock().quantity, 10)  # the in-stock line was not deducted
        self.assertEqual(self.stock(self.product_b).quantity, 5)
        self.assertFalse(StockTransaction.objects.exists())

    def test_blocked_approval_is_recorded_in_history(self):
        invoice = self.make_invoice(qty=40)
        self.approve(invoice)
        entry = invoice.history.get(action=InvoiceStatusHistory.APPROVAL_FAILED)
        self.assertEqual(entry.detail['code'], 'insufficient_stock')
        self.assertEqual(entry.detail['shortages'][0]['requested'], 40)
        self.assertIn('Insufficient stock', entry.reason)

    def test_existing_reservations_reduce_available_stock(self):
        Stock.objects.filter(pk=self.stock().pk).update(reserved_quantity=8)
        response = self.approve(self.make_invoice(qty=4))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['shortages'][0]['available'], 2)

    def test_stock_can_be_fixed_and_approval_retried(self):
        invoice = self.make_invoice(qty=12)
        self.assertEqual(self.approve(invoice).status_code, status.HTTP_409_CONFLICT)
        Stock.objects.filter(pk=self.stock().pk).update(quantity=20)
        self.assertEqual(self.approve(invoice).status_code, status.HTTP_200_OK)
        self.assertEqual(self.stock().quantity, 8)

    # 9: idempotency -----------------------------------------------------
    def test_repeated_approval_is_idempotent(self):
        invoice = self.make_invoice()
        first = self.approve(invoice)
        second = self.approve(invoice)
        third = self.approve(invoice, user=self.admin)
        self.assertTrue(first.data['created'])
        self.assertFalse(second.data['created'])
        self.assertFalse(third.data['created'])
        self.assertEqual(second.data['sale_id'], first.data['sale_id'])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock().quantity, 6)
        self.assertEqual(StockTransaction.objects.filter(reference=invoice.reference).count(), 1)
        self.assertEqual(invoice.history.filter(action=InvoiceStatusHistory.APPROVED).count(), 1)

    # 11: rejected / cancelled do not count ------------------------------
    def test_rejection_needs_a_reason_and_creates_no_sale(self):
        invoice = self.make_invoice()
        self.client.force_authenticate(user=self.manager)
        url = reverse('invoice-reject', args=[invoice.pk])
        self.assertEqual(self.client.post(url, {'reason': '  '}, format='json').status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(url, {'reason': 'Wrong prices'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.REJECTED)
        self.assertEqual(invoice.rejection_reason, 'Wrong prices')
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.stock().quantity, 10)
        self.assertEqual(self.approve(invoice).status_code, status.HTTP_409_CONFLICT)

    def test_cancelled_invoice_is_final_and_creates_no_sale(self):
        invoice = self.make_invoice()
        self.client.force_authenticate(user=self.sales_user)
        response = self.client.post(reverse('invoice-cancel', args=[invoice.pk]), {'reason': 'Customer left'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.CANCELLED)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.approve(invoice).status_code, status.HTTP_409_CONFLICT)

    def test_approved_invoice_cannot_be_cancelled_or_rejected(self):
        invoice = self.make_invoice()
        self.approve(invoice)
        self.client.force_authenticate(user=self.manager)
        for name, body in (('invoice-cancel', {'reason': 'x'}), ('invoice-reject', {'reason': 'x'})):
            response = self.client.post(reverse(name, args=[invoice.pk]), body, format='json')
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(response.data['code'], 'invalid_transition')
        self.assertEqual(Sale.objects.get().status, Sale.COMPLETED)

    def test_other_users_cannot_cancel_someone_elses_invoice(self):
        invoice = self.make_invoice(user=self.manager)
        self.client.force_authenticate(user=self.sales_user)
        response = self.client.post(reverse('invoice-cancel', args=[invoice.pk]), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_status_transition_table(self):
        for status_value in (Invoice.APPROVED, Invoice.REJECTED, Invoice.CANCELLED):
            self.assertEqual(Invoice.TRANSITIONS[status_value], frozenset())
        draft = self.make_invoice(submit=False)
        self.assertFalse(draft.can_transition_to(Invoice.APPROVED))
        invoicing.submit_invoice(draft.pk, self.sales_user)
        draft.refresh_from_db()
        self.assertTrue(draft.can_transition_to(Invoice.APPROVED))

    def test_edit_allowed_while_pending_but_not_after_approval(self):
        invoice = self.make_invoice(qty=2)
        self.client.force_authenticate(user=self.sales_user)
        url = reverse('invoice-detail', args=[invoice.pk])
        response = self.client.patch(url, {
            'items': [{'product': self.product.pk, 'quantity': 3, 'unit_price': '10.00'}],
            'notes': 'Updated',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(Invoice.objects.get().items.get().quantity, 3)
        self.approve(invoice)
        self.client.force_authenticate(user=self.sales_user)
        locked = self.client.patch(url, {'notes': 'Late'}, format='json')
        self.assertEqual(locked.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(locked.data['code'], 'invoice_locked')

    def test_discounts_require_admin_on_direct_creation(self):
        self.client.force_authenticate(user=self.sales_user)
        response = self.client.post(reverse('invoice-list'), {
            'customer': self.customer.pk,
            'items': [{'product': self.product.pk, 'quantity': 1, 'unit_price': '10.00', 'discount_percentage': '10'}],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Invoice.objects.count(), 0)

    # 12: audit trail ----------------------------------------------------
    def test_history_records_every_step(self):
        invoice = self.make_invoice()
        self.approve(invoice)
        actions = list(invoice.history.values_list('action', flat=True))
        self.assertEqual(actions, [InvoiceStatusHistory.CREATED, InvoiceStatusHistory.APPROVED])
        entry = invoice.history.get(action=InvoiceStatusHistory.APPROVED)
        self.assertEqual(entry.actor_id, self.manager.pk)
        self.assertEqual((entry.from_status, entry.to_status), (Invoice.PENDING_APPROVAL, Invoice.APPROVED))
        self.assertEqual(entry.sale_id, Sale.objects.get().pk)
        self.assertEqual(entry.detail['stock_movements'][0]['quantity'], 4)

        response = self.client.get(reverse('invoice-history', args=[invoice.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_history_is_append_only(self):
        invoice = self.make_invoice()
        entry = invoice.history.get()
        entry.reason = 'tampered'
        with self.assertRaises(ValidationError):
            entry.save()
        with self.assertRaises(ValidationError):
            entry.delete()
        with self.assertRaises(ValidationError):
            InvoiceStatusHistory.objects.all().update(reason='x')
        with self.assertRaises(ValidationError):
            InvoiceStatusHistory.objects.all().delete()
        self.assertEqual(invoice.history.get().reason, '')

    # database guarantees -----------------------------------------------
    def test_database_blocks_approved_status_without_sale(self):
        invoice = self.make_invoice()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.APPROVED)

    def test_database_allows_only_one_invoice_per_sale(self):
        invoice = self.make_invoice()
        self.approve(invoice)
        other = self.make_invoice()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.filter(pk=other.pk).update(status=Invoice.APPROVED, sale=invoice.sale)

    def test_negative_stock_is_impossible(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Stock.objects.filter(pk=self.stock().pk).update(quantity=-1)

    # 13: other modules keep working ------------------------------------
    def test_pos_sales_flow_still_deducts_and_marks_paid(self):
        self.client.force_authenticate(user=self.sales_user)
        response = self.client.post(reverse('sale-list'), {
            'customer': self.customer.pk,
            'items': [{'product': self.product.pk, 'quantity': 2, 'unit_price': '10.00'}],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        sale = Sale.objects.get()
        self.assertEqual(sale.status, Sale.PENDING)
        confirm = self.client.post(reverse('sale-confirm', args=[sale.pk]), {'payment_method': 'CARD'}, format='json')
        self.assertEqual(confirm.status_code, status.HTTP_200_OK, confirm.data)
        sale.refresh_from_db()
        self.assertEqual((sale.status, sale.payment_status), (Sale.COMPLETED, Sale.PAID))
        self.assertEqual(self.stock().quantity, 8)

    def test_approved_sale_is_not_deducted_again_by_inventory_task(self):
        from Sales.tasks import _complete_sale_inventory
        invoice = self.make_invoice()
        self.approve(invoice)
        sale = Sale.objects.get()
        self.assertTrue(sale.inventory_completed)
        self.assertEqual(self.stock().quantity, 6)

    def test_invoice_list_filters_by_status(self):
        pending = self.make_invoice()
        approved = self.make_invoice(qty=1)
        self.approve(approved)
        self.client.force_authenticate(user=self.sales_user)
        data = self.client.get(reverse('invoice-list'), {'status': 'PENDING_APPROVAL'}).data
        results = data['results'] if isinstance(data, dict) else data
        self.assertEqual([r['id'] for r in results], [pending.pk])

    def test_invoice_from_quotation_flow_end_to_end(self):
        quotation = Quotation.objects.create(
            customer=self.customer, branch=self.branch, reference='QT-1', status=Quotation.ACCEPTED,
            valid_until=timezone.now().date() + timedelta(days=5),
            created_by=self.admin, updated_by=self.admin,
        )
        QuotationItem.objects.create(quotation=quotation, product=self.product, quantity=3, unit_price=Decimal('10.00'))
        self.client.force_authenticate(user=self.sales_user)
        converted = self.client.post(reverse('quotation-convert', args=[quotation.pk]))
        self.assertEqual(converted.status_code, status.HTTP_201_CREATED, converted.data)
        self.assertEqual(Sale.objects.count(), 0)
        invoice = Invoice.objects.get()
        self.assertEqual(self.approve(invoice).status_code, status.HTTP_200_OK)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock().quantity, 7)


class ConcurrentApprovalTests(InvoiceFixtureMixin, TransactionTestCase):
    """Real concurrent transactions: row locks must prevent double approval and overselling."""

    def setUp(self):
        self.build_fixture()

    def _run_parallel(self, targets):
        results, barrier = [None] * len(targets), threading.Barrier(len(targets))

        def worker(index, invoice_id):
            try:
                barrier.wait(timeout=10)
                results[index] = invoicing.approve_invoice(invoice_id, self.manager)
            except Exception as exc:  # noqa: BLE001 - recorded for assertions
                results[index] = exc
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i, t)) for i, t in enumerate(targets)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return results

    def test_simultaneous_approvals_of_one_invoice_create_one_sale(self):
        invoice = self.make_invoice(qty=4)
        results = self._run_parallel([invoice.pk] * 4)
        self.assertTrue(all(isinstance(r, tuple) for r in results), results)
        self.assertEqual(sum(1 for r in results if r[2]), 1)  # exactly one "created"
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock().quantity, 6)
        self.assertEqual(StockTransaction.objects.filter(reference=invoice.reference).count(), 1)

    def test_concurrent_approvals_cannot_oversell_stock(self):
        # 10 in stock, three invoices of 4 each: only two can be approved.
        invoices = [self.make_invoice(qty=4) for _ in range(3)]
        results = self._run_parallel([i.pk for i in invoices])
        approved = [r for r in results if isinstance(r, tuple)]
        blocked = [r for r in results if isinstance(r, invoicing.InvoiceWorkflowError)]
        self.assertEqual(len(approved), 2, results)
        self.assertEqual(len(blocked), 1, results)
        self.assertEqual(blocked[0].code, 'insufficient_stock')
        self.assertEqual(Sale.objects.count(), 2)
        self.assertEqual(self.stock().quantity, 2)
        self.assertGreaterEqual(self.stock().quantity, 0)
