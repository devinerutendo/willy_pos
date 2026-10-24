import uuid
from rest_framework import serializers
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from decimal import Decimal
from Inventory.models import Product, Stock, Location
from .models import (
    Customer, Quotation, QuotationItem, Sale, SaleItem,
    Layby, LaybyItem, LaybyPayment, LaybyRefund, DailyReconciliation,
    Return, ReturnItem
)
from .pricing import validate_sale_prices, record_price_override


class CustomerSerializer(serializers.ModelSerializer):
    total_spent = serializers.SerializerMethodField(read_only=True)
    total_sales = serializers.SerializerMethodField(read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'email', 'phone', 'address', 'city',
            'tax_id', 'credit_limit', 'total_spent', 'total_sales', 'is_active',
            'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = ['created_at', 'updated_at', 'total_spent', 'total_sales', 'created_by', 'updated_by']

    def get_total_spent(self, obj):
        """Calculate total spending by customer"""
        sales = Sale.objects.filter(customer=obj, status='COMPLETED')
        return sum(sale.total_amount for sale in sales)

    def get_total_sales(self, obj):
        """Count total sales"""
        return Sale.objects.filter(customer=obj).count()

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Customer name is required")
        return value.strip()

    def validate_email(self, value):
        if value and '@' not in value:
            raise serializers.ValidationError("Invalid email format")
        return value


class QuotationItemInputSerializer(serializers.Serializer):
    """Serializer for quotation item input (accepts raw IDs and values)."""
    product = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    discount_percentage = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        min_value=0,
        max_value=100,
        required=False
    )
    discount_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        min_value=0,
        required=False
    )

    def validate(self, data):
        """Validate item constraints."""
        # Check product exists
        try:
            Product.objects.get(id=data['product'])
        except Product.DoesNotExist:
            raise serializers.ValidationError({"product": "Product does not exist"})
        
        # Validate discount is only for admins
        request = self.context.get('request')
        if request and not request.user.is_superuser:
            if data.get('discount_percentage', 0) > 0 or data.get('discount_amount', 0) > 0:
                raise serializers.ValidationError("Only admins can apply discounts")
        
        return data


class QuotationItemSerializer(serializers.ModelSerializer):
    quotation = serializers.PrimaryKeyRelatedField(queryset=Quotation.objects.all())
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all(), write_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    product_id = serializers.IntegerField(source='product.id', read_only=True)
    total_price = serializers.SerializerMethodField(read_only=True)
    effective_unit_price = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = QuotationItem
        fields = [
            'id', 'quotation', 'product', 'product_id', 'product_name', 'product_sku',
            'quantity', 'unit_price', 'discount_percentage', 'discount_amount',
            'effective_unit_price', 'total_price'
        ]
        read_only_fields = ['id', 'product_id', 'product_name', 'product_sku', 'total_price', 'effective_unit_price']

    def get_total_price(self, obj):
        """Calculate line total with discounts."""
        return obj.line_total

    def get_effective_unit_price(self, obj):
        """Get unit price after discounts applied."""
        return obj.effective_unit_price

    def validate(self, data):
        if data.get('quantity', 0) <= 0:
            raise serializers.ValidationError("Quantity must be greater than 0")
        if data.get('unit_price', 0) < 0:
            raise serializers.ValidationError("Price cannot be negative")
        
        # Validate discount is only for admins
        request = self.context.get('request')
        if request and not request.user.is_superuser:
            if data.get('discount_percentage', 0) > 0 or data.get('discount_amount', 0) > 0:
                raise serializers.ValidationError("Only admins can apply discounts")
        
        if data.get('discount_percentage', 0) < 0 or data.get('discount_percentage', 0) > 100:
            raise serializers.ValidationError("Discount percentage must be between 0 and 100")
        if data.get('discount_amount', 0) < 0:
            raise serializers.ValidationError("Discount amount cannot be negative")
        
        return data


class QuotationSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    customer_email = serializers.CharField(source='customer.email', read_only=True)
    customer_address = serializers.CharField(source='customer.address', read_only=True)
    customer_city = serializers.CharField(source='customer.city', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    pos_terminal_name = serializers.CharField(source='pos_terminal.name', read_only=True, allow_null=True)
    items = serializers.SerializerMethodField(read_only=True)
    total_amount = serializers.SerializerMethodField(read_only=True)
    is_expired = serializers.SerializerMethodField(read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    created_by_username = serializers.SerializerMethodField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)
    invoice_reference = serializers.CharField(source='invoice.reference', read_only=True, allow_null=True)
    invoice_status = serializers.CharField(source='invoice.status', read_only=True, allow_null=True)
    converted_by_username = serializers.SerializerMethodField(read_only=True)
    can_convert = serializers.SerializerMethodField(read_only=True)
    conversion_blocked_reason = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Quotation
        fields = [
            'id', 'customer', 'customer_name', 'customer_phone', 'customer_email',
            'customer_address', 'customer_city', 'reference', 'status',
            'branch', 'branch_name', 'pos_terminal', 'pos_terminal_name',
            'items', 'total_amount', 'valid_until', 'is_expired',
            'notes', 'created_at', 'updated_at', 'created_by', 'created_by_username', 'updated_by',
            'is_converted', 'invoice', 'invoice_reference', 'invoice_status',
            'converted_at', 'converted_by', 'converted_by_username',
            'can_convert', 'conversion_blocked_reason',
        ]
        read_only_fields = [
            'id', 'reference', 'status', 'created_at', 'updated_at', 'total_amount', 'is_expired',
            'created_by', 'updated_by', 'is_converted', 'invoice', 'invoice_reference',
            'invoice_status', 'converted_at', 'converted_by', 'converted_by_username',
            'can_convert', 'conversion_blocked_reason',
        ]

    def get_converted_by_username(self, obj):
        user = obj.converted_by
        if not user:
            return None
        return f"{user.first_name} {user.last_name}".strip() or user.username or user.email

    def get_can_convert(self, obj):
        from .conversion import get_conversion_block
        return get_conversion_block(obj) is None

    def get_conversion_blocked_reason(self, obj):
        from .conversion import get_conversion_block
        block = get_conversion_block(obj)
        return block[1] if block else None

    def get_created_by_username(self, obj):
        """Get creator's display name with intelligent fallback."""
        user = obj.created_by
        # Try full name first
        full_name = f"{user.first_name} {user.last_name}".strip()
        if full_name:
            return full_name
        # Fall back to username
        if user.username:
            return user.username
        # Final fallback to email
        return user.email

    def get_items(self, obj):
        """Serialize quotation items for output."""
        items = QuotationItem.objects.filter(quotation=obj).select_related('product')
        return QuotationItemSerializer(items, many=True).data

    def validate_customer(self, value):
        """Validate customer exists and is active."""
        if not value.is_active:
            raise serializers.ValidationError("Selected customer is inactive")
        return value

    def validate_valid_until(self, value):
        """Validate valid_until date is today or in the future."""
        if value < timezone.now().date():
            raise serializers.ValidationError("Valid until date cannot be in the past")
        return value

    def validate_items_input(self, items):
        """Validate items input during create - moved from validate_items."""
        if not items:
            raise serializers.ValidationError("At least one item is required")
        return items

    def validate(self, data):
        """Validate overall quotation data."""
        # Check if this is a create operation (items in initial_data)
        if 'items' in self.initial_data:
            items = self.initial_data.get('items', [])
            if not items:
                raise serializers.ValidationError({"items": ["At least one item is required"]})
        
        if not data.get('customer'):
            raise serializers.ValidationError({"customer": ["Customer is required"]})
        if not data.get('valid_until'):
            raise serializers.ValidationError({"valid_until": ["Valid until date is required"]})

        branch = data.get('branch')
        pos_terminal = data.get('pos_terminal')
        if pos_terminal and branch and pos_terminal.branch_id != branch.id:
            raise serializers.ValidationError(
                {"pos_terminal": ["Selected POS terminal does not belong to the selected branch."]}
            )
        return data

    @staticmethod
    def _flatten_errors(errors):
        """Collapse DRF error structures into a single readable sentence."""
        if isinstance(errors, dict):
            return '; '.join(
                f"{field}: {QuotationSerializer._flatten_errors(messages)}"
                for field, messages in errors.items()
            )
        if isinstance(errors, (list, tuple)):
            return ' '.join(QuotationSerializer._flatten_errors(e) for e in errors)
        return str(errors)

    def _validated_items(self):
        """Validate every raw line item and return clean values.

        All checks run before anything is written, so a bad line can never
        leave a half-created quotation behind.
        """
        raw_items = self.initial_data.get('items') or []
        if not isinstance(raw_items, (list, tuple)) or not raw_items:
            raise serializers.ValidationError({"items": ["At least one item is required"]})

        clean_items = []
        seen_products = set()
        for index, raw in enumerate(raw_items, start=1):
            item_serializer = QuotationItemInputSerializer(
                data=raw,
                context={'request': self.context.get('request')},
            )
            if not item_serializer.is_valid():
                raise serializers.ValidationError(
                    {"items": [f"Item {index}: {self._flatten_errors(item_serializer.errors)}"]}
                )
            data = item_serializer.validated_data
            if data['product'] in seen_products:
                raise serializers.ValidationError(
                    {"items": [f"Item {index}: product appears more than once; combine the quantities into one line."]}
                )
            seen_products.add(data['product'])
            clean_items.append(data)
        return clean_items

    def create(self, validated_data):
        """Create the quotation and its line items in a single transaction."""
        items = self._validated_items()

        with transaction.atomic():
            quotation = Quotation.objects.create(**validated_data)
            QuotationItem.objects.bulk_create([
                QuotationItem(
                    quotation=quotation,
                    product_id=item['product'],
                    quantity=item['quantity'],
                    unit_price=item['unit_price'],
                    discount_percentage=item.get('discount_percentage', 0),
                    discount_amount=item.get('discount_amount', 0),
                )
                for item in items
            ])
        return quotation

    def get_total_amount(self, obj):
        items = QuotationItem.objects.filter(quotation=obj)
        return sum(item.line_total for item in items)

    def get_is_expired(self, obj):
        return obj.is_expired


class SaleItemInputSerializer(serializers.Serializer):
    """Input serializer for sale items during creation."""
    product = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    discount_percentage = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        min_value=0,
        max_value=100,
        required=False
    )
    discount_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        min_value=0,
        required=False
    )

    def validate(self, data):
        """Validate product exists and has sufficient stock."""
        try:
            product = Product.objects.get(id=data['product'])
        except Product.DoesNotExist:
            raise serializers.ValidationError({"product": "Product does not exist"})
        
        # Check stock availability using Stock model's available_stock
        # Get all stock locations and sum available quantities
        stock_items = Stock.objects.filter(product=product)
        total_available = sum(stock.available_stock for stock in stock_items)
        
        if data['quantity'] > total_available:
            raise serializers.ValidationError({
                "quantity": f"Only {total_available} units available in stock"
            })
        
        # Validate discount is only for admins
        request = self.context.get('request')
        if request and not request.user.is_superuser:
            if data.get('discount_percentage', 0) > 0 or data.get('discount_amount', 0) > 0:
                raise serializers.ValidationError("Only admins can apply discounts")
        
        return data


