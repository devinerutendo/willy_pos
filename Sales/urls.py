from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CustomerViewSet, QuotationViewSet, QuotationItemViewSet,
    SaleViewSet, SaleItemViewSet,
    LaybyViewSet, LaybyItemViewSet, LaybyPaymentViewSet, LaybyRefundViewSet,
    DailyReconciliationViewSet, ReturnViewSet, ReturnItemViewSet, InvoiceViewSet
)

router = DefaultRouter()
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'quotations', QuotationViewSet, basename='quotation')
router.register(r'quotation-items', QuotationItemViewSet, basename='quotation-item')
router.register(r'invoices', InvoiceViewSet, basename='invoice')
router.register(r'sales', SaleViewSet, basename='sale')
router.register(r'sale-items', SaleItemViewSet, basename='sale-item')
router.register(r'laybys', LaybyViewSet, basename='layby')
router.register(r'layby-items', LaybyItemViewSet, basename='layby-item')
router.register(r'layby-payments', LaybyPaymentViewSet, basename='layby-payment')
router.register(r'layby-refunds', LaybyRefundViewSet, basename='layby-refund')
router.register(r'reconciliations', DailyReconciliationViewSet, basename='daily-reconciliation')
router.register(r'returns', ReturnViewSet, basename='return')
router.register(r'return-items', ReturnItemViewSet, basename='return-item')

urlpatterns = [
    path('', include(router.urls)),
]
