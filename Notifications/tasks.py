import logging
from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from decimal import Decimal
from datetime import timedelta
from django.core.mail import send_mail

from .models import (
    Notification,
    NotificationLog,
    NotificationType,
    NotificationLevel,
)
from .services import create_notification
from Sales.models import Sale, Layby, LaybyPayment, DailyReconciliation
from Inventory.models import Stock

logger = logging.getLogger(__name__)
User = get_user_model()


def _get_manager_users():
    return User.objects.filter(role=User.MANAGER, is_active=True)


def _has_existing_alert(notification_type, related_object_type, related_object_id):
    return Notification.objects.filter(
        notification_type=notification_type,
        related_object_type=related_object_type,
        related_object_id=str(related_object_id),
    ).exists()


def _dispatch_if_needed(notification):
    """Enqueue delivery; returns False when the notification was suppressed by user preferences."""
    if notification is None:
        return False
    try:
        dispatch_notification_task.delay(notification.id)
    except Exception:
        logger.exception('Failed to enqueue dispatch for notification %s', notification.id)
    return True


def _build_stock_notification_message(stock, notification_type):
    if notification_type == NotificationType.OUT_OF_STOCK:
        return (
            f"{stock.product.name} is out of stock at {stock.location.name}. "
            f"Reorder immediately to avoid lost sales."
        )
    return (
        f"{stock.product.name} has low stock at {stock.location.name}. "
        f"Current quantity is {stock.quantity}, reorder level is {stock.product.reorder_level}."
    )


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def generate_stock_level_alerts(self) -> dict:
    created_count = 0
    stocks = Stock.objects.select_related('product', 'location').all()

    for stock in stocks:
        if stock.quantity <= 0:
            notification_type = NotificationType.OUT_OF_STOCK
        elif stock.quantity <= stock.product.reorder_level:
            notification_type = NotificationType.LOW_STOCK
        else:
            continue

        if _has_existing_alert(notification_type, 'Stock', stock.id):
            continue

        message = _build_stock_notification_message(stock, notification_type)
        payload = {
            'stock_id': stock.id,
            'product_id': stock.product.id,
            'product_name': stock.product.name,
            'location_id': stock.location.id if stock.location else None,
            'location_name': stock.location.name if stock.location else None,
            'quantity': stock.quantity,
            'reorder_level': stock.product.reorder_level,
        }

        for manager in _get_manager_users():
            notification = create_notification(
                user=manager,
                notification_type=notification_type,
                title=(
                    f"{stock.product.name} is {'out of stock' if notification_type == NotificationType.OUT_OF_STOCK else 'low on stock'}"
                ),
                message=message,
                level=NotificationLevel.WARNING,
                payload=payload,
                related_object_type='Stock',
                related_object_id=stock.id,
            )
            if _dispatch_if_needed(notification):
                created_count += 1

    return {'success': True, 'notifications_created': created_count}


