from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import SyncOperation
from .serializers import SyncOperationSerializer
from .processor import SyncProcessor
from Inventory.models import Product, Stock
from Sales.models import Customer
from Core.permissions import can_view_cost_data
import json

class SyncPushView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        device_id = request.data.get('device_id')
        operations = request.data.get('operations', [])
        
        if not device_id:
            return Response({'error': 'device_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        processed_count = 0
        
        for op in operations:
            operation_id = op.get('operation_id')
            if not operation_id:
                continue
                
            # Queue operations
            SyncOperation.objects.get_or_create(
                operation_id=operation_id,
                defaults={
                    'device_id': device_id,
                    'entity_type': op.get('entity_type', 'unknown'),
                    'action': op.get('action', 'CREATE'),
                    'payload': op.get('payload', {}),
                    'status': 'PENDING'
                }
            )
            processed_count += 1
            
        # Immediately process the batch synchronously
        processor = SyncProcessor(user=request.user)
        results = processor.process_batch(device_id)
            
        return Response({
            'status': 'completed',
            'processed_count': processed_count,
            'results': results
        })

class SyncPullView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        updated_after_str = request.query_params.get('updated_after')
        updated_after = None
        if updated_after_str:
            updated_after = parse_datetime(updated_after_str)
            
        # Fetch Products
        products_qs = Product.objects.all()
        if updated_after:
            products_qs = products_qs.filter(updated_at__gt=updated_after)
            
        show_cost = can_view_cost_data(request.user)
        products_data = []
        for p in products_qs:
            entry = {
                'id': p.id,
                'client_id': str(p.client_id) if hasattr(p, 'client_id') else None,
                'name': p.name,
                'sku': p.sku,
                'category': p.category.name if p.category else 'General',
                'selling_price': str(p.selling_price),
                'barcode': p.barcode,
                'reorder_level': p.reorder_level,
                'is_active': p.is_active,
                'updated_at': p.updated_at.isoformat() if p.updated_at else None
            }
            if show_cost:
                entry['cost_price'] = str(p.cost_price)
            products_data.append(entry)
            
        # Fetch Stock Levels (full dump for simplicity since stock is constantly changing)
        stock_qs = Stock.objects.select_related('product', 'location')
        stock_data = []
        for s in stock_qs:
            stock_data.append({
                'product_sku': s.product.sku,
                'location_name': s.location.name,
                'quantity': s.quantity,
                'reserved_quantity': s.reserved_quantity,
                'available_stock': s.available_stock
            })
            
        # Fetch Customers
        customers_qs = Customer.objects.all()
        if updated_after:
            customers_qs = customers_qs.filter(updated_at__gt=updated_after)
            
        customers_data = []
        for c in customers_qs:
            customers_data.append({
                'client_id': str(c.client_id) if hasattr(c, 'client_id') else None,
                'name': c.name,
                'email': c.email,
                'phone': c.phone,
                'city': c.city,
                'status': 'Active',
                'updated_at': c.updated_at.isoformat() if c.updated_at else None
            })
            
        return Response({
            'generated_at': timezone.now().isoformat(),
            'products': products_data,
            'stock_levels': stock_data,
            'customers': customers_data
        })

class SyncStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        device_id = request.query_params.get('device_id')
        if not device_id:
            return Response({'error': 'device_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        pending = SyncOperation.objects.filter(device_id=device_id, status='PENDING').count()
        failed = SyncOperation.objects.filter(device_id=device_id, status='FAILED').count()
        
        return Response({
            'device_id': device_id,
            'pending_operations': pending,
            'failed_operations': failed,
            'server_time': timezone.now().isoformat()
        })

class SyncChangesView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        device_id = request.query_params.get('device_id')
        queryset = SyncOperation.objects.all()
        if device_id:
            queryset = queryset.filter(device_id=device_id)
            
        recent_ops = queryset.order_by('-created_at')[:50]
        serializer = SyncOperationSerializer(recent_ops, many=True)
        return Response(serializer.data)

class SyncSalesView(APIView):
    """Specialized endpoint for syncing sales directly."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        device_id = request.data.get('device_id')
        sales = request.data.get('sales', [])
        
        if not device_id:
            return Response({'error': 'device_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        processed_count = 0
        for sale in sales:
            operation_id = sale.get('operation_id') or sale.get('client_id')
            if not operation_id:
                continue
                
            SyncOperation.objects.get_or_create(
                operation_id=operation_id,
                defaults={
                    'device_id': device_id,
                    'entity_type': 'sale',
                    'action': 'CREATE',
                    'payload': sale,
                    'status': 'PENDING'
                }
            )
            processed_count += 1
            
        processor = SyncProcessor(user=request.user)
        results = processor.process_batch(device_id)
            
        return Response({
            'status': 'completed',
            'processed_count': processed_count,
            'results': results
        })


class SyncPaymentsView(APIView):
    """Specialized endpoint for syncing payments (e.g. Layby payments) directly."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        device_id = request.data.get('device_id')
        payments = request.data.get('payments', [])
        
        if not device_id:
            return Response({'error': 'device_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        processed_count = 0
        for payment in payments:
            operation_id = payment.get('operation_id') or payment.get('client_id')
            if not operation_id:
                continue
                
            SyncOperation.objects.get_or_create(
                operation_id=operation_id,
                defaults={
                    'device_id': device_id,
                    'entity_type': 'payment',
                    'action': 'CREATE',
                    'payload': payment,
                    'status': 'PENDING'
                }
            )
            processed_count += 1
            
        processor = SyncProcessor(user=request.user)
        results = processor.process_batch(device_id)
            
        return Response({
            'status': 'completed',
            'processed_count': processed_count,
            'results': results
        })


class SyncStockMovementsView(APIView):
    """Specialized endpoint for syncing offline stock movements/adjustments."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        device_id = request.data.get('device_id')
        movements = request.data.get('movements', [])
        
        if not device_id:
            return Response({'error': 'device_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        processed_count = 0
        for movement in movements:
            operation_id = movement.get('operation_id') or movement.get('client_id')
            if not operation_id:
                continue
                
            SyncOperation.objects.get_or_create(
                operation_id=operation_id,
                defaults={
                    'device_id': device_id,
                    'entity_type': 'stock_movement',
                    'action': 'CREATE',
                    'payload': movement,
                    'status': 'PENDING'
                }
            )
            processed_count += 1
            
        processor = SyncProcessor(user=request.user)
        results = processor.process_batch(device_id)
            
        return Response({
            'status': 'completed',
            'processed_count': processed_count,
            'results': results
        })
