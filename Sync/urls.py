from django.urls import path
from .views import (
    SyncPushView, SyncPullView, SyncStatusView, SyncChangesView,
    SyncSalesView, SyncPaymentsView, SyncStockMovementsView
)

urlpatterns = [
    path('push/', SyncPushView.as_view(), name='sync_push'),
    path('pull/', SyncPullView.as_view(), name='sync_pull'),
    path('status/', SyncStatusView.as_view(), name='sync_status'),
    path('changes/', SyncChangesView.as_view(), name='sync_changes'),
    path('sales/', SyncSalesView.as_view(), name='sync_sales'),
    path('payments/', SyncPaymentsView.as_view(), name='sync_payments'),
    path('stock-movements/', SyncStockMovementsView.as_view(), name='sync_stock_movements'),
]
