"""
Sales reporting.

Definitions used by the sales report (and echoed to the UI so users can see them):

* Only COMPLETED sales contribute to money figures. Pending and cancelled sales
  are listed in the transaction table but are never counted as sales.
* Gross sales    = quantity x list unit price, before any discount.
* Discounts      = line discounts and bundle pricing (gross minus line totals).
* Refunds        = approved/completed customer sales returns dated inside the
                   period (cash basis: a refund reduces the period it is made in).
* Net sales      = gross sales - discounts - refunds.
* Average sale   = (gross sales - discounts) / completed transactions, i.e. before refunds.
"""

from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from Core.access import branch_scoped_queryset
from .models import Return, Sale

ZERO = Decimal('0.00')
MAX_ROWS = 500
MAX_DAILY_SPAN_DAYS = 62
REFUNDABLE_RETURN_STATUSES = ('APPROVED', 'COMPLETED')


def _money(value):
    return str(Decimal(value).quantize(Decimal('0.01')))


def _parse_date(value, field):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError({field: 'Use the YYYY-MM-DD format.'})


def _apply_search(queryset, term, prefix=''):
    q = (
        Q(**{f'{prefix}reference__icontains': term})
        | Q(**{f'{prefix}customer__name__icontains': term})
    )
    if term.isdigit():
        q |= Q(**{f'{prefix}pk': int(term)})
    return queryset.filter(q)


def _sale_figures(sale):
    gross = ZERO
    net = ZERO
    units = 0
    for item in sale.items.all():
        gross += Decimal(item.quantity) * Decimal(item.unit_price)
        net += item.line_total
        units += item.quantity
    discount = max(gross - net, ZERO)
    return {'gross': net + discount, 'discount': discount, 'net': net, 'units': units}


def _bucket_key(moment, granularity):
    local = timezone.localtime(moment)
    if granularity == 'hour':
        return local.strftime('%Y-%m-%dT%H')
    if granularity == 'day':
        return local.strftime('%Y-%m-%d')
    return local.strftime('%Y-%m')


def _bucket_template(start, end, granularity):
    buckets = OrderedDict()
    if granularity == 'hour':
        for hour in range(24):
            buckets[f'{start.isoformat()}T{hour:02d}'] = f'{hour:02d}:00'
    elif granularity == 'day':
        cursor = start
        while cursor <= end:
            buckets[cursor.isoformat()] = cursor.isoformat()
            cursor += timedelta(days=1)
    else:
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            key = f'{year:04d}-{month:02d}'
            buckets[key] = key
            month += 1
            if month > 12:
                year, month = year + 1, 1
    return buckets


def _pick_granularity(start, end):
    span = (end - start).days + 1
    if span <= 1:
        return 'hour'
    if span <= MAX_DAILY_SPAN_DAYS:
        return 'day'
    return 'month'


def _refund_value(ret):
    return Decimal(ret.refund_amount if ret.refund_amount is not None else ret.total_amount)


def _period_data(sales_base, returns_base, start, end):
    sales_qs = sales_base
    returns_qs = returns_base
    if start:
        sales_qs = sales_qs.filter(created_at__date__gte=start)
        returns_qs = returns_qs.filter(
            Q(approved_at__date__gte=start)
            | Q(approved_at__isnull=True, created_at__date__gte=start)
        )
    if end:
        sales_qs = sales_qs.filter(created_at__date__lte=end)
        returns_qs = returns_qs.filter(
            Q(approved_at__date__lte=end)
            | Q(approved_at__isnull=True, created_at__date__lte=end)
        )

    sales = list(sales_qs.order_by('-created_at'))
    returns = list(returns_qs) if returns_qs is not None else []

    totals = {
        'gross': ZERO, 'discount': ZERO, 'units': 0, 'transactions': 0,
        'refunds': ZERO,
    }
    status_counts = {'COMPLETED': 0, 'PENDING': 0, 'CANCELLED': 0}
    rows = []
    for sale in sales:
        figures = _sale_figures(sale)
        status_counts[sale.status] = status_counts.get(sale.status, 0) + 1
        if sale.status == Sale.COMPLETED:
            totals['gross'] += figures['gross']
            totals['discount'] += figures['discount']
            totals['units'] += figures['units']
            totals['transactions'] += 1
        rows.append((sale, figures))
    for ret in returns:
        totals['refunds'] += _refund_value(ret)

    return {
        'totals': totals,
        'status_counts': status_counts,
        'sales': rows,
        'returns': returns,
    }


def _summary(data):
    totals = data['totals']
    after_discounts = totals['gross'] - totals['discount']
    net = after_discounts - totals['refunds']
    count = totals['transactions']
    average = after_discounts / count if count else ZERO
    return {
        'gross_sales': _money(totals['gross']),
        'discounts': _money(totals['discount']),
        'refunds': _money(totals['refunds']),
        'net_sales': _money(net),
        'transactions': count,
        'units_sold': totals['units'],
        'average_sale': _money(average),
        'status_counts': data['status_counts'],
    }


