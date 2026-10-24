"""
Invoice approval workflow.

Creating an invoice is not a sale. Only a successful approval creates the
confirmed ``Sale``, deducts stock and makes the transaction reportable.

Because the ``Sale`` row is created at approval time, the existing sales
reports (which bucket by ``Sale.created_at``) automatically attribute the sale
to the day it was approved.
"""
import logging
from collections import OrderedDict

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status

from Inventory.models import Stock, StockTransaction
from .models import Invoice, InvoiceItem, InvoiceStatusHistory, Sale, SaleItem

logger = logging.getLogger(__name__)

APPROVAL_SCOPE = 'invoice_approval'


class InvoiceWorkflowError(Exception):
    """A workflow rule blocked the operation; carries an API-ready payload."""

    def __init__(self, code, detail, status_code=status.HTTP_409_CONFLICT, **extra):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.extra = extra

    def as_payload(self):
        return {'code': self.code, 'detail': self.detail, **self.extra}


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def can_approve_invoices(user):
    """Approval needs the dedicated ``invoice_approval`` write permission.

    Reuses the role matrix (ADMIN has ``*``, MANAGER has the scope, superuser
    bypasses). Salespeople may create invoices but never approve them.
    """
    return bool(
        user
        and getattr(user, 'is_authenticated', False)
        and getattr(user, 'is_active', False)
        and user.has_permission_for(APPROVAL_SCOPE, 'write')
    )


def _require_approver(user, action):
    if not can_approve_invoices(user):
        raise InvoiceWorkflowError(
            'permission_denied',
            f'You do not have permission to {action} invoices.',
            status.HTTP_403_FORBIDDEN,
        )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def record_history(invoice, action, actor, *, from_status='', to_status='', reason='', sale=None, detail=None):
    return InvoiceStatusHistory.objects.create(
        invoice=invoice,
        action=action,
        from_status=from_status,
        to_status=to_status,
        actor=actor if getattr(actor, 'pk', None) else None,
        reason=reason or '',
        sale=sale,
        detail=detail or {},
    )


# ---------------------------------------------------------------------------
# Create / edit / submit
# ---------------------------------------------------------------------------

def _clean_items(items, kept_list_prices=None):
    """Validate item payload dicts and merge nothing: duplicates are an error.

    ``kept_list_prices`` maps product id to the catalogue price already recorded
    on an edited invoice, so editing a line never rewrites its original price.
    """
    from Inventory.models import Product

    kept_list_prices = kept_list_prices or {}
    if not items:
        raise InvoiceWorkflowError('no_items', 'An invoice needs at least one item.', status.HTTP_400_BAD_REQUEST)
    seen = set()
    cleaned = []
    for index, raw in enumerate(items, start=1):
        product = raw['product']
        if not isinstance(product, Product):
            product = Product.objects.get(pk=product)
        if product.pk in seen:
            raise InvoiceWorkflowError(
                'duplicate_product',
                f'{product.name} appears more than once. Combine the quantities into one line.',
                status.HTTP_400_BAD_REQUEST,
            )
        seen.add(product.pk)
        if not product.is_active:
            raise InvoiceWorkflowError(
                'product_inactive', f'{product.name} is no longer active.', status.HTTP_409_CONFLICT,
            )
        cleaned.append({
            'product': product,
            'quantity': raw['quantity'],
            'unit_price': raw['unit_price'],
            'list_price': kept_list_prices.get(product.pk, product.selling_price),
            'discount_percentage': raw.get('discount_percentage') or 0,
            'discount_amount': raw.get('discount_amount') or 0,
        })
    return cleaned


def create_invoice(*, customer, items, user, branch=None, pos_terminal=None, notes='',
                   quotation=None, submit=True):
    """Create an invoice (and its lines) without touching stock or reports."""
    if not customer.is_active:
        raise InvoiceWorkflowError(
            'customer_inactive',
            f'Customer "{customer.name}" is inactive. Reactivate the customer first.',
        )
    cleaned = _clean_items(items)
    target = Invoice.PENDING_APPROVAL if submit else Invoice.DRAFT

    with transaction.atomic():
        invoice = Invoice.objects.create(
            reference=Invoice.generate_reference(),
            status=target,
            customer=customer,
            branch=branch or getattr(user, 'branch', None),
            pos_terminal=pos_terminal,
            notes=notes or '',
            created_by=user,
            updated_by=user,
        )
        InvoiceItem.objects.bulk_create([InvoiceItem(invoice=invoice, **line) for line in cleaned])
        record_history(
            invoice, InvoiceStatusHistory.CREATED, user, to_status=target,
            detail={'quotation': quotation.reference if quotation else None},
        )
    return invoice