class SaleItemSerializer(serializers.ModelSerializer):
    """Output serializer for sale items."""
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    product_id = serializers.IntegerField(source='product.id', read_only=True)
    total_price = serializers.SerializerMethodField(read_only=True)
    bundle_applied = serializers.SerializerMethodField(read_only=True)
    effective_unit_price = serializers.SerializerMethodField(read_only=True)
    list_total_price = serializers.SerializerMethodField(read_only=True)
    price_discount = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = SaleItem
        fields = [
            'id', 'sale', 'product_id', 'product_name', 'product_sku',
            'quantity', 'unit_price', 'discount_percentage', 'discount_amount',
            'effective_unit_price', 'bundle_quantity', 'bundle_price',
            'bundle_applied', 'total_price',
            'list_price', 'list_total_price', 'price_discount',
        ]
        read_only_fields = [
            'id', 'sale', 'product_id', 'product_name', 'product_sku',
            'bundle_quantity', 'bundle_price', 'bundle_applied', 'total_price',
            'effective_unit_price', 'list_price', 'list_total_price', 'price_discount',
        ]

    def get_list_total_price(self, obj):
        return str(obj.list_line_total.quantize(Decimal('0.01')))

    def get_price_discount(self, obj):
        return str(obj.price_discount.quantize(Decimal('0.01')))

    def get_total_price(self, obj):
        """Calculate line total with discounts."""
        return obj.line_total

    def get_effective_unit_price(self, obj):
        """Get unit price after discounts applied."""
        return obj.effective_unit_price

    def get_bundle_applied(self, obj):
        return bool(obj.bundle_quantity and obj.bundle_price and obj.quantity >= obj.bundle_quantity)

    def validate(self, data):
        # Editing a stored line's price must obey the same rules as the cart.
        if self.instance is not None and 'unit_price' in data and data['unit_price'] != self.instance.unit_price:
            request = self.context.get('request')
            validate_sale_prices(
                [{'product': self.instance.product_id, 'unit_price': data['unit_price']}],
                getattr(request, 'user', None),
            )
        if data.get('quantity', 0) <= 0:
            raise serializers.ValidationError("Quantity must be greater than 0")
        if data.get('unit_price', 0) < 0:
            raise serializers.ValidationError("Price cannot be negative")
        
        # Validate discount is only for admins
        request = self.context.get('request')
        if request and not request.user.is_superuser:
            if data.get('discount_percentage', 0) > 0 or data.get('discount_amount', 0) > 0:
                raise serializers.ValidationError("Only admins can apply discounts")
        
        if data.get('discount_percentage', 0) < 0 or data.get('discount_percentage', 0) > 100:
            raise serializers.ValidationError("Discount percentage must be between 0 and 100")
        if data.get('discount_amount', 0) < 0:
            raise serializers.ValidationError("Discount amount cannot be negative")
        
        return data


class SaleSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True, allow_null=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    pos_terminal_name = serializers.CharField(source='pos_terminal.name', read_only=True, allow_null=True)
    items = serializers.SerializerMethodField(read_only=True)
    total_amount = serializers.SerializerMethodField(read_only=True)
    subtotal_amount = serializers.SerializerMethodField(read_only=True)
    discount_total = serializers.SerializerMethodField(read_only=True)
    sold_by_name = serializers.CharField(source='sold_by.full_name', read_only=True, allow_null=True)
    layby_reference = serializers.CharField(source='layby.reference', read_only=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)
    quotation = serializers.SerializerMethodField(read_only=True)
    quotation_reference = serializers.SerializerMethodField(read_only=True)
    invoice = serializers.SerializerMethodField(read_only=True)
    invoice_reference = serializers.SerializerMethodField(read_only=True)
    approved_at = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Sale
        fields = [
            'id', 'customer', 'customer_name', 'reference', 'status', 'payment_status',
            'branch', 'branch_name', 'pos_terminal', 'pos_terminal_name',
            'items', 'total_amount', 'subtotal_amount', 'discount_total', 'notes',
            'payment_method', 'layby', 'layby_reference', 'amount_received', 'change_amount',
            'inventory_sync_status', 'quotation', 'quotation_reference',
            'invoice', 'invoice_reference', 'approved_at',
            'sold_by', 'sold_by_name', 'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = [
            'id', 'reference', 'status', 'payment_status', 'created_at', 'updated_at',
            'total_amount', 'subtotal_amount', 'discount_total',
            'sold_by', 'sold_by_name', 'created_by', 'updated_by',
            'change_amount', 'inventory_sync_status', 'layby_reference',
            'quotation', 'quotation_reference', 'invoice', 'invoice_reference', 'approved_at',
        ]

    @staticmethod
    def _related(obj, name):
        try:
            return getattr(obj, name)
        except ObjectDoesNotExist:
            return None

    def _source_quotation(self, obj):
        legacy = self._related(obj, 'legacy_source_quotation')
        if legacy:
            return legacy
        invoice = self._related(obj, 'source_invoice')
        return self._related(invoice, 'source_quotation') if invoice else None

    def get_invoice(self, obj):
        invoice = self._related(obj, 'source_invoice')
        return invoice.id if invoice else None

    def get_invoice_reference(self, obj):
        invoice = self._related(obj, 'source_invoice')
        return invoice.reference if invoice else None

    def get_approved_at(self, obj):
        invoice = self._related(obj, 'source_invoice')
        return invoice.approved_at if invoice else None

    def get_quotation(self, obj):
        quotation = self._source_quotation(obj)
        return quotation.id if quotation else None

    def get_quotation_reference(self, obj):
        quotation = self._source_quotation(obj)
        return quotation.reference if quotation else None

    def get_items(self, obj):
        """Serialize sale items for output."""
        items = SaleItem.objects.filter(sale=obj).select_related('product')
        return SaleItemSerializer(items, many=True).data

    def validate_customer(self, value):
        """Validate customer is active if provided."""
        if value and not value.is_active:
            raise serializers.ValidationError("Selected customer is inactive")
        return value

    def validate_layby(self, value):
        """Validate layby is for the same customer and is fully paid if payment_method is LAYBY."""
        if value and self.initial_data.get('payment_method') == 'LAYBY':
            # Will be validated in validate() method
            pass
        return value

    def validate(self, data):
        """Validate overall sale data."""
        if data.get('notes') is None:
            data['notes'] = ''

        # Check if items are provided in initial_data
        if 'items' in self.initial_data:
            items = self.initial_data.get('items', [])
            if not items:
                raise serializers.ValidationError({"items": ["At least one item is required"]})

            # Authoritative selling-price rules (permission + bundle floor).
            request = self.context.get('request')
            validate_sale_prices(items, getattr(request, 'user', None))
        
        # Validate layby-payment method relationship
        payment_method = data.get('payment_method')
        layby = data.get('layby')
        
        if payment_method == 'LAYBY':
            if not layby:
                raise serializers.ValidationError(
                    {"layby": ["Layby must be specified when payment method is LAYBY"]}
                )
            if layby.customer != data.get('customer'):
                raise serializers.ValidationError(
                    {"layby": ["Layby customer must match sale customer"]}
                )
            if layby.balance > 0:
                raise serializers.ValidationError(
                    {"layby": [f"Layby must be fully paid (balance: {layby.balance})"]}
                )
        
        return data

    def create(self, validated_data):
        """Handle nested items creation during sale creation."""
        items_data = self.initial_data.get('items', [])
        
        # Validate items exist
        if not items_data:
            raise serializers.ValidationError({"items": ["At least one item is required"]})
        
        # Create sale without items
        sale = Sale.objects.create(**validated_data)
        
        # Create associated items with validation
        price_overrides = []
        for i, item_data in enumerate(items_data):
            try:
                # Validate item data
                item_serializer = SaleItemInputSerializer(
                    data=item_data,
                    context={'request': self.context.get('request')}
                )
                if not item_serializer.is_valid():
                    # Clean up sale if validation fails
                    sale.delete()
                    raise serializers.ValidationError({"items": [f"Item {i+1}: {item_serializer.errors}"]})
                
                validated_item = item_serializer.validated_data
                product = Product.objects.get(id=validated_item['product'])
                quantity = validated_item['quantity']
                unit_price = validated_item['unit_price']
                discount_percentage = validated_item.get('discount_percentage', 0)
                discount_amount = validated_item.get('discount_amount', 0)

                bundle_quantity = None
                bundle_price = None
                if (
                    product.bundle_quantity
                    and product.bundle_price is not None
                    and quantity >= product.bundle_quantity
                ):
                    bundle_quantity = product.bundle_quantity
                    bundle_price = product.bundle_price

                # Create the item
                sale_item = SaleItem.objects.create(
                    sale=sale,
                    product=product,
                    quantity=quantity,
                    unit_price=unit_price,
                    discount_percentage=discount_percentage,
                    discount_amount=discount_amount,
                    bundle_quantity=bundle_quantity,
                    bundle_price=bundle_price,
                    list_price=product.selling_price,
                )
                if unit_price != product.selling_price:
                    price_overrides.append((sale_item, product.selling_price))
            except serializers.ValidationError:
                raise
            except Exception as e:
                # Clean up sale if item creation fails
                sale.delete()
                raise serializers.ValidationError({"items": [f"Item {i+1}: {str(e)}"]})
        
        # Items are now created. The post_save signal fired during Sale.objects.create
        # but saw 0 items. We must manually reserve inventory now.
        # Audit price overrides only once every line succeeded, so a rolled-back
        # sale never leaves entries behind.
        request = self.context.get('request')
        actor = getattr(request, 'user', None) or sale.sold_by
        for sale_item, catalog_price in price_overrides:
            record_price_override(sale, sale_item, catalog_price, actor)
        if sale.status == 'PENDING':
            from .signals import _reserve_inventory, _get_or_create_location
            location = _get_or_create_location()
            if location:
                _reserve_inventory(sale, location)
        
        return sale

    def get_total_amount(self, obj):
        items = SaleItem.objects.filter(sale=obj)
        return sum(item.line_total for item in items)

    def get_subtotal_amount(self, obj):
        return str(obj.list_subtotal.quantize(Decimal('0.01')))

    def get_discount_total(self, obj):
        return str(obj.total_discount.quantize(Decimal('0.01')))


class LaybyItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    line_total = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = LaybyItem
        fields = [
            'id', 'layby', 'product', 'product_name', 'product_sku',
            'quantity', 'unit_price', 'line_total'
        ]

    def get_line_total(self, obj):
        """Calculate line total."""
        return obj.line_total

    def validate(self, data):
        if data.get('quantity', 0) <= 0:
            raise serializers.ValidationError("Quantity must be greater than 0")
        if data.get('unit_price', 0) < 0:
            raise serializers.ValidationError("Price cannot be negative")
        return data


class LaybyPaymentSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    pos_terminal_name = serializers.CharField(source='pos_terminal.name', read_only=True, allow_null=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)
    
    class Meta:
        model = LaybyPayment
        fields = [
            'id', 'layby', 'branch', 'branch_name', 'pos_terminal', 'pos_terminal_name',
            'amount', 'payment_method', 'reference',
            'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by', 'updated_by']

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Payment amount must be greater than 0")
        return value
    
    def validate_payment_method(self, value):
        """Ensure payment_method is valid."""
        if not value:
            return 'CASH'  # Default to CASH if not provided
        return value
    
    def validate(self, data):
        """Validate layby payment."""
        # Get the layby to check balance
        layby = data.get('layby')
        amount = data.get('amount')
        
        if layby and amount:
            # Check if payment exceeds balance
            if amount > layby.balance:
                raise serializers.ValidationError(
                    f"Payment exceeds remaining balance ({layby.balance})"
                )

        branch = data.get('branch') or getattr(layby, 'branch', None)
        pos_terminal = data.get('pos_terminal')

        if pos_terminal and branch and pos_terminal.branch_id != branch.id:
            raise serializers.ValidationError(
                {"pos_terminal": ["Selected POS terminal does not belong to the selected branch."]}
            )
        
        return data


