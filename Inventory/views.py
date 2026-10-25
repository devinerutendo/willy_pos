from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django.db.models import Count, F, Q, Sum, Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from Core.access import branch_scoped_queryset
from Core.permissions import ScopePermission
from .models import Category, Product, Location, Stock, StockTransaction, StockAdjustment, Supplier, Purchase, PurchaseItem
from .serializers import (
    CategorySerializer, ProductSerializer, ProductVariantCompatibilitySerializer,
    LocationSerializer, StockSerializer, StockTransactionSerializer, StockAdjustmentSerializer,
    SupplierSerializer, PurchaseSerializer, PurchaseItemSerializer, PurchaseCreateUpdateSerializer
)
from django.db import IntegrityError
from django.db.models import ProtectedError

class BaseViewSet(viewsets.ModelViewSet):
    """Base viewset with common functionality for all views."""
    permission_classes = [permissions.IsAuthenticated, ScopePermission]
    permission_scope = 'inventory'

    def _get_save_kwargs(self, serializer, include_created=False):
        model = getattr(getattr(serializer, 'Meta', None), 'model', None)
        if not model:
            return {}

        field_names = {field.name for field in model._meta.get_fields()}
        save_kwargs = {}
        if include_created and 'created_by' in field_names:
            save_kwargs['created_by'] = self.request.user
        if 'updated_by' in field_names:
            save_kwargs['updated_by'] = self.request.user
        if 'branch' in field_names and 'branch' not in serializer.validated_data:
            user_branch = getattr(self.request.user, 'branch', None)
            if user_branch is not None:
                save_kwargs['branch'] = user_branch
        return save_kwargs
    
    def perform_create(self, serializer):
        """Auto-assign created_by and updated_by fields."""
        serializer.save(**self._get_save_kwargs(serializer, include_created=True))
    
    def perform_update(self, serializer):
        """Auto-assign updated_by field."""
        serializer.save(**self._get_save_kwargs(serializer))


