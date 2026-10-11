from django.contrib import admin
from .models import Category, Product, Location, Stock, StockTransaction, Supplier, Purchase, PurchaseItem


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'category', 'sku', 'cost_price', 'selling_price',
        'bundle_quantity', 'bundle_price', 'reorder_level', 'is_active'
    )
    list_filter = ('category', 'is_active', 'created_at')
    search_fields = ('name', 'sku', 'barcode')
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ('product', 'location', 'quantity', 'reserved_quantity', 'available_stock')
    list_filter = ('location', 'product__category')
    search_fields = ('product__name',)


@admin.register(StockTransaction)
class StockTransactionAdmin(admin.ModelAdmin):
    list_display = ('product', 'location', 'transaction_type', 'quantity', 'reference', 'created_at')
    list_filter = ('transaction_type', 'location', 'created_at')
    search_fields = ('product__name', 'reference')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'phone', 'contact_person', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name', 'email', 'phone', 'contact_person')
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ('reference', 'supplier', 'status', 'delivery_date', 'created_at')
    list_filter = ('status', 'supplier', 'created_at')
    search_fields = ('reference', 'supplier__name')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')


@admin.register(PurchaseItem)
class PurchaseItemAdmin(admin.ModelAdmin):
    list_display = ('purchase', 'product', 'quantity', 'cost_price', 'received_quantity')
    list_filter = ('purchase', 'product__category')
    search_fields = ('product__name', 'purchase__reference')