class LaybyRefundSerializer(serializers.ModelSerializer):
    class Meta:
        model = LaybyRefund
        fields = [
            'id', 'layby', 'amount_paid', 'refund_amount',
            'penalty_amount', 'reason', 'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by']

    def validate(self, data):
        if data.get('refund_amount', 0) < 0:
            raise serializers.ValidationError("Refund amount cannot be negative")
        if data.get('penalty_amount', 0) < 0:
            raise serializers.ValidationError("Penalty cannot be negative")
        return data


class LaybySerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    pos_terminal_name = serializers.CharField(source='pos_terminal.name', read_only=True, allow_null=True)
    items = LaybyItemSerializer(many=True, read_only=True)
    payments = LaybyPaymentSerializer(many=True, read_only=True)
    total_amount = serializers.SerializerMethodField(read_only=True)
    total_paid = serializers.SerializerMethodField(read_only=True)
    balance = serializers.SerializerMethodField(read_only=True)
    is_overdue = serializers.SerializerMethodField(read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Layby
        fields = [
            'id', 'customer', 'customer_name', 'reference', 'status',
            'branch', 'branch_name', 'pos_terminal', 'pos_terminal_name',
            'items', 'payments', 'total_amount', 'total_paid', 'balance',
            'due_date', 'is_overdue', 'notes', 'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = [
            'reference', 'created_at', 'updated_at', 'total_amount', 'total_paid',
            'balance', 'is_overdue', 'created_by', 'updated_by'
        ]

    def validate_customer(self, value):
        if not value.is_active:
            raise serializers.ValidationError("Selected customer is inactive")
        return value

    def validate_due_date(self, value):
        if value <= timezone.now().date():
            raise serializers.ValidationError("Due date must be in the future")
        return value

    def validate(self, data):
        """Validate layby data including items."""
        # Check if items are provided in initial_data
        if 'items' in self.initial_data:
            items = self.initial_data.get('items', [])
            if not items:
                raise serializers.ValidationError({"items": ["At least one item is required"]})

        branch = data.get('branch')
        pos_terminal = data.get('pos_terminal')
        if pos_terminal and branch and pos_terminal.branch_id != branch.id:
            raise serializers.ValidationError(
                {"pos_terminal": ["Selected POS terminal does not belong to the selected branch."]}
            )
        
        return data

    def create(self, validated_data):
        """Handle nested items creation during layby creation."""
        items_data = self.initial_data.get('items', [])
        
        # Validate items exist
        if not items_data:
            raise serializers.ValidationError({"items": ["At least one item is required"]})
        
        # Validate every line and merge duplicate products (unique per layby).
        merged = {}
        for i, item_data in enumerate(items_data):
            item_serializer = QuotationItemInputSerializer(data=item_data)
            if not item_serializer.is_valid():
                raise serializers.ValidationError(
                    {"items": [f"Item {i + 1}: {item_serializer.errors}"]}
                )
            clean = item_serializer.validated_data
            key = (clean['product'], clean['unit_price'])
            if key in merged:
                merged[key]['quantity'] += clean['quantity']
            else:
                merged[key] = {
                    'product': clean['product'],
                    'quantity': clean['quantity'],
                    'unit_price': clean['unit_price'],
                }

        product_ids = [line['product'] for line in merged.values()]
        if len(set(product_ids)) != len(product_ids):
            raise serializers.ValidationError(
                {"items": ["The same product cannot be added twice with different prices."]}
            )

        total_amount = sum(
            (Decimal(line['quantity']) * line['unit_price'] for line in merged.values()),
            Decimal('0.00'),
        )
        if total_amount <= 0:
            raise serializers.ValidationError(
                {"items": ["Layby total must be greater than 0"]}
            )

        validated_data['total_amount'] = total_amount
        validated_data['reference'] = self._generate_reference()

        with transaction.atomic():
            layby = Layby.objects.create(**validated_data)
            for line in merged.values():
                LaybyItem.objects.create(
                    layby=layby,
                    product_id=line['product'],
                    quantity=line['quantity'],
                    unit_price=line['unit_price'],
                )

        return layby

    @staticmethod
    def _generate_reference():
        timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
        while True:
            reference = f"LB-{timestamp}-{uuid.uuid4().hex[:4].upper()}"
            if not Layby.objects.filter(reference=reference).exists():
                return reference

    def get_total_amount(self, obj):
        items = LaybyItem.objects.filter(layby=obj)
        return sum(item.line_total for item in items)

    def get_total_paid(self, obj):
        payments = LaybyPayment.objects.filter(layby=obj)
        return sum(payment.amount for payment in payments)

    def get_balance(self, obj):
        total = self.get_total_amount(obj)
        paid = self.get_total_paid(obj)
        return total - paid

    def get_is_overdue(self, obj):
        return obj.is_overdue


class InventoryVerificationItemSerializer(serializers.Serializer):
    """
    Serializer for individual item inventory verification details.
    Compares expected vs. actual stock deductions.
    """
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    product_sku = serializers.CharField()
    
    # Expected quantities (what should have been deducted)
    expected_quantity_sold = serializers.IntegerField(
        help_text="Units sold in this transaction"
    )
    expected_unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    expected_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    
    # Actual stock state
    current_stock_quantity = serializers.IntegerField(
        help_text="Current physical quantity in all locations"
    )
    current_reserved_quantity = serializers.IntegerField(
        help_text="Currently reserved quantity"
    )
    current_available_quantity = serializers.IntegerField(
        help_text="Available for sale (physical - reserved)"
    )
    
    # Stock locations breakdown
    stock_by_location = serializers.ListField(
        child=serializers.DictField(),
        help_text="Breakdown of stock by location"
    )
    
    # Verification result
    verification_status = serializers.CharField(
        help_text="VERIFIED, MISMATCH, or MISSING"
    )
    discrepancy = serializers.IntegerField(
        help_text="Difference between expected and actual (0 = perfect)"
    )
    notes = serializers.CharField(
        help_text="Details about any discrepancies",
        required=False,
        allow_blank=True
    )


class InventoryVerificationSerializer(serializers.Serializer):
    """
    Comprehensive inventory verification report for a completed sale.
    Confirms that stock was properly deducted.
    """
    sale_id = serializers.IntegerField()
    sale_reference = serializers.CharField()
    sale_status = serializers.CharField()
    sale_created_at = serializers.DateTimeField()
    
    # Verification summary
    verification_timestamp = serializers.DateTimeField(
        help_text="When this verification was performed"
    )
    overall_status = serializers.CharField(
        help_text="VERIFIED, PARTIAL, or FAILED"
    )
    verified_items = serializers.IntegerField(
        help_text="Number of items with correct stock deduction"
    )
    mismatch_items = serializers.IntegerField(
        help_text="Number of items with stock discrepancies"
    )
    missing_items = serializers.IntegerField(
        help_text="Number of items with no stock records"
    )
    
    # Financial totals
    total_sale_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Total sale value"
    )
    
    # Detailed item verification
    items = InventoryVerificationItemSerializer(
        many=True,
        help_text="Item-by-item verification details"
    )
    
    # Overall assessment
    all_inventory_deducted = serializers.BooleanField(
        help_text="True if all stock was properly deducted"
    )
    requires_manual_review = serializers.BooleanField(
        help_text="True if there are discrepancies requiring attention"
    )
    
    # Recommendations
    recommendations = serializers.ListField(
        child=serializers.CharField(),
        help_text="Suggested actions if issues found"
    )


