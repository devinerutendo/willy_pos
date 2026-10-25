"""
Detailed daily sales report.

One business day of sales as receipt-style rows, each with every product line.
All figures come from the same helpers as the sales report (``Sales.reporting``),
so a day's totals here always equal the sales report for that single day:

* Only COMPLETED sales count towards money figures; pending and cancelled sales
  are listed but never counted.
* Gross / discount / net per sale and per line use ``reporting._sale_figures``
  (gross = quantity x unit price as sold, net = recorded line total).
* Refunds are approved/completed customer returns dated on the day.
* The day is a calendar date in the server's configured time zone
  (``settings.TIME_ZONE``), the same zone the sales report uses.

The system records one payment method per sale and no tax. It does not record a
paid amount for partially paid sales, so those are reported as "partially paid"
with no invented amount, and tax is never returned.
"""

from decimal import Decimal

from django.db.models import prefetch_related_objects
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Sale
from .reporting import (
    REFUNDABLE_RETURN_STATUSES,
    ZERO,
    _money,
    _parse_date,
    _period_data,
    _refund_value,
    _summary,
    cashier_options,
    filtered_querysets,
)

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def _int_param(params, name, default, minimum, maximum=None):
    raw = params.get(name)
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: 'Must be a whole number.'})
    value = max(value, minimum)
    return min(value, maximum) if maximum is not None else value


def _item_payload(item):
    quantity = Decimal(item.quantity)
    unit_price = Decimal(item.unit_price)
    gross = quantity * unit_price
    line_total = item.line_total
    product = item.product
    return {
        'id': item.id,
        'product_id': item.product_id,
        'product_name': product.name,
        'sku': product.sku or '',
        'quantity': item.quantity,
        'unit_price': _money(unit_price),
        'list_price': _money(item.list_unit_price),
        'discount_percentage': _money(item.discount_percentage),
        'discount_amount': _money(item.discount_amount),
        'bundle_quantity': item.bundle_quantity,
        'bundle_price': _money(item.bundle_price) if item.bundle_price is not None else None,
        'gross': _money(gross),
        'discount': _money(max(gross - line_total, ZERO)),
        'line_total': _money(line_total),
    }


def _payment_fields(sale, net):
    """Payment position of a sale from what is actually recorded on it."""
    if sale.status != Sale.COMPLETED:
        return {'amount_paid': None, 'outstanding': None}
    if sale.payment_status == Sale.PAID:
        return {'amount_paid': _money(net), 'outstanding': _money(ZERO)}
    if sale.payment_status == Sale.UNPAID:
        return {'amount_paid': _money(ZERO), 'outstanding': _money(net)}
    # Partially paid: the paid amount is not stored, so it is not guessed.
    return {'amount_paid': None, 'outstanding': None}


def _sale_payload(sale, figures):
    local = timezone.localtime(sale.created_at)
    cashier = sale.sold_by
    refunds = [
        {
            'return_number': ret.return_number,
            'status': ret.status,
            'amount': _money(_refund_value(ret)),
            'method': ret.refund_method,
            'date': timezone.localtime(ret.approved_at or ret.created_at).date().isoformat(),
        }
        for ret in sale.returns.all()
        if ret.return_type == 'SALES' and ret.status in REFUNDABLE_RETURN_STATUSES
    ]
    cash_received = (
        sale.amount_received
        if sale.payment_method == 'CASH' and sale.status == Sale.COMPLETED
        else None
    )
    return {
        'id': sale.id,
        'reference': sale.reference,
        'created_at': local.isoformat(),
        'time': local.strftime('%H:%M'),
        'customer_name': sale.customer.name if sale.customer_id else 'Walk-in customer',
        'cashier_id': sale.sold_by_id,
        'cashier_name': (cashier.full_name or cashier.username) if cashier else '',
        'payment_method': sale.payment_method or '',
        'payment_method_label': sale.get_payment_method_display() if sale.payment_method else '',
        'status': sale.status,
        'payment_status': sale.payment_status,
        'units': figures['units'],
        'subtotal': _money(figures['gross']),
        'discount': _money(figures['discount']),
        'total': _money(figures['net']),
        'cash_received': _money(cash_received) if cash_received is not None else None,
        'change': _money(sale.change_amount) if cash_received is not None else None,
        **_payment_fields(sale, figures['net']),
        'refunds': refunds,
        'notes': sale.notes or '',
        'items': [_item_payload(item) for item in sale.items.all()],
    }


def _payment_breakdown(rows):
    totals = {}
    for sale, figures in rows:
        if sale.status != Sale.COMPLETED:
            continue
        key = sale.payment_method or ''
        entry = totals.setdefault(
            key,
            {
                'method': key,
                'label': sale.get_payment_method_display() if key else 'Not recorded',
                'count': 0,
                'amount': ZERO,
            },
        )
        entry['count'] += 1
        entry['amount'] += figures['net']
    return [
        {**entry, 'amount': _money(entry['amount'])}
        for entry in sorted(totals.values(), key=lambda e: e['label'].lower())
    ]


def _collections(rows):
    """Collected / outstanding totals for completed sales, from recorded payment status."""
    collected = outstanding = partial_total = ZERO
    partial_count = 0
    for sale, figures in rows:
        if sale.status != Sale.COMPLETED:
            continue
        if sale.payment_status == Sale.PAID:
            collected += figures['net']
        elif sale.payment_status == Sale.UNPAID:
            outstanding += figures['net']
        else:
            partial_count += 1
            partial_total += figures['net']
    return {
        'collected': _money(collected),
        'outstanding': _money(outstanding),
        'partially_paid_count': partial_count,
        'partially_paid_total': _money(partial_total),
    }


def build_daily_sales_report(user, params):
    """One page of the day's receipts plus the day's summary.

    Query params: date (YYYY-MM-DD, default today), status, payment_method,
    sold_by, branch, search (receipt, customer, product name/SKU/barcode),
    order (``desc`` newest first, default; ``asc`` oldest first), limit
    (1-100, default 25) and offset.
    """
    day = _parse_date(params.get('date'), 'date') or timezone.localdate()
    limit = _int_param(params, 'limit', DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _int_param(params, 'offset', 0, 0)
    ascending = (params.get('order') or '').lower() == 'asc'

    all_sales, sales_base, returns_base = filtered_querysets(
        user, params, search_products=True,
    )
    data = _period_data(sales_base, returns_base, day, day)
    rows = list(reversed(data['sales'])) if ascending else data['sales']

    page = rows[offset:offset + limit]
    prefetch_related_objects([sale for sale, _ in page], 'items__product', 'returns')

    return {
        'date': day.isoformat(),
        'timezone': timezone.get_current_timezone_name(),
        'generated_at': timezone.localtime().isoformat(),
        'summary': {
            **_summary(data),
            **_collections(data['sales']),
        },
        'payment_breakdown': _payment_breakdown(data['sales']),
        'count': len(rows),
        'limit': limit,
        'offset': offset,
        'has_more': offset + limit < len(rows),
        'results': [_sale_payload(sale, figures) for sale, figures in page],
        'cashiers': cashier_options(all_sales),
        'payment_methods': [
            {'value': value, 'label': label} for value, label in Sale.PAYMENT_METHODS
        ],
    }