def update_invoice(invoice_id, user, *, customer=None, items=None, notes=None):
    """Edit an invoice while it is still Draft or Pending Approval."""
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        if not invoice.is_editable:
            raise InvoiceWorkflowError(
                'invoice_locked',
                f'A {invoice.get_status_display().lower()} invoice can no longer be edited.',
            )
        changed = []
        if customer is not None and customer.pk != invoice.customer_id:
            if not customer.is_active:
                raise InvoiceWorkflowError('customer_inactive', f'Customer "{customer.name}" is inactive.')
            invoice.customer = customer
            changed.append('customer')
        if notes is not None and notes != invoice.notes:
            invoice.notes = notes
            changed.append('notes')
        if items is not None:
            cleaned = _clean_items(items, {
                line.product_id: line.list_price
                for line in invoice.items.all()
                if line.list_price is not None
            })
            invoice.items.all().delete()
            InvoiceItem.objects.bulk_create([InvoiceItem(invoice=invoice, **line) for line in cleaned])
            changed.append('items')
        if changed:
            invoice.updated_by = user
            invoice.save()
            record_history(
                invoice, InvoiceStatusHistory.EDITED, user,
                from_status=invoice.status, to_status=invoice.status,
                detail={'changed': changed},
            )
    return invoice


def _transition(invoice, target):
    if not invoice.can_transition_to(target):
        raise InvoiceWorkflowError(
            'invalid_transition',
            f'An invoice that is {invoice.get_status_display().lower()} cannot be '
            f'moved to {dict(Invoice.STATUS_CHOICES)[target].lower()}.',
            current_status=invoice.status,
        )


def submit_invoice(invoice_id, user):
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        _transition(invoice, Invoice.PENDING_APPROVAL)
        previous = invoice.status
        invoice.status = Invoice.PENDING_APPROVAL
        invoice.updated_by = user
        invoice.save()
        record_history(
            invoice, InvoiceStatusHistory.SUBMITTED, user,
            from_status=previous, to_status=invoice.status,
        )
    return invoice


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------

def _find_shortages(items):
    """Lock stock rows (ordered, to avoid deadlocks) and list any shortfalls."""
    requested = OrderedDict()
    for item in items:
        entry = requested.setdefault(item.product_id, {'product': item.product, 'quantity': 0})
        entry['quantity'] += item.quantity

    stocks = list(
        Stock.objects.select_for_update(of=('self',))
        .select_related('location')
        .filter(product_id__in=list(requested))
        .order_by('product_id', 'id')
    )
    by_product = {product_id: [] for product_id in requested}
    for stock in stocks:
        by_product[stock.product_id].append(stock)

    shortages = []
    for product_id, entry in requested.items():
        available = sum(max(s.available_stock, 0) for s in by_product[product_id])
        if entry['quantity'] > available:
            shortages.append({
                'product_id': product_id,
                'product_name': entry['product'].name,
                'sku': entry['product'].sku,
                'requested': entry['quantity'],
                'available': available,
            })
    return shortages, by_product


def _deduct(invoice, sale, item, stocks, user):
    """Deduct one line from the locked stock rows and log each movement."""
    remaining = item.quantity
    movements = []
    # Prefer locations of the invoice branch, then everything else.
    ordered = sorted(
        stocks,
        key=lambda s: (
            0 if invoice.branch_id and s.location.branch_id == invoice.branch_id else 1,
            s.pk,
        ),
    )
    for stock in ordered:
        if remaining <= 0:
            break
        take = min(remaining, max(stock.available_stock, 0))
        if take <= 0:
            continue
        before = stock.quantity
        stock.quantity -= take
        stock.save()
        StockTransaction.objects.create(
            product=item.product,
            location=stock.location,
            branch=invoice.branch or stock.location.branch,
            pos_terminal=invoice.pos_terminal,
            transaction_type=StockTransaction.OUT,
            quantity=-take,
            reference=invoice.reference,
            notes=(
                f'Invoice {invoice.reference} approved | Sale {sale.reference} (ID {sale.pk}) | '
                f'Approved by {user.pk} on {timezone.now().isoformat()}'
            ),
            created_by=user,
            updated_by=user,
        )
        movements.append({
            'product_id': item.product_id,
            'product_name': item.product.name,
            'location_id': stock.location_id,
            'quantity': take,
            'stock_before': before,
            'stock_after': stock.quantity,
        })
        remaining -= take
    return movements


