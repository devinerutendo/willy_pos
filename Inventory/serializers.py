from rest_framework import serializers
from Core.models import POSTerminal
from .models import Category, Product, Location, Stock, StockTransaction, StockAdjustment, Supplier, Purchase, PurchaseItem
from .utils import generate_purchase_reference
from django.db import IntegrityError


class CategorySerializer(serializers.ModelSerializer):
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)
    
    class Meta:
        model = Category
        fields = ['id', 'name', 'is_active', 'description', 'created_at', 'updated_at', 'created_by', 'updated_by']
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by']

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Category name cannot be empty")
        return value.strip()


class ProductVariantCompatibilitySerializer(serializers.ModelSerializer):
    """
    Serializer that presents Products as ProductVariants for frontend compatibility.
    This compensates for removing the ProductVariant model.
    Frontend expects variant endpoints with id, product, size, color, model, selling_price
    """
    # Add product as a serializer field instead of trying to get it from model
    product = serializers.SerializerMethodField()
    size = serializers.CharField(default='', allow_blank=True)
    color = serializers.CharField(default='', allow_blank=True)
    model = serializers.CharField(default='', allow_blank=True)
    
    class Meta:
        model = Product
        fields = [
            'id', 'name', 'product', 'size', 'color', 'model',
            'selling_price', 'cost_price', 'bundle_quantity', 'bundle_price', 'category'
        ]
    
    def get_product(self, obj):
        """Return product ID as the product field"""
        return obj.id
    
    def to_representation(self, instance):
        """Custom representation to match ProductVariant interface"""
        return {
            'id': instance.id,
            'product': instance.id,
            'name': instance.name,
            'size': '',
            'color': '',
            'model': '',
            'selling_price': str(instance.selling_price),
            'cost_price': str(instance.cost_price),
            'bundle_quantity': instance.bundle_quantity,
            'bundle_price': str(instance.bundle_price) if instance.bundle_price is not None else None,
            'category': instance.category.id,
        }
    
    def to_internal_value(self, data):
        """Handle incoming data from frontend"""
        return {
            'id': data.get('id'),
            'name': data.get('name'),
            'selling_price': data.get('selling_price'),
            'cost_price': data.get('cost_price'),
            'bundle_quantity': data.get('bundle_quantity'),
            'bundle_price': data.get('bundle_price'),
            'category': data.get('category'),
        }


class ProductSerializer(serializers.ModelSerializer):
    category = CategorySerializer(read_only=True)
    category_id = serializers.IntegerField(write_only=True, required=True)
    sku = serializers.CharField(read_only=True)
    profit_margin = serializers.SerializerMethodField(read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'category', 'category_id', 'sku', 'barcode',
            'description', 'cost_price', 'selling_price', 'profit_margin',
            'bundle_quantity', 'bundle_price', 'reorder_level', 'is_active',
            'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = ['sku', 'created_at', 'updated_at', 'profit_margin', 'created_by', 'updated_by']

    def get_profit_margin(self, obj):
        return round(obj.profit_margin, 2)

    def to_internal_value(self, data):
        """Handle category_id field and convert empty optional bundle fields to null."""
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f'ProductSerializer.to_internal_value() received: {data}')

        if data is None:
            return super().to_internal_value(data)

        normalized = dict(data)
        for key in ('bundle_quantity', 'bundle_price', 'barcode'):
            value = normalized.get(key)
            if isinstance(value, str) and value.strip() == '':
                normalized[key] = None

        result = super().to_internal_value(normalized)

        # Convert category_id to category if provided
        if 'category_id' in result:
            try:
                category = Category.objects.get(id=result['category_id'])
                result['category'] = category
            except Category.DoesNotExist:
                raise serializers.ValidationError({'category_id': 'Category not found'})
            del result['category_id']

        if result.get('bundle_quantity') in ('', None):
            result['bundle_quantity'] = None
        if result.get('bundle_price') in ('', None):
            result['bundle_price'] = None

        return result

    def validate_name(self, value):
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f'Validating name: {value!r} (type: {type(value).__name__})')
        if not value or not value.strip():
            raise serializers.ValidationError("Product name cannot be empty")
        if len(value) < 2:
            raise serializers.ValidationError("Product name must be at least 2 characters")
        return value.strip()

    def validate_sku(self, value):
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f'Validating sku: {value!r} (type: {type(value).__name__})')
        if not value or not value.strip():
            raise serializers.ValidationError("SKU is required")
        if len(value) > 50:
            raise serializers.ValidationError("SKU cannot exceed 50 characters")
        
        value = value.strip()
        query = Product.objects.filter(sku=value)
        if self.instance:
            query = query.exclude(pk=self.instance.pk)
        
        if query.exists():
            raise serializers.ValidationError("This SKU already exists")
        return value

    def validate_category(self, value):
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f'Validating category: {value!r} (type: {type(value).__name__})')
        if not value:
            raise serializers.ValidationError("Category is required")
        return value
    
    def validate_reorder_level(self, value):
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f'Validating reorder_level: {value!r} (type: {type(value).__name__})')
        if value is not None and value < 0:
            raise serializers.ValidationError("Reorder level cannot be negative")
        return value
    
    def validate_barcode(self, value):
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f'Validating barcode: {value!r} (type: {type(value).__name__})')
        if value == "":
            return None
        if value and len(value) > 50:
            raise serializers.ValidationError("Barcode cannot exceed 50 characters")
        if value:
            query = Product.objects.filter(barcode=value)
            if self.instance:
                query = query.exclude(pk=self.instance.pk)
            if query.exists():
                raise serializers.ValidationError("This barcode already exists")
        return value

    def validate(self, data):
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f'Running final validation on data: {data}')
        cost_price = data.get('cost_price', getattr(self.instance, 'cost_price', None))
        selling_price = data.get('selling_price', getattr(self.instance, 'selling_price', None))
        bundle_quantity = data.get('bundle_quantity', getattr(self.instance, 'bundle_quantity', None))
        bundle_price = data.get('bundle_price', getattr(self.instance, 'bundle_price', None))

        if cost_price is not None and selling_price is not None:
            if selling_price < cost_price:
                raise serializers.ValidationError("Selling price must be >= cost price")

        has_bundle_quantity = bundle_quantity is not None
        has_bundle_price = bundle_price is not None

        if has_bundle_quantity != has_bundle_price:
            raise serializers.ValidationError(
                "Bundle quantity and bundle price must both be provided together"
            )

        if has_bundle_quantity:
            if bundle_quantity <= 1:
                raise serializers.ValidationError("Bundle quantity must be greater than 1")
            if bundle_price < 0:
                raise serializers.ValidationError("Bundle price cannot be negative")

        return data

    def create(self, validated_data):
        try:
            return super().create(validated_data)
        except IntegrityError as e:
            error_str = str(e).lower()
            if 'barcode' in error_str:
                raise serializers.ValidationError({'barcode': 'This barcode already exists'})
            elif 'sku' in error_str:
                raise serializers.ValidationError({'sku': 'This SKU already exists'})
            raise
    
    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except IntegrityError as e:
            error_str = str(e).lower()
            if 'barcode' in error_str:
                raise serializers.ValidationError({'barcode': 'This barcode already exists'})
            elif 'sku' in error_str:
                raise serializers.ValidationError({'sku': 'This SKU already exists'})
            raise


class LocationSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)
    
    class Meta:
        model = Location
        fields = ['id', 'branch', 'branch_name', 'name', 'address', 'is_active', 'created_at', 'updated_at', 'created_by', 'updated_by']
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by']


class StockSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    location_name = serializers.CharField(source='location.name', read_only=True)
    branch_name = serializers.CharField(source='location.branch.name', read_only=True)
    available_stock = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Stock
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'location', 'location_name',
            'branch_name', 'quantity', 'reserved_quantity', 'available_stock'
        ]

    def get_available_stock(self, obj):
        return obj.available_stock

    def validate(self, data):
        if data.get('quantity', 0) < 0:
            raise serializers.ValidationError("Quantity cannot be negative")
        if data.get('reserved_quantity', 0) < 0:
            raise serializers.ValidationError("Reserved quantity cannot be negative")
        if data.get('reserved_quantity', 0) > data.get('quantity', 0):
            raise serializers.ValidationError("Reserved cannot exceed total quantity")
        return data


class StockTransactionSerializer(serializers.ModelSerializer):
    """
    Serializer for StockTransaction audit trail entries.
    
    Provides complete visibility into all inventory movements for
    compliance, reconciliation, and audit purposes.
    """
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    location_name = serializers.CharField(source='location.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    pos_terminal_name = serializers.CharField(source='pos_terminal.name', read_only=True, allow_null=True)
    transaction_type_display = serializers.CharField(source='get_transaction_type_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True, allow_null=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = StockTransaction
        fields = [
            'id', 
            'product', 'product_name', 'product_sku',
            'location', 'location_name',
            'branch', 'branch_name', 'pos_terminal', 'pos_terminal_name',
            'transaction_type', 'transaction_type_display',
            'quantity',
            'reference',
            'notes',
            'created_at', 'updated_at',
            'created_by', 'created_by_name',
            'updated_by'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by', 'created_by_name', 'updated_by', 'transaction_type_display']

    def validate(self, data):
        if data.get('quantity', 0) == 0:
            raise serializers.ValidationError("Quantity cannot be zero")
        return data


class StockAdjustmentSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    location_name = serializers.CharField(source='location.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    pos_terminal_name = serializers.CharField(source='pos_terminal.name', read_only=True, allow_null=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)
    quantity_after = serializers.IntegerField(write_only=True, required=True)

    class Meta:
        model = StockAdjustment
        fields = [
            'id', 'product', 'product_name', 'product_sku',
            'location', 'location_name', 'branch', 'branch_name',
            'pos_terminal', 'pos_terminal_name',
            'quantity_before', 'quantity_after', 'quantity_delta',
            'reason', 'reference', 'notes',
            'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = [
            'id', 'quantity_before', 'quantity_delta', 'branch', 'created_at',
            'updated_at', 'created_by', 'updated_by', 'product_name', 'product_sku',
            'location_name', 'branch_name', 'pos_terminal_name'
        ]

    def validate(self, data):
        location = data.get('location')
        product = data.get('product')
        quantity_after = data.get('quantity_after')

        if quantity_after is None or quantity_after < 0:
            raise serializers.ValidationError({'quantity_after': 'Adjusted quantity must be zero or greater.'})

        if not Stock.objects.filter(product=product, location=location).exists():
            raise serializers.ValidationError({'location': 'No stock record exists for this product at the selected location.'})

        if data.get('pos_terminal') and data['pos_terminal'].branch_id != location.branch_id:
            raise serializers.ValidationError({'pos_terminal': 'POS terminal must belong to the same branch as the location.'})

        return data

    def create(self, validated_data):
        quantity_after = validated_data.pop('quantity_after')
        stock = Stock.objects.get(product=validated_data['product'], location=validated_data['location'])
        user = self.context['request'].user
        return StockAdjustment.apply_adjustment(
            stock=stock,
            new_quantity=quantity_after,
            user=user,
            reason=validated_data['reason'],
            reference=validated_data.get('reference', ''),
            notes=validated_data.get('notes', ''),
            pos_terminal=validated_data.get('pos_terminal'),
        )


class SupplierSerializer(serializers.ModelSerializer):
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)
    
    class Meta:
        model = Supplier
        fields = [
            'id', 'name', 'email', 'phone', 'address', 'contact_person',
            'is_active', 'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by']

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Supplier name cannot be empty")
        return value.strip()

    def validate_email(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Email is required")
        if '@' not in value or '.' not in value:
            raise serializers.ValidationError("Invalid email format")
        return value.strip()

    def validate_phone(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Phone is required")
        return value.strip()


class PurchaseItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    variant = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = PurchaseItem
        fields = [
            'id', 'purchase', 'product', 'variant', 'product_name', 'product_sku',
            'quantity', 'cost_price', 'received_quantity'
        ]
        read_only_fields = ['id', 'purchase', 'variant', 'product_name', 'product_sku', 'received_quantity']

    def get_variant(self, obj):
        """Return product ID as variant for frontend compatibility"""
        return obj.product.id

    def validate(self, data):
        if data.get('quantity', 0) <= 0:
            raise serializers.ValidationError("Quantity must be greater than 0")
        if data.get('cost_price', 0) < 0:
            raise serializers.ValidationError("Cost price cannot be negative")
        return data


class PurchaseSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    items = PurchaseItemSerializer(many=True, read_only=True)
    total_cost = serializers.SerializerMethodField(read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Purchase
        fields = [
            'id', 'supplier', 'supplier_name', 'reference', 'status',
            'branch', 'branch_name', 'delivery_date', 'notes', 'items', 'total_cost',
            'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by']

    def get_total_cost(self, obj):
        return str(obj.total_cost)


class PurchaseCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating purchases with nested items"""
    items = PurchaseItemSerializer(many=True, write_only=True)
    reference = serializers.CharField(required=False, allow_blank=True, max_length=100)
    
    class Meta:
        model = Purchase
        fields = ['supplier', 'branch', 'reference', 'delivery_date', 'notes', 'status', 'items']
        read_only_fields = ['status']
    
    def validate_supplier(self, value):
        """Validate supplier exists"""
        if not value:
            raise serializers.ValidationError("Supplier is required. Please select or create a supplier.")
        return value
    
    def validate_reference(self, value):
        """Validate reference - auto-generated if not provided"""
        if value and isinstance(value, str):
            value = value.strip()
            if not value:
                return ""
            if len(value) < 2:
                raise serializers.ValidationError("Reference must be at least 2 characters long.")
            if len(value) > 100:
                raise serializers.ValidationError("Reference cannot exceed 100 characters.")
            return value
        return ""
    
    def validate_items(self, value):
        """Validate items array"""
        if not value or len(value) == 0:
            raise serializers.ValidationError("At least one purchase item is required.")
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        
        # Auto-generate reference if not provided
        reference = validated_data.get('reference') or ''
        if not reference.strip():
            validated_data['reference'] = generate_purchase_reference()
        
        # Auto-receive purchases on creation for better UX
        validated_data.setdefault('status', Purchase.RECEIVED)
        
        from django.db import transaction
        with transaction.atomic():
            purchase = Purchase.objects.create(**validated_data)
            for item_data in items_data:
                item_data['received_quantity'] = item_data.get('quantity', 0)
                PurchaseItem.objects.create(purchase=purchase, **item_data)
        user = self.context.get('request').user if self.context.get('request') else None
        purchase.update_stock(user=user)
    
        return purchase
    
    def to_representation(self, instance):
        """Return full purchase data with items for the response"""
        # Use PurchaseSerializer for reading the response
        return PurchaseSerializer(instance).data
