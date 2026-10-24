from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from Accounts.models import User
from Core.models import AuditLog, Branch
from Inventory.models import Category, Location, Product, Stock
from Sales.models import Customer, Invoice, Quotation, QuotationItem, Sale


class QuotationConversionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin', email='admin@example.com', password='secret123',
            role=User.ADMIN, is_superuser=True,
        )
        self.branch = Branch.objects.create(
            name='Downtown', code='DTN', created_by=self.admin, updated_by=self.admin,
        )
        self.other_branch = Branch.objects.create(
            name='Uptown', code='UPT', created_by=self.admin, updated_by=self.admin,
        )
        self.users = {}
        for role in (User.SALES, User.MANAGER, User.AUDITOR, User.INVENTORY):
            user = User.objects.create_user(
                username=role.lower(), email=f'{role.lower()}@example.com',
                password='secret123', role=role,
            )
            user.branch = self.branch
            user.save(update_fields=['branch'])
            self.users[role] = user
        self.sales_user = self.users[User.SALES]

        self.category = Category.objects.create(
            name='Snacks', description='x', created_by=self.admin, updated_by=self.admin,
        )
        self.location = Location.objects.create(
            name='Shelf A', created_by=self.admin, updated_by=self.admin,
        )
        self.product_a = self._product('Potato Chips', '10.00', stock=50)
        self.product_b = self._product('Cola', '4.00', stock=20)
        self.customer = Customer.objects.create(
            name='Acme Ltd', email='acme@example.com', phone='123', address='1 Road',
            created_by=self.admin, updated_by=self.admin,
        )
        self.quotation = self._quotation(status=Quotation.ACCEPTED)
        self.client = APIClient()

    # helpers -----------------------------------------------------------
    def _product(self, name, price, stock):
        product = Product.objects.create(
            name=name, category=self.category, cost_price=Decimal('1.00'),
            selling_price=Decimal(price), created_by=self.admin, updated_by=self.admin,
        )
        Stock.objects.create(product=product, location=self.location, quantity=stock)
        return product

    def _quotation(self, status=Quotation.ACCEPTED, valid_days=10, reference=None):
        quotation = Quotation.objects.create(
            customer=self.customer, branch=self.branch,
            reference=reference or f'QT-{Quotation.objects.count() + 1}',
            status=status, valid_until=timezone.now().date() + timedelta(days=valid_days),
            notes='Delivery within 3 days', created_by=self.admin, updated_by=self.admin,
        )
        QuotationItem.objects.create(
            quotation=quotation, product=self.product_a, quantity=5,
            unit_price=Decimal('10.00'), discount_percentage=Decimal('10.00'),
        )
        QuotationItem.objects.create(
            quotation=quotation, product=self.product_b, quantity=3,
            unit_price=Decimal('4.00'), discount_amount=Decimal('0.50'),
        )
        return quotation

    def _convert(self, user=None, quotation=None):
        quotation = quotation or self.quotation
        self.client.force_authenticate(user=user or self.sales_user)
        return self.client.post(reverse('quotation-convert', args=[quotation.pk]))

    def _stock(self, product):
        return Stock.objects.get(product=product, location=self.location)

    # 1-3: creation, data transfer, quotation preserved ------------------
    def test_converts_accepted_quotation_into_single_pending_invoice(self):
        response = self._convert()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.data['created'])
        self.assertEqual(Invoice.objects.count(), 1)
        # An invoice is not a sale.
        self.assertEqual(Sale.objects.count(), 0)

        invoice = Invoice.objects.get()
        self.assertEqual(invoice.status, Invoice.PENDING_APPROVAL)
        self.quotation.refresh_from_db()
        self.assertEqual(self.quotation.invoice_id, invoice.pk)
        self.assertTrue(self.quotation.is_converted)
        self.assertEqual(self.quotation.status, Quotation.CONVERTED)
        self.assertEqual(self.quotation.converted_by_id, self.sales_user.pk)
        self.assertIsNotNone(self.quotation.converted_at)
        self.assertEqual(invoice.source_quotation.pk, self.quotation.pk)
        self.assertEqual(response.data['invoice']['quotation_reference'], self.quotation.reference)
        self.assertEqual(response.data['quotation']['invoice_reference'], invoice.reference)

    def test_customer_items_discounts_and_notes_are_carried_over(self):
        self._convert()
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.customer_id, self.customer.pk)
        self.assertEqual(invoice.notes, 'Delivery within 3 days')
        self.assertEqual(invoice.branch_id, self.branch.pk)
        self.assertEqual(invoice.created_by_id, self.sales_user.pk)

        lines = {i.product_id: i for i in invoice.items.all()}
        self.assertEqual(set(lines), {self.product_a.pk, self.product_b.pk})
        a, b = lines[self.product_a.pk], lines[self.product_b.pk]
        self.assertEqual((a.quantity, a.unit_price, a.discount_percentage), (5, Decimal('10.00'), Decimal('10.00')))
        self.assertEqual((b.quantity, b.unit_price, b.discount_amount), (3, Decimal('4.00'), Decimal('0.50')))
        # 5 * 9.00 + 3 * 3.50 = 55.50 for both documents
        self.assertEqual(invoice.total_amount, Decimal('55.50'))
        self.assertEqual(invoice.total_amount, self.quotation.total_amount)

    def test_original_quotation_lines_are_not_modified(self):
        before = list(self.quotation.items.values_list('product_id', 'quantity', 'unit_price', 'discount_percentage', 'discount_amount'))
        self._convert()
        after = list(self.quotation.items.values_list('product_id', 'quantity', 'unit_price', 'discount_percentage', 'discount_amount'))
        self.assertEqual(before, after)

    # 4: unique reference ------------------------------------------------
    def test_invoice_gets_unique_reference(self):
        self._convert()
        other = self._quotation(status=Quotation.ACCEPTED)
        self._convert(quotation=other)
        references = list(Invoice.objects.values_list('reference', flat=True))
        self.assertEqual(len(references), 2)
        self.assertEqual(len(set(references)), 2)
        self.assertTrue(all(ref.startswith('INV-') for ref in references))

    # 5-6: no sale, no reservation, no deduction until approval -----------
    def test_conversion_neither_reserves_nor_deducts_stock(self):
        self._convert()
        for product, total in ((self.product_a, 50), (self.product_b, 20)):
            stock = self._stock(product)
            self.assertEqual(stock.quantity, total)
            self.assertEqual(stock.reserved_quantity, 0)

    def test_conversion_does_not_check_stock_it_is_checked_on_approval(self):
        stock = self._stock(self.product_b)
        stock.quantity = 2
        stock.save(update_fields=['quantity'])
        response = self._convert()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.client.force_authenticate(user=self.users[User.MANAGER])
        approval = self.client.post(reverse('invoice-approve', args=[response.data['invoice']['id']]))
        self.assertEqual(approval.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(approval.data['code'], 'insufficient_stock')

    def test_approving_the_converted_invoice_creates_one_sale_and_deducts_once(self):
        response = self._convert()
        self.client.force_authenticate(user=self.users[User.MANAGER])
        url = reverse('invoice-approve', args=[response.data['invoice']['id']])
        self.assertEqual(self.client.post(url).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.post(url).status_code, status.HTTP_200_OK)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self._stock(self.product_a).quantity, 45)
        self.assertEqual(self._stock(self.product_b).quantity, 17)

    # 7: duplicates ------------------------------------------------------
    def test_second_conversion_returns_existing_invoice(self):
        first = self._convert()
        second = self._convert()
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertFalse(second.data['created'])
        self.assertEqual(second.data['invoice']['id'], first.data['invoice']['id'])
        self.assertEqual(Invoice.objects.count(), 1)

    def test_database_prevents_two_quotations_sharing_an_invoice(self):
        self._convert()
        invoice = Invoice.objects.get()
        other = self._quotation(status=Quotation.ACCEPTED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Quotation.objects.filter(pk=other.pk).update(invoice=invoice, is_converted=True)

    def test_database_rejects_converted_flag_without_invoice(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Quotation.objects.filter(pk=self.quotation.pk).update(is_converted=True)

    # 8: eligibility -----------------------------------------------------
    def test_ineligible_quotations_are_blocked_with_reason(self):
        cases = {
            Quotation.REJECTED: 'quotation_rejected',
            Quotation.EXPIRED: 'quotation_expired',
            Quotation.PENDING: 'quotation_not_accepted',
        }
        for quotation_status, code in cases.items():
            quotation = self._quotation(status=quotation_status)
            response = self._convert(quotation=quotation)
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, quotation_status)
            self.assertEqual(response.data['code'], code)
            self.assertTrue(response.data['detail'])
        self.assertEqual(Invoice.objects.count(), 0)

    def test_accepted_but_past_validity_is_blocked(self):
        quotation = self._quotation(status=Quotation.ACCEPTED)
        Quotation.objects.filter(pk=quotation.pk).update(valid_until=timezone.now().date() - timedelta(days=1))
        response = self._convert(quotation=quotation)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'quotation_expired')

    def test_inactive_customer_or_product_is_blocked(self):
        Customer.objects.filter(pk=self.customer.pk).update(is_active=False)
        response = self._convert()
        self.assertEqual(response.data['code'], 'customer_inactive')
        Customer.objects.filter(pk=self.customer.pk).update(is_active=True)

        Product.objects.filter(pk=self.product_a.pk).update(is_active=False)
        response = self._convert()
        self.assertEqual(response.data['code'], 'product_inactive')
        self.assertEqual(Invoice.objects.count(), 0)

    def test_serializer_exposes_conversion_state(self):
        self.client.force_authenticate(user=self.sales_user)
        data = self.client.get(reverse('quotation-detail', args=[self.quotation.pk])).data
        self.assertTrue(data['can_convert'])
        self.assertIsNone(data['conversion_blocked_reason'])

        self._convert()
        data = self.client.get(reverse('quotation-detail', args=[self.quotation.pk])).data
        self.assertFalse(data['can_convert'])
        self.assertTrue(data['is_converted'])
        self.assertTrue(data['invoice_reference'])

    # 9: converted quotation is locked -----------------------------------
    def test_converted_quotation_cannot_be_edited_deleted_or_re_decided(self):
        self._convert()
        self.client.force_authenticate(user=self.admin)
        url = reverse('quotation-detail', args=[self.quotation.pk])
        self.assertEqual(self.client.patch(url, {'notes': 'x'}, format='json').status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            self.client.post(reverse('quotation-accept', args=[self.quotation.pk])).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(reverse('quotation-reject', args=[self.quotation.pk])).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        item = self.quotation.items.first()
        item_url = reverse('quotation-item-detail', args=[item.pk])
        self.assertEqual(self.client.patch(item_url, {'quantity': 99}, format='json').status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.delete(item_url).status_code, status.HTTP_400_BAD_REQUEST)

    def test_linked_invoice_cannot_be_deleted(self):
        response = self._convert()
        self.client.force_authenticate(user=self.admin)
        # Invoices expose no delete endpoint at all.
        delete = self.client.delete(reverse('invoice-detail', args=[response.data['invoice']['id']]))
        self.assertEqual(delete.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(Invoice.objects.count(), 1)

    # 10: permissions ----------------------------------------------------
    def test_roles_allowed_to_convert(self):
        for user in (self.users[User.MANAGER], self.admin):
            quotation = self._quotation(status=Quotation.ACCEPTED)
            response = self._convert(user=user, quotation=quotation)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, user.role)
        response = self._convert(user=self.sales_user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_read_only_roles_cannot_convert(self):
        for role in (User.AUDITOR, User.INVENTORY):
            response = self._convert(user=self.users[role])
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, role)
        self.assertEqual(Invoice.objects.count(), 0)
        self.quotation.refresh_from_db()
        self.assertFalse(self.quotation.is_converted)

    def test_anonymous_cannot_convert(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(reverse('quotation-convert', args=[self.quotation.pk]))
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertEqual(Invoice.objects.count(), 0)

    def test_user_cannot_convert_quotation_from_another_branch(self):
        Quotation.objects.filter(pk=self.quotation.pk).update(branch=self.other_branch)
        response = self._convert()
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Invoice.objects.count(), 0)

    # 11: audit ----------------------------------------------------------
    def test_conversion_is_audited(self):
        self._convert()
        invoice = Invoice.objects.get()
        self.assertTrue(
            AuditLog.objects.filter(model_label='Sales.Invoice', object_pk=str(invoice.pk), action=AuditLog.CREATE).exists()
        )
        log = AuditLog.objects.filter(
            model_label='Sales.Quotation', object_pk=str(self.quotation.pk), action=AuditLog.UPDATE,
        ).latest('id')
        self.assertEqual(log.changes['invoice']['to'], invoice.pk)
        self.assertEqual(log.changes['status']['to'], Quotation.CONVERTED)
        self.assertEqual(log.changes['converted_by']['to'], self.sales_user.pk)
