import logging
from decimal import Decimal
from django.db import transaction, models
from django.utils import timezone
from .models import SyncOperation
from Sales.models import Customer, Sale, SaleItem
from Inventory.models import Product, Stock, StockMovement, Location
from Accounts.models import User

logger = logging.getLogger(__name__)

class SyncProcessor:
    def __init__(self, user: User):
        self.user = user
        self.branch = getattr(user, 'branch', None)
        
        # Find an active location for inventory deduction
        if self.branch:
            self.location = Location.objects.filter(branch=self.branch, is_active=True).first()
        else:
            self.location = Location.objects.filter(is_active=True).first()

    def process_batch(self, device_id: str) -> dict:
        operations = SyncOperation.objects.filter(
            device_id=device_id, status='PENDING'
        ).order_by('created_at')
        
        results = {}
        for op in operations:
            try:
                with transaction.atomic():
                    self._process_operation(op)
                    
                op.status = 'SUCCESS'
                op.synced_at = timezone.now()
                results[str(op.operation_id)] = {'status': 'success'}
                logger.info(f"Successfully processed sync operation {op.operation_id}")
            except Exception as e:
                logger.exception(f"Failed to process sync operation {op.operation_id}")
                op.status = 'FAILED'
                op.error_message = str(e)
                results[str(op.operation_id)] = {'status': 'failed', 'error': str(e)}
            finally:
                op.attempt_count += 1
                op.last_attempt_at = timezone.now()
                op.save()
                
        return results

    def _process_operation(self, op: SyncOperation):
        op.status = 'PROCESSING'
        op.save(update_fields=['status'])
        
        payload = op.payload
        entity_type = op.entity_type.lower()
        
        if entity_type == 'customer':
            self._process_customer(payload, op)
        elif entity_type == 'sale':
            self._process_sale(payload, op)
        else:
            raise ValueError(f"Unknown entity_type: {entity_type}")

    def _process_customer(self, payload: dict, op: SyncOperation):
        client_id = payload.get('client_id')
        if not client_id:
            raise ValueError("Customer payload missing client_id")
            
        Customer.objects.update_or_create(
            client_id=client_id,
            defaults={
                'name': payload.get('name', 'Unknown Customer'),
                'email': payload.get('email', ''),
                'phone': payload.get('phone', ''),
                'address': payload.get('address', ''),
                'city': payload.get('city', ''),
                'device_id': op.device_id,
                'source_device': 'mobile-pos',
                'is_synced': True,
                'created_by': self.user,
                'updated_by': self.user,
            }
        )

    def _process_sale(self, payload: dict, op: SyncOperation):
        client_id = payload.get('client_id')
        if not client_id:
            raise ValueError("Sale payload missing client_id")
            
        customer_client_id = payload.get('customer_client_id')
        customer = None
        if customer_client_id:
            customer = Customer.objects.filter(client_id=customer_client_id).first()
            
        # Safely parse numeric amounts
        try:
            total_amount = Decimal(str(payload.get('amount') or 0))
        except:
            total_amount = Decimal('0.00')
            
        try:
            amount_received = Decimal(str(payload.get('amount_received') or total_amount))
        except:
            amount_received = total_amount
            
        sale, created = Sale.objects.get_or_create(
            client_id=client_id,
            defaults={
                'reference': payload.get('reference', f'SYNC-{str(client_id)[:8].upper()}'),
                'customer': customer,
                'branch': self.branch,
                'sold_by': self.user,
                'amount_received': amount_received,
                'change_amount': amount_received - total_amount if amount_received > total_amount else Decimal('0.00'),
                'payment_method': payload.get('payment_method', 'CASH').upper(),
                'status': 'COMPLETED',
                'payment_status': 'PAID',
                'device_id': op.device_id,
                'source_device': 'mobile-pos',
                'is_synced': True,
                'created_by': self.user,
                'updated_by': self.user,
                'inventory_sync_status': 'SUCCESS',
                'inventory_completed': True
            }
        )
        
        if not created:
            # Idempotency: Sale already exists from a prior successful save but failed overall batch
            return
            
        items = payload.get('items', [])
        for item_data in items:
            sku = item_data.get('product_sku')
            try:
                quantity = int(item_data.get('quantity', 1))
            except:
                quantity = 1
                
            try:
                unit_price = Decimal(str(item_data.get('unit_price', 0)))
            except:
                unit_price = Decimal('0.00')
            
            product = Product.objects.filter(sku=sku).first()
            if not product:
                # Log and skip this line item rather than aborting the entire sale.
                # The cashier already handed the physical goods to the customer offline,
                # so we preserve the sale record and flag the gap for admin review.
                logger.warning(
                    f"Sale {client_id}: SKU '{sku}' not found in product catalogue. "
                    f"Item skipped — manual reconciliation may be required."
                )
                continue
                
            SaleItem.objects.create(
                sale=sale,
                product=product,
                quantity=quantity,
                unit_price=unit_price,
                client_id=item_data.get('client_id') # Will default if None
            )
            
            # Process Stock Deduction
            if self.location:
                stock, _ = Stock.objects.get_or_create(
                    product=product, 
                    location=self.location,
                    defaults={'quantity': 0, 'reserved_quantity': 0}
                )
                
                quantity_before = stock.quantity
                
                # Perform the deduction, clamped to 0 to respect DB constraints
                deduction = quantity if quantity_before >= quantity else quantity_before
                
                stock.quantity = models.F('quantity') - deduction
                stock.save(update_fields=['quantity'])
                stock.refresh_from_db(fields=['quantity'])
                
                # Audit the deduction
                StockMovement.objects.create(
                    product=product,
                    location=self.location,
                    branch=self.branch,
                    quantity_before=quantity_before,
                    quantity_delta=-deduction,
                    quantity_after=stock.quantity,
                    movement_type='SALE',
                    reference_type='Sale',
                    reference_id=str(sale.client_id),
                    created_by=self.user,
                    updated_by=self.user
                )