class CategoryViewSet(BaseViewSet):
    queryset = Category.objects.all().order_by('name')
    serializer_class = CategorySerializer
    
    def get_queryset(self):
        """Annotate product counts; filter by active state and search text."""
        queryset = super().get_queryset().annotate(
            product_count_annotated=Count('products', distinct=True)
        )
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(name__icontains=search.strip())
        return queryset

    def destroy(self, request, *args, **kwargs):
        """Refuse to delete a category that still has products assigned."""
        category = self.get_object()
        count = category.products.count()
        if count:
            noun = 'product is' if count == 1 else 'products are'
            return Response(
                {
                    'detail': (
                        f'"{category.name}" cannot be deleted because {count} {noun} still assigned to it. '
                        'Reassign the products or deactivate the category instead.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        try:
            self.perform_destroy(category)
        except ProtectedError:
            return Response(
                {'detail': f'"{category.name}" is in use and cannot be deleted. Deactivate it instead.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProductViewSet(BaseViewSet):
    """Product endpoints.

    Inherit permission behaviour from `BaseViewSet` (which applies
    `IsAuthenticated` + `ScopePermission`) so permission checks remain
    consistent across the inventory API.
    """
    serializer_class = ProductSerializer
    permission_scope = 'inventory'
    
    def get_queryset(self):
        """Optimized query with related categories."""
        queryset = Product.objects.select_related('category').order_by('-created_at')
        
        # Filter by category
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category_id=category)
        
        # Filter by active status
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        # Filter by SKU or name (search)
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(sku__icontains=search)
            )
        
        # Filter by reorder level (low stock)
        low_stock = self.request.query_params.get('low_stock')
        if low_stock and low_stock.lower() == 'true':
            queryset = queryset.filter(reorder_level__gt=0)
        
        return queryset

    def create(self, request, *args, **kwargs):
        """Create a product with clearer error mapping for DB constraint failures.

        The base `ModelViewSet.create` already handles serializer validation,
        but wrapping `perform_create` lets us catch DB-level IntegrityErrors
        (unique constraints) and return field-specific messages instead of
        noisy 500s.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            self.perform_create(serializer)
        except IntegrityError as e:
            err = str(e).lower()
            if 'barcode' in err:
                return Response({'barcode': ['This barcode already exists']}, status=status.HTTP_400_BAD_REQUEST)
            if 'sku' in err:
                return Response({'sku': ['This SKU already exists']}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'detail': 'Database integrity error.'}, status=status.HTTP_400_BAD_REQUEST)

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def destroy(self, request, *args, **kwargs):
        """Delete a product, returning a clear error if it has related history."""
        product = self.get_object()
        try:
            self.perform_destroy(product)
        except ProtectedError:
            return Response(
                {
                    'detail': (
                        f'"{product.name}" has stock or transaction history and cannot be deleted. '
                        'Deactivate it instead.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    @action(detail=False, methods=['get'])
    def low_stock(self, request):
        """Get products below reorder level."""
        queryset = self.get_queryset().filter(
            Q(stock__quantity__lt=F('reorder_level')) |
            Q(reorder_level__gt=0)
        ).distinct()
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """Deactivate a product."""
        product = self.get_object()
        product.is_active = False
        product.updated_by = request.user
        product.save()
        return Response(
            {'detail': f'Product {product.name} deactivated successfully.'},
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Activate a product."""
        product = self.get_object()
        product.is_active = True
        product.updated_by = request.user
        product.save()
        return Response(
            {'detail': f'Product {product.name} activated successfully.'},
            status=status.HTTP_200_OK
        )

    @action(detail=False, methods=['get'], url_path='by-barcode')
    def by_barcode(self, request):
        """Look up a product by its barcode or SKU for mobile scanning."""
        from django.db.models import Q
        
        identifier = (request.query_params.get('barcode') or request.query_params.get('sku') or '').strip()
        if not identifier:
            return Response({'detail': 'Barcode or SKU identifier is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Fallback to SKU if the scanner reads a short code or manual entry
        product = self.get_queryset().filter(Q(barcode__iexact=identifier) | Q(sku__iexact=identifier)).first()
        if product is None:
            return Response({'detail': 'No product found for this barcode or SKU.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(product)
        payload = serializer.data

        stock_levels = Stock.objects.filter(product=product).select_related('location', 'location__branch')
        payload['stock'] = [
            {
                'id': stock.id,
                'location_id': stock.location_id,
                'location_name': stock.location.name,
                'branch_id': stock.location.branch_id,
                'branch_name': stock.location.branch.name if stock.location.branch else None,
                'quantity': stock.quantity,
                'reserved_quantity': stock.reserved_quantity,
                'available_stock': stock.available_stock,
            }
            for stock in stock_levels
        ]
        payload['available_stock'] = sum((entry['available_stock'] for entry in payload['stock']), 0)

        return Response(payload)

    @action(detail=False, methods=['get'], url_path='validate-barcode')
    def validate_barcode(self, request):
        """Validate if a barcode is available for a new or existing product."""
        barcode = (request.query_params.get('barcode') or '').strip()
        exclude_id = request.query_params.get('exclude_product_id')
        
        if not barcode:
            return Response({'detail': 'Barcode is required.'}, status=status.HTTP_400_BAD_REQUEST)
            
        queryset = self.get_queryset().filter(barcode__iexact=barcode)
        
        if exclude_id and exclude_id.isdigit():
            queryset = queryset.exclude(id=exclude_id)
            
        is_used = queryset.exists()
        
        return Response({
            'barcode': barcode,
            'is_valid': not is_used,
            'message': 'Barcode is already in use by another product.' if is_used else 'Barcode is available.'
        })

    @action(detail=False, methods=['get'], url_path='sync')
    def sync(self, request):
        """Return a lightweight sync payload for mobile devices."""
        updated_after = request.query_params.get('updated_after')
        cutoff = None
        if updated_after:
            parsed = parse_datetime(updated_after)
            if parsed is None:
                return Response({'detail': 'updated_after must be an ISO 8601 timestamp.'}, status=status.HTTP_400_BAD_REQUEST)
            cutoff = parsed

        queryset = self.get_queryset()
        if cutoff is not None:
            queryset = queryset.filter(updated_at__gte=cutoff)

        stock_tx_queryset = StockTransaction.objects.select_related('product', 'location', 'branch', 'pos_terminal').order_by('-updated_at')
        if cutoff is not None:
            stock_tx_queryset = stock_tx_queryset.filter(updated_at__gte=cutoff)

        products = list(queryset[:1000])
        stock_transactions = list(stock_tx_queryset[:1000])

        return Response({
            'generated_at': timezone.now().isoformat(),
            'updated_after': updated_after,
            'count_products': len(products),
            'count_stock_transactions': len(stock_transactions),
            'products': self.get_serializer(products, many=True).data,
            'stock_transactions': StockTransactionSerializer(stock_transactions, many=True).data,
        })


class ProductVariantViewSet(BaseViewSet):
    """
    Compatibility viewset for frontend that expects ProductVariant endpoints.
    Maps to Product model for backward compatibility.
    """
    serializer_class = ProductVariantCompatibilitySerializer
    
    def get_queryset(self):
        """Optimized query with related categories."""
        queryset = Product.objects.select_related('category').order_by('-created_at')
        
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category_id=category)
        
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(sku__icontains=search)
            )
        
        return queryset


class LocationViewSet(BaseViewSet):
    queryset = Location.objects.select_related('branch').all().order_by('name')
    serializer_class = LocationSerializer
    
    def get_queryset(self):
        """Filter active/inactive locations."""
        queryset = super().get_queryset()
        queryset = branch_scoped_queryset(queryset, self.request.user)
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        return queryset


class StockViewSet(BaseViewSet):
    serializer_class = StockSerializer
    
    def get_queryset(self):
        """Optimized query with related products and locations."""
        queryset = Stock.objects.select_related(
            'product',
            'location',
            'location__branch'
        ).order_by('product__name')
        queryset = branch_scoped_queryset(queryset, self.request.user, 'location__branch')
        
        # Filter by product
        product = self.request.query_params.get('product')
        if product:
            queryset = queryset.filter(product_id=product)
        
        # Filter by location
        location = self.request.query_params.get('location')
        if location:
            queryset = queryset.filter(location_id=location)
        
        # Filter by low stock (available < reorder level)
        low_stock = self.request.query_params.get('low_stock')
        if low_stock and low_stock.lower() == 'true':
            queryset = queryset.filter(
                quantity__lt=F('product__reorder_level')
            )
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def adjust_quantity(self, request, pk=None):
        """Adjust stock quantity with transaction logging."""
        stock = self.get_object()
        new_quantity = request.data.get('quantity')
        reference = request.data.get('reference', '')
        notes = request.data.get('notes', '')
        
        if new_quantity is None:
            raise ValidationError({'quantity': 'Quantity is required.'})
        
        try:
            new_quantity = int(new_quantity)
            if new_quantity < 0:
                raise ValidationError({'quantity': 'Quantity cannot be negative.'})
        except (ValueError, TypeError):
            raise ValidationError({'quantity': 'Quantity must be a valid integer.'})
        
        # Calculate difference and log transaction
        if new_quantity != stock.quantity:
            StockAdjustment.apply_adjustment(
                stock=stock,
                new_quantity=new_quantity,
                user=request.user,
                reason=request.data.get('reason', 'Manual stock count correction'),
                reference=reference,
                notes=notes,
            )
        
        serializer = self.get_serializer(stock)
        return Response(serializer.data)


class StockTransactionViewSet(BaseViewSet):
    serializer_class = StockTransactionSerializer
    
    def get_queryset(self):
        """Optimized query with related products and locations."""
        queryset = StockTransaction.objects.select_related(
            'product',
            'location',
            'branch',
            'pos_terminal',
            'created_by'
        ).order_by('-created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user)
        
        # Filter by product
        product = self.request.query_params.get('product')
        if product:
            queryset = queryset.filter(product_id=product)
        
        # Filter by location
        location = self.request.query_params.get('location')
        if location:
            queryset = queryset.filter(location_id=location)
        
        # Filter by transaction type
        transaction_type = self.request.query_params.get('type')
        if transaction_type:
            queryset = queryset.filter(transaction_type=transaction_type)

        terminal = self.request.query_params.get('pos_terminal')
        if terminal:
            queryset = queryset.filter(pos_terminal_id=terminal)
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            queryset = queryset.filter(created_at__gte=start_date)
        if end_date:
            queryset = queryset.filter(created_at__lte=end_date)
        
        return queryset


class StockAdjustmentViewSet(BaseViewSet):
    serializer_class = StockAdjustmentSerializer
    permission_scope = 'stock_adjustments'

    def get_queryset(self):
        queryset = StockAdjustment.objects.select_related(
            'product', 'location', 'branch', 'pos_terminal', 'created_by'
        ).order_by('-created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user)

        product = self.request.query_params.get('product')
        if product:
            queryset = queryset.filter(product_id=product)

        location = self.request.query_params.get('location')
        if location:
            queryset = queryset.filter(location_id=location)

        return queryset


class SupplierViewSet(BaseViewSet):
    serializer_class = SupplierSerializer
    
    def get_queryset(self):
        """Filter active/inactive suppliers."""
        queryset = Supplier.objects.all().order_by('name')
        
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | 
                Q(email__icontains=search) | 
                Q(phone__icontains=search)
            )
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """Deactivate a supplier."""
        supplier = self.get_object()
        supplier.is_active = False
        supplier.save()
        return Response(
            {'detail': f'Supplier {supplier.name} deactivated successfully.'},
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Activate a supplier."""
        supplier = self.get_object()
        supplier.is_active = True
        supplier.save()
        return Response(
            {'detail': f'Supplier {supplier.name} activated successfully.'},
            status=status.HTTP_200_OK
        )


class PurchaseViewSet(BaseViewSet):
    serializer_class = PurchaseSerializer
    permission_scope = 'purchases'
    
    def get_serializer_class(self):
        """Use different serializer for create/update operations."""
        if self.action in ['create', 'update', 'partial_update']:
            return PurchaseCreateUpdateSerializer
        return PurchaseSerializer

    def create(self, request, *args, **kwargs):
        """Create a purchase; a retry carrying the same Idempotency-Key returns the original.

        Creating a purchase also books stock in, so a retry after a network
        failure must never produce a second purchase.
        """
        key = (request.headers.get('Idempotency-Key') or '').strip()[:64]
        if key:
            existing = Purchase.objects.filter(client_request_id=key, created_by=request.user).first()
            if existing:
                return Response(self._read_data(existing), status=status.HTTP_200_OK)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        save_kwargs = self._get_save_kwargs(serializer, include_created=True)
        if key:
            save_kwargs['client_request_id'] = key
        try:
            purchase = serializer.save(**save_kwargs)
        except IntegrityError:
            existing = Purchase.objects.filter(client_request_id=key, created_by=request.user).first() if key else None
            if existing:
                return Response(self._read_data(existing), status=status.HTTP_200_OK)
            return Response(
                {'reference': ['This purchase reference already exists.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(self._read_data(purchase), status=status.HTTP_201_CREATED)

    def _read_data(self, purchase):
        return PurchaseSerializer(purchase, context=self.get_serializer_context()).data
    
    def get_queryset(self):
        """Optimized query with related suppliers and items."""
        queryset = Purchase.objects.select_related(
            'branch',
            'supplier',
            'created_by',
            'updated_by'
        ).prefetch_related('items__product').order_by('-created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user)
        
        # Filter by supplier
        supplier = self.request.query_params.get('supplier')
        if supplier:
            queryset = queryset.filter(supplier_id=supplier)
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            queryset = queryset.filter(created_at__gte=start_date)
        if end_date:
            queryset = queryset.filter(created_at__lte=end_date)
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def mark_received(self, request, pk=None):
        """Mark all items in purchase as received and update stock."""
        purchase = self.get_object()
        items = PurchaseItem.objects.filter(purchase=purchase)
        
        if not items.exists():
            return Response(
                {'detail': 'No items in this purchase.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        items.update(received_quantity=F('quantity'))
        purchase.status = Purchase.RECEIVED
        purchase.updated_by = request.user
        purchase.save()
        
        # Update stock inventory
        purchase.update_stock(user=request.user)
        
        serializer = self.get_serializer(purchase)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a purchase."""
        purchase = self.get_object()
        if purchase.status == Purchase.CANCELLED:
            return Response(
                {'detail': 'This purchase is already cancelled.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        purchase.status = Purchase.CANCELLED
        purchase.updated_by = request.user
        purchase.save()
        
        serializer = self.get_serializer(purchase)
        return Response(serializer.data)


class PurchaseItemViewSet(BaseViewSet):
    serializer_class = PurchaseItemSerializer
    permission_scope = 'purchases'
    
    def get_queryset(self):
        """Optimized query with related purchases and products."""
        queryset = PurchaseItem.objects.select_related(
            'purchase',
            'product'
        ).order_by('purchase__created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user, 'purchase__branch')
        
        # Filter by purchase
        purchase = self.request.query_params.get('purchase')
        if purchase:
            queryset = queryset.filter(purchase_id=purchase)
        
        # Filter by product
        product = self.request.query_params.get('product')
        if product:
            queryset = queryset.filter(product_id=product)
        
        return queryset