def _create_sale(invoice, user, now):
    sale = Sale.objects.create(
        reference=Sale.generate_reference(),
        customer=invoice.customer,
        sold_by=invoice.created_by,
        branch=invoice.branch,
        pos_terminal=invoice.pos_terminal,
        status=Sale.COMPLETED,
        payment_status=Sale.UNPAID,
        notes=invoice.notes,
        # Stock is deducted below in the same transaction, so the background
        # inventory task must never run for this sale.
        inventory_completed=True,
        inventory_sync_status='SUCCESS',
        created_by=user,
        updated_by=user,
    )
    lines = []
    for item in invoice.items.all():
        bundle_quantity = bundle_price = None
        product = item.product
        if product.bundle_quantity and product.bundle_price is not None and item.quantity >= product.bundle_quantity:
            bundle_quantity, bundle_price = product.bundle_quantity, product.bundle_price
        lines.append(SaleItem(
            sale=sale, product=product, quantity=item.quantity, unit_price=item.unit_price,
            discount_percentage=item.discount_percentage, discount_amount=item.discount_amount,
            bundle_quantity=bundle_quantity, bundle_price=bundle_price,
            list_price=item.list_price,
        ))
    SaleItem.objects.bulk_create(lines)
    return sale


def approve_invoice(invoice_id, user):
    """Approve an invoice: create one confirmed sale and deduct stock once.

    Returns ``(invoice, sale, created)``. ``created`` is False when the invoice
    was already approved (idempotent repeat / double click): nothing changes.
    Any failure rolls back everything; blocking errors are written to the
    history afterwards so they stay visible.
    """
    try:
        _require_approver(user, 'approve')
        return _approve(invoice_id, user)
    except InvoiceWorkflowError as exc:
        _log_failure(invoice_id, user, exc)
        raise


def _approve(invoice_id, user):
    try:
        with transaction.atomic():
            invoice = (
                Invoice.objects.select_for_update(of=('self',))
                .select_related('customer', 'branch')
                .get(pk=invoice_id)
            )
            if invoice.status == Invoice.APPROVED:
                return invoice, invoice.sale, False
            if invoice.status != Invoice.PENDING_APPROVAL:
                raise InvoiceWorkflowError(
                    'invalid_transition',
                    f'Only invoices pending approval can be approved. '
                    f'This invoice is {invoice.get_status_display().lower()}.',
                    current_status=invoice.status,
                )
            if not invoice.customer.is_active:
                raise InvoiceWorkflowError(
                    'customer_inactive',
                    f'Customer "{invoice.customer.name}" is inactive. Reactivate the customer before approving.',
                )
            items = list(invoice.items.select_related('product').order_by('id'))
            if not items:
                raise InvoiceWorkflowError('no_items', 'This invoice has no items.')
            inactive = sorted({i.product.name for i in items if not i.product.is_active})
            if inactive:
                raise InvoiceWorkflowError(
                    'product_inactive',
                    'These products are no longer active: ' + ', '.join(inactive) + '.',
                    products=inactive,
                )

            shortages, stocks_by_product = _find_shortages(items)
            if shortages:
                summary = ', '.join(
                    f"{s['product_name']} (requested {s['requested']}, available {s['available']})"
                    for s in shortages
                )
                raise InvoiceWorkflowError(
                    'insufficient_stock',
                    f'Insufficient stock to approve this invoice: {summary}.',
                    shortages=shortages,
                )

            now = timezone.now()
            sale = _create_sale(invoice, user, now)
            movements = []
            for item in items:
                movements.extend(_deduct(invoice, sale, item, stocks_by_product[item.product_id], user))

            previous = invoice.status
            invoice.status = Invoice.APPROVED
            invoice.sale = sale
            invoice.approved_by = user
            invoice.approved_at = now
            invoice.updated_by = user
            invoice.save()
            record_history(
                invoice, InvoiceStatusHistory.APPROVED, user,
                from_status=previous, to_status=invoice.status, sale=sale,
                detail={'sale_reference': sale.reference, 'stock_movements': movements},
            )
            logger.info('Invoice %s approved by user %s -> sale %s', invoice.reference, user.pk, sale.reference)
            return invoice, sale, True
    except IntegrityError:
        # Lost a race that the row lock should already prevent; treat as repeat.
        invoice = Invoice.objects.get(pk=invoice_id)
        if invoice.status == Invoice.APPROVED:
            return invoice, invoice.sale, False
        raise


