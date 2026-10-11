"""
Sales signal handlers for inventory management.
Automatically manages stock levels when sales are created or completed.

This module provides signal handlers to synchronize inventory levels with
sales operations. All operations are logged for auditing and debugging purposes.

STOCK RELEASE (Rollback) Strategy:
===================================
When inventory operations fail (reservation, completion, or release):
1. All database changes inside transaction.atomic() blocks are automatically rolled back
2. Reserved quantities are NOT persisted if an exception occurs
3. ValueError (stock insufficient) and IntegrityError are caught and logged
4. The sale remains unchanged in the database
5. Frontend receives error via API validation or signal failure

Example:
- _reserve_inventory() creates Stock.select_for_update() record lock
- If stock insufficient: raises ValueError
- Transaction.atomic() catches exception and ROLLBACKS all changes
- No cleanup needed - database enforces atomicity
"""

import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db import transaction, IntegrityError
from django.core.exceptions import ValidationError
from Inventory.models import Stock, Location, StockTransaction
from .models import Sale, SaleItem, LaybyPayment
from Notifications.services import create_notification
from Notifications.models import NotificationType, NotificationLevel

# Configure logger for this module
logger = logging.getLogger(__name__)


@receiver(post_save, sender=Sale)
def handle_sale_status_change(sender, instance, created, **kwargs):
    """
    Handle inventory changes when sale status changes.
    
    Signal flow:
    - PENDING: Reserve inventory for upcoming transaction
    - COMPLETED: Deduct inventory and audit trail
    - CANCELLED: Release reserved inventory back to available stock
    
    Args:
        sender: Model class (Sale)
        instance: Sale instance being saved
        created: Boolean indicating if this is a new record
        kwargs: Additional signal arguments
    """
    sale_id = instance.id
    sale_ref = instance.reference
    old_status = kwargs.get('old_status', 'UNKNOWN')
    new_status = instance.status
    
    logger.debug(
        f"Sale signal triggered: {sale_ref} | ID: {sale_id} | "
        f"Status: {old_status} -> {new_status} | Created: {created}"
    )
    
    # Acquire the correct location scoped to the sale's branch.
    # Passing the branch prevents stock from being deducted from the wrong
    # warehouse in a multi-branch deployment.
    location = _get_or_create_location(branch=instance.branch)
    if not location:
        logger.error(
            f"Failed to acquire location for sale {sale_ref} (ID: {sale_id}). "
            "Inventory operations cannot proceed."
        )
        return
    
    logger.debug(
        f"Location acquired for sale {sale_ref}: {location.name} (ID: {location.id})"
    )
    
    # Route to appropriate inventory handler based on status
    try:
        if instance.status == Sale.PENDING and created:
            logger.info(
                f"Initiating inventory reservation for pending sale {sale_ref} (ID: {sale_id})",
                extra={"sale_id": sale_id, "sale_ref": sale_ref, "action": "reserve"}
            )
            _reserve_inventory(instance, location)
            
        elif instance.status == Sale.COMPLETED:
            # NOTE: Inventory deduction for COMPLETED sales is handled by the async task
            # (complete_sale_inventory_task in tasks.py) to avoid double deduction.
            # The signal should NOT call _complete_sale_inventory here.
            logger.info(
                f"Sale {sale_ref} (ID: {sale_id}) marked COMPLETED. "
                f"Inventory deduction will be processed by async task.",
                extra={"sale_id": sale_id, "sale_ref": sale_ref, "action": "complete_deferred"}
            )
            
        elif instance.status == Sale.CANCELLED:
            logger.info(
                f"Initiating inventory release for cancelled sale {sale_ref} (ID: {sale_id})",
                extra={"sale_id": sale_id, "sale_ref": sale_ref, "action": "cancel"}
            )
            _release_reserved_inventory(instance, location)
    except ValueError as stock_error:
        # ✅ Stock validation failed - inventory was NOT reserved
        logger.warning(
            f"Stock reservation failed for sale {sale_ref} (ID: {sale_id}): {stock_error}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref, "error": str(stock_error)}
        )
        # Transaction automatically rolled back - no cleanup needed
        # Sale remains in PENDING status but with NO reserved inventory
    except Exception as e:
        logger.exception(
            f"Unexpected error processing sale signal for {sale_ref} (ID: {sale_id})",
            extra={"sale_id": sale_id, "sale_ref": sale_ref, "error_type": type(e).__name__}
        )


