from django.contrib import admin
from .models import (
    Customer, Quotation, QuotationItem, Sale, SaleItem,
    Layby, LaybyItem, LaybyPayment, LaybyRefund,
    Invoice, InvoiceItem, InvoiceStatusHistory,
)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'phone', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name', 'email', 'phone')
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ('reference', 'customer', 'created_by', 'status', 'valid_until', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('reference', 'customer__name', 'created_by__username')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(QuotationItem)
class QuotationItemAdmin(admin.ModelAdmin):
    list_display = ('quotation', 'product', 'quantity', 'unit_price')
    list_filter = ('quotation', 'product__category')
    search_fields = ('product__name', 'quotation__reference')


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ('reference', 'customer', 'sold_by', 'status', 'created_at')
    list_filter = ('status', 'created_at', 'sold_by')
    search_fields = ('reference', 'customer__name', 'sold_by__username')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(SaleItem)
class SaleItemAdmin(admin.ModelAdmin):
    list_display = ('sale', 'product', 'quantity', 'unit_price')
    list_filter = ('sale', 'product__category')
    search_fields = ('product__name', 'sale__reference')


@admin.register(Layby)
class LaybyAdmin(admin.ModelAdmin):
    list_display = ('reference', 'customer', 'status', 'total_amount', 'balance', 'due_date', 'created_at')
    list_filter = ('status', 'created_at', 'due_date')
    search_fields = ('reference', 'customer__name')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(LaybyItem)
class LaybyItemAdmin(admin.ModelAdmin):
    list_display = ('layby', 'product', 'quantity', 'unit_price')
    list_filter = ('layby', 'product__category')
    search_fields = ('product__name', 'layby__reference')


@admin.register(LaybyPayment)
class LaybyPaymentAdmin(admin.ModelAdmin):
    list_display = ('layby', 'amount', 'reference', 'created_at')
    list_filter = ('layby', 'created_at')
    search_fields = ('reference', 'layby__reference')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(LaybyRefund)
class LaybyRefundAdmin(admin.ModelAdmin):
    list_display = ('layby', 'amount_paid', 'refund_amount', 'penalty_amount', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('layby__reference',)
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0
    can_delete = False
    readonly_fields = ('product', 'quantity', 'unit_price', 'discount_percentage', 'discount_amount')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    """Read-only: status changes must go through the approval workflow."""
    list_display = ('reference', 'customer', 'status', 'sale', 'approved_by', 'approved_at', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('reference', 'customer__name', 'sale__reference')
    inlines = [InvoiceItemInline]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InvoiceStatusHistory)
class InvoiceStatusHistoryAdmin(admin.ModelAdmin):
    """Append-only audit trail: view only."""
    list_display = ('invoice', 'action', 'from_status', 'to_status', 'actor', 'created_at')
    list_filter = ('action', 'created_at')
    search_fields = ('invoice__reference',)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