def _log_failure(invoice_id, user, exc):
    """Record a blocked approval outside the rolled-back transaction."""
    try:
        invoice = Invoice.objects.get(pk=invoice_id)
    except Invoice.DoesNotExist:
        return
    try:
        record_history(
            invoice, InvoiceStatusHistory.APPROVAL_FAILED, user,
            from_status=invoice.status, to_status=invoice.status,
            reason=str(exc.detail),
            detail={'code': exc.code, **{k: v for k, v in exc.extra.items()}},
        )
    except Exception:  # never mask the original error
        logger.exception('Could not record failed approval for invoice %s', invoice_id)


# ---------------------------------------------------------------------------
# Reject / cancel
# ---------------------------------------------------------------------------

def reject_invoice(invoice_id, user, reason):
    _require_approver(user, 'reject')
    reason = (reason or '').strip()
    if not reason:
        raise InvoiceWorkflowError(
            'reason_required', 'A reason is required to reject an invoice.', status.HTTP_400_BAD_REQUEST,
        )
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        _transition(invoice, Invoice.REJECTED)
        previous = invoice.status
        invoice.status = Invoice.REJECTED
        invoice.rejected_by = user
        invoice.rejected_at = timezone.now()
        invoice.rejection_reason = reason
        invoice.updated_by = user
        invoice.save()
        record_history(
            invoice, InvoiceStatusHistory.REJECTED, user,
            from_status=previous, to_status=invoice.status, reason=reason,
        )
    return invoice


def cancel_invoice(invoice_id, user, reason=''):
    """Cancel a not-yet-approved invoice. Creators and approvers may cancel."""
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        if not (can_approve_invoices(user) or invoice.created_by_id == user.pk):
            raise InvoiceWorkflowError(
                'permission_denied',
                'Only the invoice creator or an approver can cancel this invoice.',
                status.HTTP_403_FORBIDDEN,
            )
        _transition(invoice, Invoice.CANCELLED)
        previous = invoice.status
        invoice.status = Invoice.CANCELLED
        invoice.cancelled_by = user
        invoice.cancelled_at = timezone.now()
        invoice.cancellation_reason = (reason or '').strip()
        invoice.updated_by = user
        invoice.save()
        record_history(
            invoice, InvoiceStatusHistory.CANCELLED, user,
            from_status=previous, to_status=invoice.status, reason=invoice.cancellation_reason,
        )
    return invoice


# ---------------------------------------------------------------------------
# Quotation -> invoice
# ---------------------------------------------------------------------------

def mark_sale_paid(sale_id, user, amount=None):
    """Record that an approved sale has been paid (distinct from sale status)."""
    with transaction.atomic():
        sale = Sale.objects.select_for_update().get(pk=sale_id)
        if sale.status != Sale.COMPLETED:
            raise InvoiceWorkflowError('sale_not_confirmed', 'Only confirmed sales can be marked as paid.')
        if sale.payment_status == Sale.PAID:
            return sale
        sale.payment_status = Sale.PAID
        sale.updated_by = user
        sale.save(update_fields=['payment_status', 'updated_by', 'updated_at'])
        invoice = Invoice.objects.filter(sale=sale).first()
        if invoice:
            record_history(
                invoice, InvoiceStatusHistory.PAYMENT_RECORDED, user,
                from_status=invoice.status, to_status=invoice.status, sale=sale,
            )
    return sale
