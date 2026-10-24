"""
Quotation -> invoice conversion.

Converting creates an ``Invoice`` in Pending Approval. It is not a sale: stock
is neither reserved nor deducted and nothing reaches the sales reports until an
authorized user approves the invoice (see ``Sales.invoicing.approve_invoice``).
"""
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status

from .invoicing import create_invoice
from .models import Invoice, Quotation

logger = logging.getLogger(__name__)


class QuotationConversionError(Exception):
    """Raised when a quotation cannot be converted; carries an API-ready payload."""

    def __init__(self, code, detail, status_code=status.HTTP_400_BAD_REQUEST, **extra):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.extra = extra

    def as_payload(self):
        return {'code': self.code, 'detail': self.detail, **self.extra}


def get_conversion_block(quotation):
    """Return ``(code, reason)`` when the quotation cannot be converted, else ``None``.

    Only state-based rules live here so the same answer can be shown in the UI
    (``can_convert``) and enforced by the server.
    """
    if quotation.is_converted or quotation.invoice_id or quotation.legacy_sale_id:
        return (
            'already_converted',
            'This quotation has already been converted to an invoice.',
        )
    if quotation.status == Quotation.REJECTED:
        return ('quotation_rejected', 'A rejected quotation cannot be converted to an invoice.')
    if quotation.status == Quotation.EXPIRED:
        return ('quotation_expired', 'An expired quotation cannot be converted to an invoice.')
    if quotation.status != Quotation.ACCEPTED:
        return (
            'quotation_not_accepted',
            'The quotation must be accepted by the customer before it can be converted to an invoice.',
        )
    if quotation.is_expired:
        return (
            'quotation_expired',
            'This quotation passed its validity date and cannot be converted to an invoice.',
        )
    return None


def convert_quotation_to_invoice(quotation_id, user):
    """Convert a quotation into an invoice that is pending approval.

    Returns ``(invoice, created)``. ``created`` is ``False`` when the quotation
    was already converted, in which case the existing invoice is returned
    unchanged (idempotent repeats and double clicks never create a second one).
    """
    try:
        with transaction.atomic():
            # Row lock serialises concurrent conversions of the same quotation.
            quotation = Quotation.objects.select_for_update().get(pk=quotation_id)

            if quotation.invoice_id:
                return Invoice.objects.get(pk=quotation.invoice_id), False

            block = get_conversion_block(quotation)
            if block:
                code, reason = block
                raise QuotationConversionError(code, reason, status.HTTP_409_CONFLICT)

            customer = quotation.customer
            if not customer.is_active:
                raise QuotationConversionError(
                    'customer_inactive',
                    f'Customer "{customer.name}" is inactive. Reactivate the customer before converting.',
                    status.HTTP_409_CONFLICT,
                )

            items = list(quotation.items.select_related('product').order_by('id'))
            if not items:
                raise QuotationConversionError(
                    'no_items', 'This quotation has no items to invoice.', status.HTTP_409_CONFLICT,
                )

            inactive = sorted({item.product.name for item in items if not item.product.is_active})
            if inactive:
                raise QuotationConversionError(
                    'product_inactive',
                    'These products are no longer active: ' + ', '.join(inactive) + '.',
                    status.HTTP_409_CONFLICT,
                    products=inactive,
                )

            # Quoted discounts carry over unchanged: the admin-only discount rule
            # was already enforced when the quotation was created.
            invoice = create_invoice(
                customer=customer,
                items=[
                    {
                        'product': item.product,
                        'quantity': item.quantity,
                        'unit_price': item.unit_price,
                        'discount_percentage': item.discount_percentage,
                        'discount_amount': item.discount_amount,
                    }
                    for item in items
                ],
                user=user,
                branch=quotation.branch,
                pos_terminal=quotation.pos_terminal,
                notes=quotation.notes,
                quotation=quotation,
                submit=True,
            )

            quotation.invoice = invoice
            quotation.is_converted = True
            quotation.status = Quotation.CONVERTED
            quotation.converted_at = timezone.now()
            quotation.converted_by = user
            quotation.updated_by = user
            quotation.save()  # audited by the existing Core.audit signal handlers

            logger.info(
                'Quotation %s converted to invoice %s by user %s',
                quotation.reference, invoice.reference, user.pk,
            )
            return invoice, True
    except IntegrityError:
        # Lost a race against another conversion that committed first.
        quotation = Quotation.objects.get(pk=quotation_id)
        if quotation.invoice_id:
            return Invoice.objects.get(pk=quotation.invoice_id), False
        raise
