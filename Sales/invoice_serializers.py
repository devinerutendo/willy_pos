from decimal import Decimal

from rest_framework import serializers

from Inventory.models import Product
from .invoicing import can_approve_invoices
from .models import Invoice, InvoiceItem, InvoiceStatusHistory


def _display_name(user):
    if not user:
        return None
    return f"{user.first_name} {user.last_name}".strip() or user.username or user.email


class InvoiceItemInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    discount_percentage = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False, default=0,
    )
    discount_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, default=0,
    )

    def validate(self, data):
        request = self.context.get('request')
        if request and not request.user.is_superuser:
            if data.get('discount_percentage', 0) > 0 or data.get('discount_amount', 0) > 0:
                raise serializers.ValidationError("Only admins can apply discounts")
        return data


class InvoiceItemSerializer(serializers.ModelSerializer):
    product_id = serializers.IntegerField(read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    effective_unit_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    total_price = serializers.DecimalField(source='line_total', max_digits=14, decimal_places=2, read_only=True)
    list_total_price = serializers.DecimalField(
        source='list_line_total', max_digits=14, decimal_places=2, read_only=True,
    )
    price_discount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = InvoiceItem
        fields = [
            'id', 'product_id', 'product_name', 'product_sku', 'quantity', 'unit_price',
            'discount_percentage', 'discount_amount', 'effective_unit_price', 'total_price',
            'list_price', 'list_total_price', 'price_discount',
        ]
        read_only_fields = fields


class InvoiceStatusHistorySerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    sale_reference = serializers.CharField(source='sale.reference', read_only=True, allow_null=True)

    class Meta:
        model = InvoiceStatusHistory
        fields = [
            'id', 'action', 'from_status', 'to_status', 'actor', 'actor_name',
            'reason', 'sale', 'sale_reference', 'detail', 'created_at',
        ]
        read_only_fields = fields

    def get_actor_name(self, obj):
        return _display_name(obj.actor)


class InvoiceSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    customer_email = serializers.CharField(source='customer.email', read_only=True)
    customer_address = serializers.CharField(source='customer.address', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    items = InvoiceItemSerializer(many=True, read_only=True)
    total_amount = serializers.SerializerMethodField()
    subtotal_amount = serializers.SerializerMethodField()
    discount_total = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    rejected_by_name = serializers.SerializerMethodField()
    cancelled_by_name = serializers.SerializerMethodField()
    sale_reference = serializers.CharField(source='sale.reference', read_only=True, allow_null=True)
    sale_status = serializers.CharField(source='sale.status', read_only=True, allow_null=True)
    payment_status = serializers.CharField(source='sale.payment_status', read_only=True, allow_null=True)
    quotation = serializers.SerializerMethodField()
    quotation_reference = serializers.SerializerMethodField()
    can_approve = serializers.SerializerMethodField()
    can_reject = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'reference', 'status', 'status_display',
            'customer', 'customer_name', 'customer_phone', 'customer_email', 'customer_address',
            'branch', 'branch_name', 'pos_terminal', 'notes',
            'items', 'total_amount', 'subtotal_amount', 'discount_total',
            'sale', 'sale_reference', 'sale_status', 'payment_status',
            'quotation', 'quotation_reference',
            'created_at', 'updated_at', 'created_by', 'created_by_name',
            'approved_by', 'approved_by_name', 'approved_at',
            'rejected_by', 'rejected_by_name', 'rejected_at', 'rejection_reason',
            'cancelled_by', 'cancelled_by_name', 'cancelled_at', 'cancellation_reason',
            'can_approve', 'can_reject', 'can_cancel', 'can_edit',
        ]
        read_only_fields = [
            'id', 'reference', 'status', 'sale', 'created_at', 'updated_at', 'created_by',
            'approved_by', 'approved_at', 'rejected_by', 'rejected_at', 'rejection_reason',
            'cancelled_by', 'cancelled_at', 'cancellation_reason', 'branch',
        ]

    def get_total_amount(self, obj):
        return str(obj.total_amount.quantize(Decimal('0.01')))

    def get_subtotal_amount(self, obj):
        return str(obj.list_subtotal.quantize(Decimal('0.01')))

    def get_discount_total(self, obj):
        return str(obj.total_discount.quantize(Decimal('0.01')))

    def get_created_by_name(self, obj):
        return _display_name(obj.created_by)

    def get_approved_by_name(self, obj):
        return _display_name(obj.approved_by)

    def get_rejected_by_name(self, obj):
        return _display_name(obj.rejected_by)

    def get_cancelled_by_name(self, obj):
        return _display_name(obj.cancelled_by)

    def _quotation(self, obj):
        return getattr(obj, 'source_quotation', None) if hasattr(obj, 'source_quotation') else None

    def get_quotation(self, obj):
        quotation = self._quotation(obj)
        return quotation.id if quotation else None

    def get_quotation_reference(self, obj):
        quotation = self._quotation(obj)
        return quotation.reference if quotation else None

    def _user(self):
        request = self.context.get('request')
        return getattr(request, 'user', None)

    def get_can_approve(self, obj):
        return obj.status == Invoice.PENDING_APPROVAL and can_approve_invoices(self._user())

    def get_can_reject(self, obj):
        return self.get_can_approve(obj)

    def get_can_cancel(self, obj):
        user = self._user()
        if obj.status not in (Invoice.DRAFT, Invoice.PENDING_APPROVAL) or not user:
            return False
        return can_approve_invoices(user) or obj.created_by_id == user.pk

    def get_can_edit(self, obj):
        return obj.is_editable

    def validate_customer(self, value):
        if not value.is_active:
            raise serializers.ValidationError("Selected customer is inactive")
        return value
