import json
from threading import local

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from Core.models import AuditLog


_state = local()
TRACKED_APPS = {'Accounts', 'Core', 'Expenses', 'Inventory', 'Notifications', 'Sales'}
IGNORED_FIELDS = {'created_at', 'updated_at', 'last_login'}


def set_current_user(user):
    _state.user = user


def get_current_user():
    return getattr(_state, 'user', None)


def _should_track(sender):
    meta = getattr(sender, '_meta', None)
    if not meta or meta.abstract or not meta.managed:
        return False
    if sender is AuditLog:
        return False
    return meta.app_label in TRACKED_APPS


def _serialize_value(value):
    if hasattr(value, 'pk'):
        return value.pk
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _snapshot(instance):
    snapshot = {}
    for field in instance._meta.concrete_fields:
        if field.name in IGNORED_FIELDS:
            continue
        value = field.value_from_object(instance)
        snapshot[field.name] = _serialize_value(value)
    return snapshot


def _resolve_actor(instance, created):
    actor = get_current_user()
    if actor and getattr(actor, 'is_authenticated', False):
        return actor

    field_name = 'created_by' if created else 'updated_by'
    return getattr(instance, field_name, None)


def _resolve_branch(instance, actor):
    branch = getattr(instance, 'branch', None)
    if branch is not None:
        return branch

    location = getattr(instance, 'location', None)
    if location is not None:
        return getattr(location, 'branch', None)

    return getattr(actor, 'branch', None)


@receiver(pre_save, dispatch_uid='core_audit_pre_save')
def capture_pre_save_state(sender, instance, **kwargs):
    if not _should_track(sender) or not instance.pk:
        return

    try:
        instance._audit_snapshot = _snapshot(sender.objects.get(pk=instance.pk))
    except sender.DoesNotExist:
        instance._audit_snapshot = None


@receiver(post_save, dispatch_uid='core_audit_post_save')
def log_model_save(sender, instance, created, **kwargs):
    if not _should_track(sender):
        return

    previous = {} if created else getattr(instance, '_audit_snapshot', None) or {}
    current = _snapshot(instance)

    if created:
        changes = {
            field: {'from': None, 'to': value}
            for field, value in current.items()
            if value not in (None, '', [], {})
        }
        action = AuditLog.CREATE
    else:
        changes = {}
        for field, new_value in current.items():
            old_value = previous.get(field)
            if old_value != new_value:
                changes[field] = {'from': old_value, 'to': new_value}
        action = AuditLog.UPDATE

    if not changes:
        return

    actor = _resolve_actor(instance, created)
    AuditLog.objects.create(
        action=action,
        model_label=instance._meta.label,
        object_pk=str(instance.pk),
        object_repr=str(instance),
        changes=changes,
        actor=actor if getattr(actor, 'pk', None) else None,
        branch=_resolve_branch(instance, actor),
    )


@receiver(post_delete, dispatch_uid='core_audit_post_delete')
def log_model_delete(sender, instance, **kwargs):
    if not _should_track(sender):
        return

    actor = _resolve_actor(instance, created=False)
    AuditLog.objects.create(
        action=AuditLog.DELETE,
        model_label=instance._meta.label,
        object_pk=str(instance.pk),
        object_repr=str(instance),
        changes={},
        actor=actor if getattr(actor, 'pk', None) else None,
        branch=_resolve_branch(instance, actor),
    )