from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CategoryViewSet, ProductViewSet, ProductVariantViewSet,
    LocationViewSet, StockViewSet, StockTransactionViewSet,
    StockAdjustmentViewSet, SupplierViewSet, PurchaseViewSet, PurchaseItemViewSet
)

router = DefaultRouter()
router.register(r'categories', CategoryViewSet, basename='category')
router.register(r'products', ProductViewSet, basename='product')
router.register(r'variants', ProductVariantViewSet, basename='variant')
router.register(r'locations', LocationViewSet, basename='location')
router.register(r'stock', StockViewSet, basename='stock')
router.register(r'transactions', StockTransactionViewSet, basename='transaction')
router.register(r'adjustments', StockAdjustmentViewSet, basename='adjustment')
router.register(r'suppliers', SupplierViewSet, basename='supplier')
router.register(r'purchases', PurchaseViewSet, basename='purchase')
router.register(r'purchase-items', PurchaseItemViewSet, basename='purchase-item')

urlpatterns = [
    path('', include(router.urls)),
]
