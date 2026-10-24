"""
Sales tasks - Celery async tasks for inventory operations.
Handles asynchronous inventory deduction and verification for completed sales.
"""

import logging
from celery import shared_task
from django.db import transaction
from .models import Sale
from Inventory.models import Stock, Location

logger = logging.getLogger(__name__)


def _complete_sale_inventory(sale_id: int) -> dict:
    logger.info(f"Starting inventory completion for sale ID: {sale_id}")

    try:
        sale = Sale.objects.get(id=sale_id)
        logger.debug(f"Fetched sale {sale.reference} (ID: {sale.id})")

        location = Location.objects.filter(is_active=True).first()
        if not location:
            location, _ = Location.objects.get_or_create(
                name="Main Warehouse",
                defaults={"is_active": True}
            )

        logger.debug(f"Using location: {location.name} (ID: {location.id})")

        with transaction.atomic():
            deducted_items = []
            total_value = 0

            for sale_item in sale.items.all():
                product_id = sale_item.product.id
                product_name = sale_item.product.name
                quantity = sale_item.quantity
                unit_price = sale_item.unit_price
                line_total = quantity * unit_price

                logger.debug(f"Deducting {quantity} units of {product_name} @ ${unit_price}")

                try:
                    stock = Stock.objects.select_for_update().get(
                        product=sale_item.product,
                        location=location
                    )
                except Stock.DoesNotExist:
                    logger.warning(
                        f"Stock record missing for product {product_name} during completion. Creating..."
                    )
                    stock = Stock.objects.create(
                        product=sale_item.product,
                        location=location,
                        quantity=0,
                        reserved_quantity=0
                    )

                prev_qty = stock.quantity
                prev_reserved = stock.reserved_quantity

                stock.quantity = max(0, stock.quantity - quantity)
                stock.reserved_quantity = max(0, stock.reserved_quantity - quantity)

                stock.full_clean()
                stock.save(update_fields=["quantity", "reserved_quantity"])

                # Emit notifications when stock crosses alert thresholds
                try:
                    from django.contrib.auth import get_user_model
                    from Notifications.services import create_notification
                    from Notifications.models import NotificationType, NotificationLevel
                except Exception:
                    logger.exception("Failed to import notification helpers; skipping alerts")
                else:
                    try:
                        User = get_user_model()
                        managers = list(User.objects.filter(role=User.MANAGER, is_active=True))

                        # also notify the salesperson who processed the sale
                        recipients = {m.id: m for m in managers}
                        if hasattr(sale, 'sold_by') and sale.sold_by and sale.sold_by.is_active:
                            recipients[sale.sold_by.id] = sale.sold_by
                        recipients = list(recipients.values())

                        # Out of stock: just reached zero
                        if prev_qty > 0 and stock.quantity <= 0:
                            for recipient in recipients:
                                n = create_notification(
                                    user=recipient,
                                    notification_type=NotificationType.OUT_OF_STOCK,
                                    title=f"Out of stock: {product_name}",
                                    message=(
                                        f"{product_name} is now out of stock at {location.name}. "
                                        f"Previous quantity: {prev_qty}."
                                    ),
                                    level=NotificationLevel.WARNING,
                                    payload={
                                        'product_id': product_id,
                                        'product_name': product_name,
                                        'location': location.name,
                                        'stock_before': prev_qty,
                                        'stock_after': stock.quantity,
                                    },
                                    related_object_type='Product',
                                    related_object_id=product_id,
                                )
                                if n is None:
                                    continue
                                try:
                                    # lazy import dispatch task to avoid circular imports
                                    from Notifications.tasks import dispatch_notification_task
                                    dispatch_notification_task.delay(n.id)
                                except Exception:
                                    logger.exception('Failed to enqueue dispatch for out-of-stock notification %s', getattr(n, 'id', None))

                        # Low stock: crossed below reorder level
                        elif prev_qty > getattr(sale_item.product, 'reorder_level', 0) and stock.quantity <= getattr(sale_item.product, 'reorder_level', 0):
                            for recipient in recipients:
                                n = create_notification(
                                    user=recipient,
                                    notification_type=NotificationType.LOW_STOCK,
                                    title=f"Low stock: {product_name}",
                                    message=(
                                        f"{product_name} has reached low stock ({stock.quantity}) at {location.name}. "
                                        f"Reorder level: {sale_item.product.reorder_level}."
                                    ),
                                    level=NotificationLevel.WARNING,
                                    payload={
                                        'product_id': product_id,
                                        'product_name': product_name,
                                        'location': location.name,
                                        'stock_before': prev_qty,
                                        'stock_after': stock.quantity,
                                    },
                                    related_object_type='Product',
                                    related_object_id=product_id,
                                )
                                if n is None:
                                    continue
                                try:
                                    from Notifications.tasks import dispatch_notification_task
                                    dispatch_notification_task.delay(n.id)
                                except Exception:
                                    logger.exception('Failed to enqueue dispatch for low-stock notification %s', getattr(n, 'id', None))
                    except Exception:
                        logger.exception('Error while creating stock notifications')

                deducted_items.append({
                    "product_id": product_id,
                    "product_name": product_name,
                    "quantity_deducted": quantity,
                    "unit_price": str(unit_price),
                    "line_total": str(line_total),
                    "stock_before": prev_qty,
                    "stock_after": stock.quantity,
                    "reserved_before": prev_reserved,
                    "reserved_after": stock.reserved_quantity,
                })

                total_value += line_total
                logger.debug(
                    f"Successfully deducted {quantity} units of {product_name}. "
                    f"Stock: {prev_qty} → {stock.quantity}"
                )

            sale.inventory_completed = True
            sale.save(update_fields=["inventory_completed"])

            logger.info(
                f"Inventory deduction completed for sale {sale.reference} (ID: {sale.id}). "
                f"Items: {len(deducted_items)}, Total Value: ${total_value}"
            )

            return {
                "success": True,
                "sale_id": sale_id,
                "sale_reference": sale.reference,
                "items_deducted": len(deducted_items),
                "total_value": total_value,
                "items": deducted_items,
            }

    except Sale.DoesNotExist:
        logger.error(f"Sale ID {sale_id} not found")
        return {
            "success": False,
            "error": f"Sale {sale_id} not found",
            "sale_id": sale_id,
        }

    except Exception as e:
        logger.exception(f"Error in inventory completion for sale {sale_id}: {str(e)}")
        raise


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=2,
    time_limit=300  # 5 minutes hard limit
)
def complete_sale_inventory_task(self, sale_id: int) -> dict:
    try:
        return _complete_sale_inventory(sale_id)
    except Exception as e:
        logger.exception(f"Async task error for sale {sale_id}: {str(e)}")
        try:
            raise self.retry(exc=e)
        except self.MaxRetriesExceededError:
            logger.error(f"Max retries exceeded for sale inventory completion: sale {sale_id}")
            return {
                "success": False,
                "error": f"Failed after 3 attempts: {str(e)}",
                "sale_id": sale_id,
            }


