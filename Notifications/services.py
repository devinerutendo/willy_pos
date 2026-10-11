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
        return pref.channels
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