def _get_or_create_location(branch=None):
    """
    Acquire the best-fit active stock location for inventory operations.

    Resolution order
    ----------------
    1. First active location linked to *branch* (branch-scoped lookup).
    2. If no branch-specific location exists, create a "Main Warehouse" for
       that branch so every branch owns its own default location.
    3. If *branch* is None (e.g. legacy walk-in sale), fall back to any
       active location in the system, then create a global "Main Warehouse".

    Parameters
    ----------
    branch : Branch | None
        The branch that owns this sale.  Pass ``None`` only for branch-less
        scenarios; in all other cases pass the sale's branch so inventory
        is never deducted from the wrong warehouse.

    Returns
    -------
    Location | None
        The resolved location, or ``None`` when an unrecoverable DB error
        occurs.
    """
    branch_label = f"branch '{branch.name}' (ID: {branch.id})" if branch else "(no branch)"
    logger.debug(f"Resolving inventory location for {branch_label}")

    try:
        # ── 1. Branch-scoped lookup ──────────────────────────────────────────
        if branch is not None:
            location = Location.objects.filter(
                branch=branch, is_active=True
            ).order_by('id').first()

            if location:
                logger.debug(
                    f"Using branch-scoped location: {location.name} (ID: {location.id}) "
                    f"for {branch_label}"
                )
                return location

            # No location for this branch yet — create the branch's own default.
            logger.warning(
                f"No active location found for {branch_label}. "
                "Creating branch-specific 'Main Warehouse'."
            )
            location, created = Location.objects.get_or_create(
                branch=branch,
                name="Main Warehouse",
                defaults={"is_active": True},
            )
            if created:
                logger.info(
                    f"Created default location 'Main Warehouse' for {branch_label} "
                    f"(ID: {location.id})"
                )
            else:
                logger.debug(
                    f"Default location already exists for {branch_label}: "
                    f"{location.name} (ID: {location.id})"
                )
            return location

        # ── 2. Unscoped fallback (no branch) ────────────────────────────────
        logger.debug("No branch provided — falling back to global location lookup")
        location = Location.objects.filter(is_active=True).order_by('id').first()
        if location:
            logger.debug(f"Using global location: {location.name} (ID: {location.id})")
            return location

        logger.warning(
            "No active locations found anywhere. Creating global 'Main Warehouse'."
        )
        location, created = Location.objects.get_or_create(
            name="Main Warehouse",
            branch=None,
            defaults={"is_active": True},
        )
        if created:
            logger.info(
                f"Created global default location: {location.name} (ID: {location.id})"
            )
        return location

    except IntegrityError as e:
        logger.error(
            f"DB integrity error resolving location for {branch_label}: {e}",
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.exception(
            f"Unexpected error resolving location for {branch_label}: "
            f"{type(e).__name__}: {e}"
        )
        return None


def _reserve_inventory(sale, location):
    """
    Reserve inventory quantities for a pending sale.
    
    This operation locks stock rows and increments reserved_quantity to prevent
    the stock from being allocated to other sales.
    
    Args:
        sale: Sale instance with items to reserve
        location: Location where inventory is reserved
        
    Raises:
        ValueError: If insufficient stock is available
    """
    sale_id = sale.id
    sale_ref = sale.reference
    items_count = sale.items.count()
    
    logger.debug(f"Starting inventory reservation for sale {sale_ref} (ID: {sale_id}) with {items_count} items")
    
    try:
        with transaction.atomic():
            item_results = []
            
            for item in sale.items.all():
                product_id = item.product.id
                product_name = item.product.name
                requested_qty = item.quantity
                
                logger.debug(
                    f"Processing reservation for product: {product_name} (ID: {product_id}) | Qty: {requested_qty}"
                )
                
                # Acquire or create stock record with row-level lock
                stock, created = Stock.objects.select_for_update().get_or_create(
                    product=item.product,
                    location=location,
                    defaults={"quantity": 0, "reserved_quantity": 0}
                )
                
                if created:
                    logger.warning(
                        f"Stock record created during reservation for product {product_name} (ID: {product_id}). "
                        "Physical quantity initialized to 0."
                    )
                
                # Validate sufficient stock availability
                if stock.available_stock < requested_qty:
                    error_msg = (
                        f"Insufficient stock for {product_name} (ID: {product_id}). "
                        f"Available: {stock.available_stock}, Requested: {requested_qty}, "
                        f"Physical: {stock.quantity}, Reserved: {stock.reserved_quantity}"
                    )
                    logger.warning(error_msg, extra={"product_id": product_id, "sale_id": sale_id})
                    raise ValueError(error_msg)
                
                # Reserve the quantity
                previous_reserved = stock.reserved_quantity
                stock.reserved_quantity += requested_qty
                stock.save(update_fields=['reserved_quantity'])
                
                item_results.append({
                    "product_id": product_id,
                    "product_name": product_name,
                    "quantity": requested_qty,
                    "previous_reserved": previous_reserved,
                    "new_reserved": stock.reserved_quantity,
                    "available_after": stock.available_stock
                })
                
                logger.debug(
                    f"Successfully reserved {requested_qty} units of {product_name} | "
                    f"Reserved: {previous_reserved} -> {stock.reserved_quantity}"
                )
        
        logger.info(
            f"Successfully reserved inventory for sale {sale_ref} (ID: {sale_id}) | Items: {len(item_results)}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref, "items_reserved": item_results}
        )
        
    except ValueError as e:
        logger.error(
            f"Stock validation failure during reservation for sale {sale_ref} (ID: {sale_id}): {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref},
            exc_info=True
        )
        # ✅ STOCK AUTOMATICALLY ROLLED BACK by transaction.atomic()
        # All reserved_quantity increments are reverted in memory
        logger.debug(
            f"Transaction rolled back - reserved quantities released for sale {sale_ref} (ID: {sale_id})"
        )
    except IntegrityError as e:
        logger.error(
            f"Database integrity error during reservation for sale {sale_ref} (ID: {sale_id}): {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref},
            exc_info=True
        )
        # ✅ STOCK AUTOMATICALLY ROLLED BACK by transaction.atomic()
        logger.debug(
            f"Transaction rolled back - reserved quantities released for sale {sale_ref} (ID: {sale_id})"
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error reserving inventory for sale {sale_ref} (ID: {sale_id}): "
            f"{type(e).__name__}: {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref}
        )
        # ✅ STOCK AUTOMATICALLY ROLLED BACK by transaction.atomic()
        logger.debug(
            f"Transaction rolled back - reserved quantities released for sale {sale_ref} (ID: {sale_id})"
        )


def _complete_sale_inventory(sale, location):
    """
    Deduct actual inventory when sale is completed.
    
    This operation:
    1. Decrements physical stock quantity
    2. Releases the reserved quantity
    3. Creates audit trail for accounting
    
    Args:
        sale: Completed Sale instance with items to deduct
        location: Location from which to deduct inventory
    """
    sale_id = sale.id
    sale_ref = sale.reference
    items_count = sale.items.count()
    
    logger.debug(
        f"Starting inventory completion for sale {sale_ref} (ID: {sale_id}) with {items_count} items"
    )
    
    try:
        with transaction.atomic():
            item_results = []
            total_deducted_value = 0
            
            for item in sale.items.all():
                product_id = item.product.id
                product_name = item.product.name
                quantity = item.quantity
                unit_price = item.unit_price
                line_total = quantity * unit_price
                
                logger.debug(
                    f"Processing completion for product: {product_name} (ID: {product_id}) | "
                    f"Qty: {quantity} @ ${unit_price} = ${line_total}"
                )
                
                # Acquire or initialize stock record
                try:
                    stock = Stock.objects.select_for_update().get(
                        product=item.product,
                        location=location
                    )
                    stock_existed = True
                except Stock.DoesNotExist:
                    logger.warning(
                        f"Stock record missing for product {product_name} (ID: {product_id}) at "
                        f"location {location.name} during completion of sale {sale_ref}. Creating new record."
                    )
                    stock = Stock.objects.create(
                        product=item.product,
                        location=location,
                        quantity=0,
                        reserved_quantity=0
                    )
                    stock_existed = False
                
                # Record pre-completion state for audit
                prev_quantity = stock.quantity
                prev_reserved = stock.reserved_quantity
                
                # Deduct from physical inventory
                stock.quantity -= quantity
                if stock.quantity < 0:
                    logger.warning(
                        f"Stock going negative for {product_name} (ID: {product_id}). "
                        f"Previous: {prev_quantity}, Deducting: {quantity}, "
                        f"Would be: {stock.quantity}. Setting to 0."
                    )
                    stock.quantity = 0
                
                # Release the reserved quantity
                stock.reserved_quantity = max(0, stock.reserved_quantity - quantity)
                
                # Validate constraints before saving
                try:
                    stock.full_clean()
                except Exception as validation_error:
                    logger.error(
                        f"Stock validation failed for {product_name} (ID: {product_id}): "
                        f"{validation_error}",
                        extra={"product_id": product_id, "sale_id": sale_id}
                    )
                    raise
                
                stock.save(update_fields=['quantity', 'reserved_quantity'])
                
                item_results.append({
                    "product_id": product_id,
                    "product_name": product_name,
                    "quantity_deducted": quantity,
                    "unit_price": str(unit_price),
                    "line_total": str(line_total),
                    "stock_before": prev_quantity,
                    "stock_after": stock.quantity,
                    "reserved_before": prev_reserved,
                    "reserved_after": stock.reserved_quantity
                })
                
                total_deducted_value += line_total
                
                logger.debug(
                    f"Successfully completed {quantity} units of {product_name} | "
                    f"Stock: {prev_quantity} -> {stock.quantity} | Reserved: {prev_reserved} -> {stock.reserved_quantity}"
                )
        
        logger.info(
            f"Successfully completed inventory for sale {sale_ref} (ID: {sale_id}) | "
            f"Items: {len(item_results)} | Total Value: ${total_deducted_value}",
            extra={
                "sale_id": sale_id,
                "sale_ref": sale_ref,
                "items_completed": item_results,
                "total_value": str(total_deducted_value)
            }
        )
        
        # Create audit trail entries for compliance and reconciliation
        _create_stock_transactions(sale, location)
        
    except IntegrityError as e:
        logger.error(
            f"Database integrity error during completion for sale {sale_ref} (ID: {sale_id}): {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref},
            exc_info=True
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error completing sale inventory for {sale_ref} (ID: {sale_id}): "
            f"{type(e).__name__}: {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref}
        )


def _create_stock_transactions(sale, location):
    """
    Create audit trail entries for all stock movements in a completed sale.
    
    This function generates StockTransaction records for each sold item,
    providing a complete audit trail for compliance and reconciliation.
    
    Transaction Details:
    - Type: OUT (Stock Out - Sale)
    - Quantity: Negative (represents deduction)
    - Reference: Sale reference number for traceability
    - Notes: Product name, quantity, unit price for context
    
    Args:
        sale: Completed Sale instance
        location: Location where inventory was deducted
    """
    sale_id = sale.id
    sale_ref = sale.reference
    items_count = sale.items.count()
    
    logger.debug(
        f"Starting stock transaction creation for sale {sale_ref} (ID: {sale_id}) "
        f"with {items_count} items"
    )
    
    try:
        with transaction.atomic():
            transaction_count = 0
            total_units_logged = 0
            
            for sale_item in sale.items.all():
                product = sale_item.product
                quantity = sale_item.quantity
                unit_price = sale_item.unit_price
                
                product_id = product.id
                product_name = product.name
                
                logger.debug(
                    f"Creating transaction for product {product_name} (ID: {product_id}) "
                    f"| Quantity: {quantity} | Price: ${unit_price}"
                )
                
                # Build detailed notes for audit trail
                notes = (
                    f"Sale completion | Qty: {quantity} × ${unit_price} = "
                    f"${quantity * unit_price} | Sold via POS"
                )
                
                # Create transaction record
                # Quantity is negative to indicate stock OUT
                stock_transaction = StockTransaction.objects.create(
                    product=product,
                    location=location,
                    branch=sale.branch or location.branch,
                    pos_terminal=sale.pos_terminal,
                    transaction_type=StockTransaction.OUT,
                    quantity=-quantity,  # Negative for stock deduction
                    reference=sale_ref,
                    notes=notes,
                    created_by=sale.updated_by or sale.sold_by,
                    updated_by=sale.updated_by or sale.sold_by,
                )
                
                transaction_count += 1
                total_units_logged += quantity
                
                logger.debug(
                    f"Created transaction record: ID={stock_transaction.id} | "
                    f"Reference={sale_ref} | Product={product_name} | Qty={-quantity}"
                )
            
            logger.info(
                f"Successfully created stock transactions for sale {sale_ref} (ID: {sale_id}) | "
                f"Transactions: {transaction_count} | Total Units: {total_units_logged}",
                extra={
                    "sale_id": sale_id,
                    "sale_ref": sale_ref,
                    "transaction_count": transaction_count,
                    "total_units": total_units_logged,
                    "location_id": location.id
                }
            )
            
    except IntegrityError as e:
        logger.error(
            f"Database integrity error creating transactions for sale {sale_ref} (ID: {sale_id}): {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref},
            exc_info=True
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error creating stock transactions for sale {sale_ref} (ID: {sale_id}): "
            f"{type(e).__name__}: {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref}
        )


def _create_cancellation_transactions(sale, location):
    """
    Create return transaction entries when a sale is cancelled.
    
    Documents inventory being returned to stock after a sale cancellation.
    Completes the audit trail by recording the reversal of the original deduction.
    
    Transaction Details:
    - Type: RETURN (Return - Customer/Supplier)
    - Quantity: Positive (represents inventory return/cancellation reversal)
    - Reference: Sale reference prefixed with CANCELLED_ for traceability
    - Notes: Product name, quantity for context
    
    Args:
        sale: Cancelled Sale instance
        location: Location where inventory was originally deducted
    """
    sale_id = sale.id
    sale_ref = sale.reference
    items_count = sale.items.count()
    
    logger.debug(
        f"Starting cancellation transaction creation for sale {sale_ref} (ID: {sale_id}) "
        f"with {items_count} items"
    )
    
    try:
        with transaction.atomic():
            transaction_count = 0
            total_units_returned = 0
            
            for sale_item in sale.items.all():
                product = sale_item.product
                quantity = sale_item.quantity
                
                product_id = product.id
                product_name = product.name
                
                logger.debug(
                    f"Creating cancellation transaction for product {product_name} "
                    f"(ID: {product_id}) | Return Quantity: {quantity}"
                )
                
                # Build detailed notes for audit trail
                notes = f"Sale cancellation reversal | Qty returned: {quantity}"
                
                # Create RETURN transaction
                # Quantity is positive (reversal of the deduction)
                stock_transaction = StockTransaction.objects.create(
                    product=product,
                    location=location,
                    branch=sale.branch or location.branch,
                    pos_terminal=sale.pos_terminal,
                    transaction_type=StockTransaction.RETURN,
                    quantity=quantity,  # Positive for inventory return
                    reference=f"CANCELLED_{sale_ref}",
                    notes=notes,
                    created_by=sale.updated_by or sale.sold_by,
                    updated_by=sale.updated_by or sale.sold_by,
                )
                
                transaction_count += 1
                total_units_returned += quantity
                
                logger.debug(
                    f"Created cancellation transaction: ID={stock_transaction.id} | "
                    f"Reference=CANCELLED_{sale_ref} | Product={product_name} | Qty={quantity}"
                )
            
            logger.info(
                f"Successfully created cancellation transactions for sale {sale_ref} (ID: {sale_id}) | "
                f"Transactions: {transaction_count} | Total Units Returned: {total_units_returned}",
                extra={
                    "sale_id": sale_id,
                    "sale_ref": sale_ref,
                    "transaction_count": transaction_count,
                    "total_units": total_units_returned,
                    "location_id": location.id
                }
            )
            
    except IntegrityError as e:
        logger.error(
            f"Database integrity error creating cancellation transactions for sale {sale_ref} "
            f"(ID: {sale_id}): {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref},
            exc_info=True
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error creating cancellation transactions for sale {sale_ref} "
            f"(ID: {sale_id}): {type(e).__name__}: {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref}
        )


def _release_reserved_inventory(sale, location):
    """
    Release reserved inventory when sale is cancelled.
    
    This operation restores reserved quantities back to available stock,
    making them available for other sales.
    
    Args:
        sale: Cancelled Sale instance with items to release
        location: Location from which to release inventory
    """
    sale_id = sale.id
    sale_ref = sale.reference
    items_count = sale.items.count()
    
    logger.debug(
        f"Starting inventory release for cancelled sale {sale_ref} (ID: {sale_id}) with {items_count} items"
    )
    
    try:
        with transaction.atomic():
            item_results = []
            
            for item in sale.items.all():
                product_id = item.product.id
                product_name = item.product.name
                quantity_to_release = item.quantity
                
                logger.debug(
                    f"Processing release for product: {product_name} (ID: {product_id}) | "
                    f"Qty to release: {quantity_to_release}"
                )
                
                try:
                    stock = Stock.objects.select_for_update().get(
                        product=item.product,
                        location=location
                    )
                    
                    previous_reserved = stock.reserved_quantity
                    stock.reserved_quantity = max(0, stock.reserved_quantity - quantity_to_release)
                    
                    stock.save(update_fields=['reserved_quantity'])
                    
                    item_results.append({
                        "product_id": product_id,
                        "product_name": product_name,
                        "quantity_released": quantity_to_release,
                        "reserved_before": previous_reserved,
                        "reserved_after": stock.reserved_quantity,
                        "available_now": stock.available_stock
                    })
                    
                    logger.debug(
                        f"Successfully released {quantity_to_release} units of {product_name} | "
                        f"Reserved: {previous_reserved} -> {stock.reserved_quantity}"
                    )
                    
                except Stock.DoesNotExist:
                    logger.warning(
                        f"Stock record missing for product {product_name} (ID: {product_id}) at "
                        f"location {location.name} during release of cancelled sale {sale_ref}. "
                        f"No inventory to release.",
                        extra={"product_id": product_id, "sale_id": sale_id}
                    )
        
        logger.info(
            f"Successfully released inventory for cancelled sale {sale_ref} (ID: {sale_id}) | Items: {len(item_results)}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref, "items_released": item_results}
        )
        
        # Create audit trail entries for cancellation
        _create_cancellation_transactions(sale, location)
        
    except IntegrityError as e:
        logger.error(
            f"Database integrity error during release for sale {sale_ref} (ID: {sale_id}): {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref},
            exc_info=True
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error releasing inventory for cancelled sale {sale_ref} (ID: {sale_id}): "
            f"{type(e).__name__}: {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref}
        )




# ============================================================================
# LAYBY PAYMENT SIGNAL HANDLERS
# ============================================================================

@receiver(post_save, sender=LaybyPayment)
def update_layby_status_on_payment(sender, instance, created, **kwargs):
    """
    Automatically mark layby as COMPLETED when fully paid.
    
    When a payment is recorded and the balance becomes zero,
    this signal updates the layby status to COMPLETED.
    
    Args:
        sender: LaybyPayment model class
        instance: The LaybyPayment instance being saved
        created: Boolean indicating if this is a new record
        kwargs: Additional signal arguments
    """
    if not created:
        return
    
    from .models import Layby
    from decimal import Decimal
    
    layby = instance.layby
    layby_id = layby.id
    layby_ref = layby.reference
    
    logger.debug(
        f"Payment recorded for layby {layby_ref} (ID: {layby_id}) | "
        f"Amount: {instance.amount} | Method: {instance.payment_method}"
    )
    
    try:
        # Recalculate total paid (sum of all payments)
        total_paid = sum(
            Decimal(str(payment.amount))
            for payment in layby.payments.all()
        )
        
        balance = Decimal(str(layby.total_amount)) - total_paid
        
        logger.debug(
            f"Layby {layby_ref} (ID: {layby_id}) | "
            f"Total Amount: {layby.total_amount} | Total Paid: {total_paid} | "
            f"Balance: {balance}"
        )
        
        # Check if layby is now fully paid
        if balance <= 0:  # <= handles edge case of overpayment
            logger.info(
                f"Layby {layby_ref} (ID: {layby_id}) is now FULLY PAID | Balance: {balance}",
                extra={"layby_id": layby_id, "layby_ref": layby_ref, "balance": str(balance)}
            )
            
            # Update layby status
            layby.status = Layby.COMPLETED
            layby.updated_by = instance.created_by
            layby.save(update_fields=['status', 'updated_by'])
            
            logger.info(
                f"Layby {layby_ref} (ID: {layby_id}) status updated to COMPLETED",
                extra={"layby_id": layby_id, "layby_ref": layby_ref}
            )
            
            create_notification(
                user=instance.created_by,
                notification_type=NotificationType.LAYBY_FINAL_PAYMENT,
                title=f"Layby {layby_ref} completed",
                message=(
                    f"The layby payment for {layby_ref} is now complete. "
                    "Prepare the order for pickup and notify the customer."
                ),
                level=NotificationLevel.INFO,
                payload={
                    'layby_id': layby_id,
                    'layby_reference': layby_ref,
                    'balance': str(balance),
                },
                related_object_type='Layby',
                related_object_id=layby_id,
            )
        else:
            logger.debug(
                f"Layby {layby_ref} (ID: {layby_id}) still has balance: {balance}",
                extra={"layby_id": layby_id, "layby_ref": layby_ref, "balance": str(balance)}
            )
            
    except Exception as e:
        logger.exception(
            f"Error updating layby status after payment for {layby_ref} (ID: {layby_id}): "
            f"{type(e).__name__}: {e}",
            extra={"layby_id": layby_id, "layby_ref": layby_ref}
        )


@receiver(post_save, sender=Sale)
def validate_layby_sale_relationship(sender, instance, created, **kwargs):
    """
    Validate Sale-Layby relationship when sale status changes.
    
    Ensures:
    - If sale completing a layby, payment_method must be 'LAYBY'
    - If payment_method='LAYBY', layby must be specified and fully paid
    - Regular sales cannot have both payment_method='LAYBY' and payment_method=CASH/CARD
    
    Args:
        sender: Sale model class
        instance: The Sale instance being saved
        created: Boolean indicating if this is a new record
        kwargs: Additional signal arguments
    """
    if created:
        return  # Only validate on updates
    
    sale_id = instance.id
    sale_ref = instance.reference
    
    try:
        # Check if completing a layby sale
        if instance.status == Sale.COMPLETED and instance.payment_method == 'LAYBY':
            if not instance.layby:
                logger.error(
                    f"Sale {sale_ref} (ID: {sale_id}) marked LAYBY_COMPLETED but no layby specified",
                    extra={"sale_id": sale_id, "sale_ref": sale_ref}
                )
                raise ValidationError(
                    "Layby must be specified when payment method is LAYBY"
                )
            
            if instance.layby.balance > 0:
                logger.error(
                    f"Sale {sale_ref} (ID: {sale_id}) cannot be completed. Layby {instance.layby.reference} "
                    f"has balance: {instance.layby.balance}",
                    extra={"sale_id": sale_id, "sale_ref": sale_ref, "layby_balance": str(instance.layby.balance)}
                )
                raise ValidationError(
                    f"Cannot complete sale. Layby balance remaining: {instance.layby.balance}"
                )
            
            logger.info(
                f"Sale {sale_ref} (ID: {sale_id}) validated as LAYBY completion | Layby: {instance.layby.reference}",
                extra={"sale_id": sale_id, "sale_ref": sale_ref, "layby_ref": instance.layby.reference}
            )
        
        # Check if layby is linked but has wrong payment method
        elif instance.layby and instance.payment_method != 'LAYBY':
            logger.error(
                f"Sale {sale_ref} (ID: {sale_id}) has layby {instance.layby.reference} "
                f"but payment_method={instance.payment_method} (should be LAYBY)",
                extra={"sale_id": sale_id, "sale_ref": sale_ref, "payment_method": instance.payment_method}
            )
            raise ValidationError(
                "Payment method must be 'LAYBY' when layby is specified"
            )
            
    except ValidationError:
        raise
    except Exception as e:
        logger.exception(
            f"Error validating layby-sale relationship for {sale_ref} (ID: {sale_id}): "
            f"{type(e).__name__}: {e}",
            extra={"sale_id": sale_id, "sale_ref": sale_ref}
        )


def ready():
    """Register signal handlers when app is ready."""
    pass