@shared_task(
    bind=True,
    max_retries=1,
    default_retry_delay=5,
    time_limit=600  # 10 minutes hard limit
)
def generate_daily_reconciliation_task(self, user_id: int, reconciliation_date=None) -> dict:
    """
    Async task to generate end-of-day reconciliation report.
    
    Aggregates all sales and layby payments for the day by payment method.
    Creates DailyReconciliation record for manager verification.
    
    This task runs automatically at 11:55 PM or can be triggered manually.
    
    Args:
        user_id: ID of user who prepared the reconciliation
        reconciliation_date: Date string (YYYY-MM-DD) or None for today
        
    Returns:
        Dictionary with reconciliation details and totals
    """
    from django.utils import timezone
    from django.db.models import Sum, Q
    from decimal import Decimal
    from Accounts.models import User
    from .models import Sale, LaybyPayment, DailyReconciliation
    
    if reconciliation_date is None:
        reconciliation_date = timezone.now().date()
    else:
        from datetime import datetime
        reconciliation_date = datetime.strptime(reconciliation_date, '%Y-%m-%d').date()
    
    logger.info(
        f"Starting daily reconciliation for {reconciliation_date} by user ID {user_id}"
    )
    
    try:
        # Fetch user
        user = User.objects.get(id=user_id)
        logger.debug(f"Fetched user: {user.get_full_name()} ({user.username})")
        
        # Get all completed sales for the day
        sales_today = Sale.objects.filter(
            created_at__date=reconciliation_date,
            status=Sale.COMPLETED
        )
        
        # Get all layby payments for the day
        layby_payments_today = LaybyPayment.objects.filter(
            created_at__date=reconciliation_date
        )
        
        logger.debug(
            f"Found {sales_today.count()} completed sales and "
            f"{layby_payments_today.count()} layby payments for {reconciliation_date}"
        )
        
        # Calculate sales totals by payment method
        cash_sales_total = sales_today.filter(
            payment_method='CASH'
        ).aggregate(total=Sum('amount_received'))['total'] or Decimal('0.00')
        
        # For proper calculation, use total_amount property via annotation
        # But since total_amount is a property, we need to calculate from items
        from django.db.models import F, DecimalField
        from django.db.models.functions import Cast
        cash_sales = sales_today.filter(payment_method='CASH')
        cash_sales_total = Decimal('0.00')
        for sale in cash_sales:
            cash_sales_total += sale.total_amount
        
        card_sales_total = Decimal('0.00')
        for sale in sales_today.filter(payment_method='CARD'):
            card_sales_total += sale.total_amount
        
        logger.debug(f"Sales totals - Cash: {cash_sales_total}, Card: {card_sales_total}")
        
        # Calculate layby payment totals by method
        layby_cash = layby_payments_today.filter(
            payment_method='CASH'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        layby_card = layby_payments_today.filter(
            payment_method='CARD'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        layby_cheques = layby_payments_today.filter(
            payment_method='CHEQUE'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        layby_transfers = layby_payments_today.filter(
            payment_method='BANK_TRANSFER'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        logger.debug(
            f"Layby payments - Cash: {layby_cash}, Card: {layby_card}, "
            f"Cheques: {layby_cheques}, Transfers: {layby_transfers}"
        )
        
        # Calculate aggregates
        total_cash_in = Decimal(str(cash_sales_total)) + Decimal(str(layby_cash))
        total_electronics = Decimal(str(card_sales_total)) + Decimal(str(layby_card)) + Decimal(str(layby_transfers))
        total_cheques = Decimal(str(layby_cheques))
        total_expected = total_cash_in + total_electronics + total_cheques
        
        logger.info(
            f"Reconciliation totals for {reconciliation_date}: "
            f"Cash: {total_cash_in}, Electronics: {total_electronics}, "
            f"Cheques: {total_cheques}, Total: {total_expected}"
        )
        
        # Create reconciliation record
        reconciliation, created = DailyReconciliation.objects.get_or_create(
            reconciliation_date=reconciliation_date,
            defaults={
                'prepared_by': user,
                'total_cash_sales': cash_sales_total,
                'total_card_sales': card_sales_total,
                'total_layby_cash': layby_cash,
                'total_layby_card': layby_card,
                'total_layby_cheques': layby_cheques,
                'total_layby_transfers': layby_transfers,
                'total_cash_in': total_cash_in,
                'total_electronics': total_electronics,
                'total_cheques': total_cheques,
                'total_expected': total_expected,
            }
        )
        
        if not created:
            # Update existing reconciliation
            reconciliation.total_cash_sales = cash_sales_total
            reconciliation.total_card_sales = card_sales_total
            reconciliation.total_layby_cash = layby_cash
            reconciliation.total_layby_card = layby_card
            reconciliation.total_layby_cheques = layby_cheques
            reconciliation.total_layby_transfers = layby_transfers
            reconciliation.total_cash_in = total_cash_in
            reconciliation.total_electronics = total_electronics
            reconciliation.total_cheques = total_cheques
            reconciliation.total_expected = total_expected
            reconciliation.save()
        
        logger.info(
            f"Daily reconciliation created/updated for {reconciliation_date} (ID: {reconciliation.id})"
        )
        
        return {
            "success": True,
            "reconciliation_id": reconciliation.id,
            "date": str(reconciliation_date),
            "cash_in": str(total_cash_in),
            "electronics": str(total_electronics),
            "cheques": str(total_cheques),
            "total_expected": str(total_expected),
            "sales_count": sales_today.count(),
            "layby_payments_count": layby_payments_today.count(),
        }
        
    except User.DoesNotExist:
        logger.error(f"User ID {user_id} not found")
        return {
            "success": False,
            "error": f"User {user_id} not found",
        }
        
    except Exception as e:
        logger.exception(f"Error in daily reconciliation task: {str(e)}")
        
        # Retry once on failure
        try:
            self.retry(countdown=5)
        except self.MaxRetriesExceededError:
            logger.error(f"Max retries exceeded for daily reconciliation: {str(e)}")
            return {
                "success": False,
                "error": f"Failed after 2 attempts: {str(e)}",
            }
