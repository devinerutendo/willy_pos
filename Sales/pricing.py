"""
Selling-price rules for point-of-sale sale lines.

The backend is the authority for what a cashier may charge:

* A line priced at the product's catalogue ``selling_price`` is always allowed.
* Any other unit price is an *override*. Only users who may manage product
  prices (``inventory`` write: Administrators, Managers, superusers) can do it.
* An override can never go below the product's bundle price **per unit**.
  ``Product.bundle_price`` is the total for ``Product.bundle_quantity`` units, so
  the per-unit floor is ``bundle_price / bundle_quantity`` rounded *up* to the
  cent. Rounding up makes the floor exact: ``p >= floor`` holds precisely when
  ``p >= bundle_price / bundle_quantity``. Products without a bundle have no
  floor beyond the existing "not negative" rule.

Violations are reported as one structured payload listing every bad line, so
the app can show them without parsing text.
"""

from decimal import Decimal, ROUND_CEILING

from rest_framework import serializers, status
from rest_framework.exceptions import APIException

from Core.models import AuditLog
from Inventory.models import Product

CENT = Decimal('0.01')

PRICE_INVALID = 'price_invalid'
PRICE_BELOW_MINIMUM = 'price_below_minimum'
PRICE_OVERRIDE_FORBIDDEN = 'price_override_forbidden'

_price_field = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)


def can_override_selling_price(user) -> bool:
    """Whether ``user`` may sell at a price other than the catalogue price."""
    return bool(user and user.is_authenticated and user.has_permission_for('inventory', 'write'))


def bundle_unit_floor(product) -> Decimal | None:
    """Lowest permitted unit price implied by the product's bundle, or None."""
    quantity = product.bundle_quantity
    price = product.bundle_price
    if not quantity or quantity <= 1 or price is None:
        return None
    floor = (Decimal(price) / Decimal(quantity)).quantize(CENT, rounding=ROUND_CEILING)
    return floor if floor > 0 else None


class PricingRuleError(APIException):
    """Rejected sale prices. ``detail`` is ``{code, detail, items: [...]}``."""

    default_code = PRICE_INVALID

    def __init__(self, violations, status_code):
        super().__init__()
        self.status_code = status_code
        message = violations[0]['detail']
        if len(violations) > 1:
            message += f' {len(violations) - 1} more item(s) also need attention.'
        # Keep the payload as plain JSON (DRF would stringify the numbers).
        self.detail = {
            'code': violations[0]['code'],
            'detail': message,
            'items': violations,
        }


def _money(value: Decimal) -> str:
    return f'{Decimal(value).quantize(CENT):,.2f}'


def _violation(index, product, code, message, submitted, floor=None):
    return {
        'index': index,
        'product_id': product.id if product else None,
        'product_name': product.name if product else None,
        'code': code,
        'detail': message,
        'submitted_price': submitted,
        'catalog_price': str(product.selling_price) if product else None,
        'minimum_price': str(floor) if floor is not None else None,
    }


def validate_sale_prices(raw_items, user) -> None:
    """Reject the whole sale if any line breaks the price rules.

    Lines that are not well-formed objects or reference unknown products are
    left to the regular item validation, which already reports them.
    """
    violations = []
    may_override = can_override_selling_price(user)

    for index, raw in enumerate(raw_items or []):
        if not isinstance(raw, dict):
            continue
        try:
            product = Product.objects.filter(pk=int(raw.get('product'))).first()
        except (TypeError, ValueError):
            product = None
        if product is None:
            continue

        submitted = raw.get('unit_price')
        try:
            price = _price_field.run_validation(submitted)
        except serializers.ValidationError as exc:
            reason = ' '.join(str(m) for m in exc.detail).rstrip('.')
            violations.append(_violation(
                index, product, PRICE_INVALID,
                f'Invalid selling price for {product.name}: {reason}.',
                None if submitted is None else str(submitted),
            ))
            continue

        if price == product.selling_price:
            continue  # catalogue price: not an override

        if not may_override:
            violations.append(_violation(
                index, product, PRICE_OVERRIDE_FORBIDDEN,
                f'You do not have permission to change the selling price of {product.name}. '
                f'Its current price is ${_money(product.selling_price)}.',
                str(price),
            ))
            continue

        floor = bundle_unit_floor(product)
        if floor is not None and price < floor:
            violations.append(_violation(
                index, product, PRICE_BELOW_MINIMUM,
                f'Selling price for {product.name} cannot be below the bundle price of '
                f'${_money(floor)} per unit ({product.bundle_quantity} for '
                f'${_money(product.bundle_price)}).',
                str(price), floor,
            ))

    if violations:
        forbidden = any(v['code'] == PRICE_OVERRIDE_FORBIDDEN for v in violations)
        raise PricingRuleError(
            violations,
            status.HTTP_403_FORBIDDEN if forbidden else status.HTTP_400_BAD_REQUEST,
        )


def record_price_override(sale, sale_item, catalog_price, actor) -> None:
    """Audit-trail entry for a line sold at a non-catalogue price."""
    floor = bundle_unit_floor(sale_item.product)
    changes = {
        'unit_price': {'from': str(catalog_price), 'to': str(sale_item.unit_price)},
        'sale_reference': {'from': None, 'to': sale.reference},
        'product': {'from': None, 'to': sale_item.product.name},
        'quantity': {'from': None, 'to': sale_item.quantity},
    }
    if floor is not None:
        changes['minimum_unit_price'] = {'from': None, 'to': str(floor)}
    AuditLog.objects.create(
        action=AuditLog.UPDATE,
        model_label=sale_item._meta.label,
        object_pk=str(sale_item.pk),
        object_repr=f'Price override: {sale_item.product.name} on {sale.reference}',
        changes=changes,
        actor=actor if getattr(actor, 'pk', None) else None,
        branch=sale.branch,
    )