class DailyReconciliationSerializer(serializers.ModelSerializer):
    """Serializer for daily reconciliation reports."""
    prepared_by_name = serializers.CharField(source='prepared_by.get_full_name', read_only=True)
    verified_by_name = serializers.CharField(source='verified_by.get_full_name', read_only=True, allow_null=True)
    
    # Calculated properties
    total_sales = serializers.SerializerMethodField(read_only=True)
    total_layby_payments = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = DailyReconciliation
        fields = [
            'id', 'reconciliation_date',
            'total_cash_sales', 'total_card_sales',
            'total_layby_cash', 'total_layby_card', 'total_layby_cheques', 'total_layby_transfers',
            'total_sales', 'total_layby_payments',
            'total_cash_in', 'total_electronics', 'total_cheques', 'total_expected',
            'counted_cash', 'variance', 'variance_note',
            'is_locked', 'locked_at',
            'prepared_by', 'prepared_by_name', 'verified_by', 'verified_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'total_sales', 'total_layby_payments',
            'total_cash_in', 'total_electronics', 'total_cheques', 'total_expected',
            'variance', 'created_at', 'updated_at'
        ]

    def get_total_sales(self, obj):
        """Calculate total sales (cash + card)."""
        return obj.total_sales

    def get_total_layby_payments(self, obj):
        """Calculate total layby payments (all methods)."""
        return obj.total_layby_payments

    def validate_counted_cash(self, value):
        """Validate counted cash is positive."""
        if value is not None and value < 0:
            raise serializers.ValidationError("Counted cash cannot be negative")
        return value

    def validate(self, data):
        """Validate reconciliation data."""
        # If counted_cash is provided, calculate variance
        if data.get('counted_cash') is not None:
            instance = self.instance
            total_cash_in = Decimal(str(instance.total_cash_in if instance else 0))
            counted = Decimal(str(data['counted_cash']))
            data['variance'] = counted - total_cash_in
        
        return data


