import logging
from django.conf import settings
from django.db import transaction
from .models import (
    Notification,
    NotificationPreference,
    NotificationLog,
    NotificationLevel,
    NotificationType,
)

logger = logging.getLogger(__name__)

DEFAULT_CHANNELS = ['in_app']


def get_notification_preferences(user):
    if user is None or not user.is_authenticated:
        return {}
    return {
        pref.notification_type: pref
        for pref in NotificationPreference.objects.filter(user=user)
    }


def get_preferred_channels(user, notification_type):
    if user is None or not user.is_authenticated:
        return DEFAULT_CHANNELS

    pref = NotificationPreference.objects.filter(
        user=user,
        notification_type=notification_type,
        enabled=True,
    ).first()
    if pref and pref.channels:
        # The in-app record is the source of truth for history, so it is always kept.
        return list(dict.fromkeys(['in_app', *pref.channels]))
    return DEFAULT_CHANNELS


def create_notification(
    user,
    notification_type,
    title,
    message='',
    level=NotificationLevel.INFO,
    payload=None,
    related_object_type=None,
    related_object_id=None,
    channels=None,
):
    """
    Create a notification and its delivery log rows.

    Returns ``None`` when the recipient has switched this notification type
    off, so callers must handle a missing result.
    """
    if user is not None and not should_emit_alert(user, notification_type):
        return None

    if channels is None:
        channels = get_preferred_channels(user, notification_type)

    if isinstance(notification_type, NotificationType):
        notification_type = notification_type.value

    if isinstance(level, NotificationLevel):
        level = level.value

    with transaction.atomic():
        notification = Notification.objects.create(
            user=user,
            notification_type=notification_type,
            level=level,
            title=title,
            message=message,
            payload=payload or {},
            related_object_type=related_object_type,
            related_object_id=str(related_object_id) if related_object_id is not None else None,
        )

        logs = []
        for channel in channels:
            if channel not in ['in_app', 'email', 'sms']:
                logger.warning(
                    "Skipping unsupported notification channel %s for user %s",
                    channel,
                    getattr(user, 'id', None),
                )
                continue
            logs.append(
                NotificationLog(notification=notification, channel=channel, status='pending')
            )
        NotificationLog.objects.bulk_create(logs)

    logger.info(
        "Created notification %s for user %s via %s",
        notification.id,
        getattr(user, 'id', None),
        ','.join(channels),
    )

    return notification


def create_system_notification(
    notification_type,
    title,
    message='',
    level=NotificationLevel.INFO,
    payload=None,
    related_object_type=None,
    related_object_id=None,
):
    return create_notification(
        user=None,
        notification_type=notification_type,
        title=title,
        message=message,
        level=level,
        payload=payload,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        channels=DEFAULT_CHANNELS,
    )


def should_emit_alert(user, notification_type):
    if user is None or not user.is_authenticated:
        return False
    pref = NotificationPreference.objects.filter(
        user=user,
        notification_type=notification_type,
    ).first()
    return pref.enabled if pref else True


# ---------------------------------------------------------------------------
# User-facing preference settings
#
# The settings screen exposes a handful of switches, while preferences are
# stored per notification type. These helpers translate between the two.
# ---------------------------------------------------------------------------

PREFERENCE_CATEGORIES = {
    'order_alerts': [
        NotificationType.FAILED_SALE,
        NotificationType.RETURN_REFUND,
        NotificationType.CASH_MISMATCH,
    ],
    'stock_alerts': [
        NotificationType.LOW_STOCK,
        NotificationType.OUT_OF_STOCK,
    ],
    'payment_reminders': [
        NotificationType.FAILED_PAYMENT,
        NotificationType.LAYBY_PAYMENT_DUE,
        NotificationType.LAYBY_OVERDUE,
        NotificationType.LAYBY_FINAL_PAYMENT,
    ],
    'daily_report': [
        NotificationType.DAILY_SALES_SUMMARY,
    ],
}

# Categories that are on until the user switches them off.
DEFAULT_CATEGORY_STATE = {
    'order_alerts': True,
    'stock_alerts': True,
    'payment_reminders': True,
    'daily_report': True,
}

OPTIONAL_CHANNELS = ('email', 'sms')


def sms_delivery_available() -> bool:
    return bool(getattr(settings, 'NOTIFICATIONS_SMS_ENABLED', False))


def get_preference_settings(user) -> dict:
    """Collapse stored per-type preferences into the settings-screen shape."""
    prefs = list(NotificationPreference.objects.filter(user=user))
    by_type = {pref.notification_type: pref for pref in prefs}

    categories = {}
    for category, types in PREFERENCE_CATEGORIES.items():
        rows = [by_type[t.value] for t in types if t.value in by_type]
        categories[category] = (
            any(row.enabled for row in rows) if rows else DEFAULT_CATEGORY_STATE[category]
        )

    managed_types = {t.value for types in PREFERENCE_CATEGORIES.values() for t in types}
    managed_rows = [pref for pref in prefs if pref.notification_type in managed_types]
    channels = {
        channel: any(channel in (row.channels or []) for row in managed_rows)
        for channel in OPTIONAL_CHANNELS
    }
    if not sms_delivery_available():
        channels['sms'] = False

    return {
        'categories': categories,
        'channels': channels,
        'capabilities': {'sms': sms_delivery_available()},
    }


@transaction.atomic
def save_preference_settings(user, categories=None, channels=None) -> dict:
    """
    Persist the settings-screen state as one preference row per notification
    type. Omitted keys keep their current value, so partial updates are safe.
    """
    current = get_preference_settings(user)
    next_categories = {**current['categories'], **(categories or {})}
    next_channels = {**current['channels'], **(channels or {})}

    channel_list = ['in_app'] + [c for c in OPTIONAL_CHANNELS if next_channels.get(c)]

    for category, types in PREFERENCE_CATEGORIES.items():
        for notification_type in types:
            NotificationPreference.objects.update_or_create(
                user=user,
                notification_type=notification_type.value,
                defaults={
                    'enabled': bool(next_categories[category]),
                    'channels': channel_list,
                },
            )

    return get_preference_settings(user)