def _trend(data, start, end, granularity):
    buckets = _bucket_template(start, end, granularity)
    series = OrderedDict(
        (key, {'key': key, 'label': label, 'gross': ZERO, 'net': ZERO, 'transactions': 0})
        for key, label in buckets.items()
    )
    for sale, figures in data['sales']:
        if sale.status != Sale.COMPLETED:
            continue
        entry = series.get(_bucket_key(sale.created_at, granularity))
        if entry:
            entry['gross'] += figures['gross']
            entry['net'] += figures['net']
            entry['transactions'] += 1
    for ret in data['returns']:
        entry = series.get(_bucket_key(ret.approved_at or ret.created_at, granularity))
        if entry:
            entry['net'] -= _refund_value(ret)
    return [
        {
            'key': e['key'], 'label': e['label'], 'gross': _money(e['gross']),
            'net': _money(e['net']), 'transactions': e['transactions'],
        }
        for e in series.values()
    ]


def build_sales_report(user, params):
    """Return summary, trend, previous-period comparison and transaction rows."""
    start = _parse_date(params.get('start_date'), 'start_date')
    end = _parse_date(params.get('end_date'), 'end_date')
    if start and end and start > end:
        raise ValidationError({'end_date': 'End date must not be before the start date.'})

    status_filter = (params.get('status') or '').upper()
    payment_method = params.get('payment_method') or ''
    sold_by = params.get('sold_by') or ''
    branch = params.get('branch') or ''
    search = (params.get('search') or '').strip()

    all_sales = branch_scoped_queryset(Sale.objects.all(), user)
    if branch:
        all_sales = all_sales.filter(branch_id=branch)

    sales_base = all_sales.select_related('customer', 'sold_by').prefetch_related('items')
    if status_filter:
        sales_base = sales_base.filter(status=status_filter)
    if payment_method:
        sales_base = sales_base.filter(payment_method=payment_method)
    if sold_by:
        sales_base = sales_base.filter(sold_by_id=sold_by)
    if search:
        sales_base = _apply_search(sales_base, search)

    # Refunds only make sense when completed sales are part of the view.
    returns_base = Return.objects.none()
    if not status_filter or status_filter == Sale.COMPLETED:
        returns_base = branch_scoped_queryset(
            Return.objects.filter(
                return_type='SALES', status__in=REFUNDABLE_RETURN_STATUSES,
            ).select_related('sale'),
            user,
        )
        if branch:
            returns_base = returns_base.filter(branch_id=branch)
        if payment_method:
            returns_base = returns_base.filter(sale__payment_method=payment_method)
        if sold_by:
            returns_base = returns_base.filter(sale__sold_by_id=sold_by)
        if search:
            returns_base = _apply_search(returns_base, search, prefix='sale__')

    # Resolve open-ended ranges ("All time") from the data itself.
    today = timezone.localdate()
    if not start:
        first = all_sales.order_by('created_at').values_list('created_at', flat=True).first()
        start = timezone.localtime(first).date() if first else (end or today)
    if not end:
        end = today if today >= start else start

    current = _period_data(sales_base, returns_base, start, end)
    granularity = _pick_granularity(start, end)

    previous = None
    if params.get('start_date') or params.get('end_date'):
        span = (end - start).days + 1
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span - 1)
        previous_data = _period_data(sales_base, returns_base, prev_start, prev_end)
        previous = {
            'start': prev_start.isoformat(),
            'end': prev_end.isoformat(),
            **_summary(previous_data),
        }

    rows = []
    for sale, figures in current['sales'][:MAX_ROWS]:
        local = timezone.localtime(sale.created_at)
        cashier = sale.sold_by
        rows.append({
            'id': sale.id,
            'reference': sale.reference,
            'created_at': local.isoformat(),
            'customer_name': sale.customer.name if sale.customer_id else 'Walk-in customer',
            'cashier_id': sale.sold_by_id,
            'cashier_name': (cashier.full_name or cashier.username) if cashier else '',
            'payment_method': sale.payment_method or '',
            'status': sale.status,
            'units': figures['units'],
            'gross': _money(figures['gross']),
            'discount': _money(figures['discount']),
            'total': _money(figures['net']),
        })

    cashiers = {}
    for row in all_sales.values('sold_by', 'sold_by__first_name', 'sold_by__last_name', 'sold_by__username').distinct():
        name = f"{row['sold_by__first_name']} {row['sold_by__last_name']}".strip() or row['sold_by__username']
        cashiers[row['sold_by']] = name

    return {
        'period': {
            'start': start.isoformat(),
            'end': end.isoformat(),
            'granularity': granularity,
        },
        'summary': _summary(current),
        'previous': previous,
        'trend': _trend(current, start, end, granularity),
        'rows': rows,
        'row_count': len(current['sales']),
        'rows_truncated': len(current['sales']) > MAX_ROWS,
        'cashiers': [{'id': pk, 'name': name} for pk, name in sorted(cashiers.items(), key=lambda i: i[1].lower())],
        'payment_methods': [{'value': value, 'label': label} for value, label in Sale.PAYMENT_METHODS],
        'definitions': {
            'scope': 'Only completed sales are counted. Pending and cancelled sales are listed but excluded from totals.',
            'gross_sales': 'Quantity x list price before discounts.',
            'net_sales': 'Gross sales minus discounts and approved customer refunds dated in the period.',
        },
    }