@shared_task(bind=True, max_retries=1, default_retry_delay=60)
def dispatch_notification_task(self, notification_id: int) -> dict:
    try:
        notification = Notification.objects.get(id=notification_id)
    except Notification.DoesNotExist:
        logger.warning('Notification %s does not exist', notification_id)
        return {'success': False, 'error': 'Notification not found'}

    logs = notification.logs.filter(status='pending')
    for log in logs:
        try:
            if log.channel == 'in_app':
                log.mark_sent('In-app notification stored successfully')
            elif log.channel == 'email':
                if notification.user and notification.user.email:
                    send_mail(
                        subject=notification.title,
                        message=notification.message or '',
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[notification.user.email],
                        fail_silently=False,
                    )
                    log.mark_sent('Email delivered')
                else:
                    log.mark_failed('User has no email address')
            elif log.channel == 'sms':
                log.mark_failed('SMS channel not implemented')
            else:
                log.mark_failed('Unsupported channel')
        except Exception as exc:
            logger.exception('Failed to send notification %s via %s', notification.id, log.channel)
            log.mark_failed(str(exc))
    return {'success': True, 'notification_id': notification_id}


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def daily_sales_summary_alert_task(self, summary_date=None) -> dict:
    if summary_date is None:
        summary_date = timezone.now().date()
    elif isinstance(summary_date, str):
        summary_date = timezone.datetime.strptime(summary_date, '%Y-%m-%d').date()

    sales = Sale.objects.filter(created_at__date=summary_date, status=Sale.COMPLETED)
    cash_sales_total = sum(
        sale.amount_received or Decimal('0.00')
        for sale in sales.filter(payment_method='CASH')
    )
    card_sales_total = sum(
        sale.total_amount
        for sale in sales.filter(payment_method='CARD')
    )
    layby_payments_total = sum(
        payment.amount
        for payment in LaybyPayment.objects.filter(created_at__date=summary_date)
    )
    total_sales = sum(sale.total_amount for sale in sales)
    completed_count = sales.count()
    active_layby_count = Layby.objects.filter(status=Layby.ACTIVE, due_date__gte=summary_date).count()

    message = (
        f"Daily sales summary for {summary_date}: {completed_count} completed sales, "
        f"total sales {total_sales}, cash {cash_sales_total}, card {card_sales_total}, "
        f"layby payments {layby_payments_total}, active laybys {active_layby_count}."
    )
    payload = {
        'date': str(summary_date),
        'completed_sales': completed_count,
        'total_sales': str(total_sales),
        'cash_sales_total': str(cash_sales_total),
        'card_sales_total': str(card_sales_total),
        'layby_payments_total': str(layby_payments_total),
        'active_layby_count': active_layby_count,
    }

    managers = _get_manager_users()
    for manager in managers:
        notification = create_notification(
            user=manager,
            notification_type=NotificationType.DAILY_SALES_SUMMARY,
            title=f"Daily Sales Summary: {summary_date}",
            message=message,
            level=NotificationLevel.INFO,
            payload=payload,
            related_object_type='DailySalesSummary',
            related_object_id=str(summary_date),
        )
        _dispatch_if_needed(notification)

    return {
        'success': True,
        'date': str(summary_date),
        'recipients': managers.count(),
        'completed_sales': completed_count,
    }


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def layby_payment_due_reminder_task(self, reminder_days=None) -> dict:
    reminder_days = int(
        reminder_days if reminder_days is not None else
        getattr(settings, 'NOTIFICATIONS_LAYBY_DUE_REMINDER_DAYS', 3)
    )
    today = timezone.now().date()
    deadline = today + timedelta(days=reminder_days)

    laybys = Layby.objects.filter(
        status=Layby.ACTIVE,
        due_date__gt=today,
        due_date__lte=deadline,
    )

    count = 0
    for layby in laybys:
        if _has_existing_alert(NotificationType.LAYBY_PAYMENT_DUE, 'Layby', layby.id):
            continue

        message = (
            f"Layby {layby.reference} is due on {layby.due_date}. "
            f"Remaining balance is {layby.balance}. Please follow up with the customer."
        )
        payload = {
            'layby_id': layby.id,
            'layby_reference': layby.reference,
            'due_date': str(layby.due_date),
            'balance': str(layby.balance),
        }

        for manager in _get_manager_users():
            notification = create_notification(
                user=manager,
                notification_type=NotificationType.LAYBY_PAYMENT_DUE,
                title=f"Layby payment due: {layby.reference}",
                message=message,
                level=NotificationLevel.WARNING,
                payload=payload,
                related_object_type='Layby',
                related_object_id=layby.id,
            )
            if _dispatch_if_needed(notification):
                count += 1

    return {'success': True, 'reminder_days': reminder_days, 'notifications_created': count}


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def layby_overdue_alert_task(self) -> dict:
    today = timezone.now().date()
    laybys = Layby.objects.filter(
        status=Layby.ACTIVE,
        due_date__lt=today,
    )

    count = 0
    for layby in laybys:
        if _has_existing_alert(NotificationType.LAYBY_OVERDUE, 'Layby', layby.id):
            continue

        message = (
            f"Layby {layby.reference} is overdue since {layby.due_date}. "
            f"Outstanding balance is {layby.balance}. Take immediate follow-up action."
        )
        payload = {
            'layby_id': layby.id,
            'layby_reference': layby.reference,
            'due_date': str(layby.due_date),
            'balance': str(layby.balance),
        }

        for manager in _get_manager_users():
            notification = create_notification(
                user=manager,
                notification_type=NotificationType.LAYBY_OVERDUE,
                title=f"Layby overdue: {layby.reference}",
                message=message,
                level=NotificationLevel.ERROR,
                payload=payload,
                related_object_type='Layby',
                related_object_id=layby.id,
            )
            if _dispatch_if_needed(notification):
                count += 1

    return {'success': True, 'notifications_created': count}


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def cash_mismatch_alert_task(self, threshold=None) -> dict:
    threshold = Decimal(str(
        threshold if threshold is not None else
        getattr(settings, 'NOTIFICATIONS_CASH_MISMATCH_THRESHOLD', '10.00')
    ))
    reconciliations = DailyReconciliation.objects.filter(
        is_locked=True,
        variance__isnull=False,
    )

    count = 0
    for reconciliation in reconciliations:
        if reconciliation.variance is None:
            continue
        if abs(reconciliation.variance) < threshold:
            continue
        if _has_existing_alert(
            NotificationType.CASH_MISMATCH,
            'DailyReconciliation',
            reconciliation.id,
        ):
            continue

        message = (
            f"Cash mismatch detected for {reconciliation.reconciliation_date}: "
            f"variance {reconciliation.variance}. Review the reconciliation and cash drawer."
        )
        payload = {
            'reconciliation_id': reconciliation.id,
            'date': str(reconciliation.reconciliation_date),
            'variance': str(reconciliation.variance),
            'total_expected': str(reconciliation.total_expected),
            'counted_cash': str(reconciliation.counted_cash) if reconciliation.counted_cash is not None else None,
        }

        for manager in _get_manager_users():
            notification = create_notification(
                user=manager,
                notification_type=NotificationType.CASH_MISMATCH,
                title=f"Cash mismatch alert: {reconciliation.reconciliation_date}",
                message=message,
                level=NotificationLevel.ERROR,
                payload=payload,
                related_object_type='DailyReconciliation',
                related_object_id=reconciliation.id,
            )
            if _dispatch_if_needed(notification):
                count += 1

    return {'success': True, 'threshold': str(threshold), 'notifications_created': count}