class DailyReconciliationDetailSerializer(serializers.Serializer):
    """Summary serializer showing reconciliation without full nested details."""
    reconciliation_id = serializers.IntegerField()
    date = serializers.DateField()
    prepared_by = serializers.CharField()
    status = serializers.CharField()
    
    # Aggregates
    total_expected = serializers.DecimalField(max_digits=12, decimal_places=2)
    counted_cash = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    variance = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    is_locked = serializers.BooleanField()


class ReturnItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    line_total = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = ReturnItem
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'quantity',
            'unit_price', 'reason', 'line_total'
        ]
        read_only_fields = ['id']
    
    def get_line_total(self, obj):
        """Calculate line total."""
        return float(obj.line_total)
    
    def validate_quantity(self, value):
        """Validate quantity is positive."""
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than 0")
        return value
    
    def validate_unit_price(self, value):
        """Validate unit price is positive."""
        if value <= 0:
            raise serializers.ValidationError("Unit price must be greater than 0")
        return value


class ReturnSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    sale_reference = serializers.CharField(source='sale.reference', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    pos_terminal_name = serializers.CharField(source='pos_terminal.name', read_only=True, allow_null=True)
    approved_by_name = serializers.CharField(source='approved_by.get_full_name', read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)
    items = ReturnItemSerializer(many=True, read_only=True)
    item_count = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = Return
        fields = [
            'id', 'return_number', 'return_type', 'sale', 'sale_reference',
            'purchase', 'customer', 'customer_name', 'supplier', 'supplier_name',
            'branch', 'branch_name', 'pos_terminal', 'pos_terminal_name',
            'status', 'reason', 'notes', 'total_amount', 'refund_amount',
            'refund_method', 'approved_by', 'approved_by_name', 'approved_at',
            'items', 'item_count', 'created_at', 'updated_at', 
            'created_by', 'updated_by'
        ]
        read_only_fields = [
            'id', 'return_number', 'approved_by', 'approved_at',
            'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
    
    def get_item_count(self, obj):
        """Count items in return."""
        return obj.item_count
    
    def validate_total_amount(self, value):
        """Validate total amount is non-negative."""
        if value < 0:
            raise serializers.ValidationError("Total amount cannot be negative")
        return value
    
    def validate_refund_amount(self, value):
        """Validate refund amount is non-negative."""
        if value is not None and value < 0:
            raise serializers.ValidationError("Refund amount cannot be negative")
        return value

    def validate(self, data):
        data = super().validate(data)

        branch = data.get('branch')
        pos_terminal = data.get('pos_terminal')
        sale = data.get('sale')
        purchase = data.get('purchase')

        derived_branch = branch
        if sale and getattr(sale, 'branch_id', None):
            derived_branch = sale.branch
        elif purchase and getattr(purchase, 'branch_id', None):
            derived_branch = purchase.branch

        if pos_terminal and derived_branch and pos_terminal.branch_id != derived_branch.id:
            raise serializers.ValidationError(
                {"pos_terminal": ["Selected POS terminal does not belong to the selected branch."]}
            )

        return data


