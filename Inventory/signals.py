import logging

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model

from Notifications.models import Notification, NotificationType, NotificationLevel
from Notifications.services import create_notification
from .models import Stock

logger = logging.getLogger(__name__)
User = get_user_model()


def _get_stock_alert_recipients():
    return User.objects.filter(role=User.MANAGER, is_active=True)


def _has_existing_stock_alert(notification_type, stock):
    return Notification.objects.filter(
        notification_type=notification_type,
        related_object_type='Stock',
        related_object_id=str(stock.id),
    ).exists()


def _build_stock_notification_message(stock, notification_type):
    if notification_type == NotificationType.OUT_OF_STOCK:
        return (
            f"{stock.product.name} is out of stock at {stock.location.name if stock.location else 'the selected location'}. "
            f"Reorder immediately to avoid lost sales."
        )

    return (
        f"{stock.product.name} has low stock at {stock.location.name if stock.location else 'the selected location'}. "
        f"Current quantity is {stock.quantity}, reorder level is {stock.product.reorder_level}."
    )


def _build_stock_notification_title(stock, notification_type):
    if notification_type == NotificationType.OUT_OF_STOCK:
        return f"{stock.product.name} is out of stock"
    return f"{stock.product.name} is low on stock"


@receiver(pre_save, sender=Stock)
def cache_previous_stock_quantity(sender, instance, **kwargs):
    if not instance.pk:
        return

    try:
        previous = Stock.objects.get(pk=instance.pk)
    except Stock.DoesNotExist:
        return

    instance._previous_quantity = previous.quantity


@receiver(post_save, sender=Stock)
def create_stock_alert_on_threshold_change(sender, instance, created, **kwargs):
    old_quantity = getattr(instance, '_previous_quantity', None)
    new_quantity = instance.quantity
    # Guard: F() expressions haven't been resolved yet — skip, the next save will catch it
    if not isinstance(new_quantity, (int, float)):
        return
    reorder_level = instance.product.reorder_level

    if new_quantity <= 0 and (old_quantity is None or old_quantity > 0):
        notification_type = NotificationType.OUT_OF_STOCK
    elif new_quantity <= reorder_level and (old_quantity is None or old_quantity > reorder_level):
        notification_type = NotificationType.LOW_STOCK
    else:
        return

    if _has_existing_stock_alert(notification_type, instance):
        return

    message = _build_stock_notification_message(instance, notification_type)
    payload = {
        'stock_id': instance.id,
        'product_id': instance.product.id,
        'product_name': instance.product.name,
        'location_id': instance.location.id if instance.location else None,
        'location_name': instance.location.name if instance.location else None,
        'quantity': instance.quantity,
        'reorder_level': instance.product.reorder_level,
    }

    for manager in _get_stock_alert_recipients():
        try:
            notification = create_notification(
                user=manager,
                notification_type=notification_type,
                title=_build_stock_notification_title(instance, notification_type),
                message=message,
                level=NotificationLevel.WARNING,
                payload=payload,
                related_object_type='Stock',
                related_object_id=instance.id,
            )
            if notification:
                logger.info('Created stock alert notification %s for stock %s', notification.id, instance.id)
        except Exception:
            logger.exception('Failed to create stock alert for stock %s', instance.id)
