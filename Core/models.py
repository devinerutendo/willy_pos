from django.db import models
from django.conf import settings

User = settings.AUTH_USER_MODEL


class BaseModel(models.Model):
    """
    Abstract base model providing audit trail and timestamp tracking.
    All business entities should inherit from this for consistency.
    """
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when record was created"
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when record was last modified"
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='%(class)s_created',
        help_text="User who created this record"
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='%(class)s_updated',
        help_text="User who last modified this record"
    )

    class Meta:
        abstract = True


import uuid

class BaseSyncModel(BaseModel):
    """
    Abstract base model for entities that can be created offline and synced.
    """
    client_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        db_index=True,
        help_text="Client-side generated UUID for offline creation and idempotency"
    )
    device_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Identifier of the device that created this record"
    )
    source_device = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Where this record originated (e.g. 'web', 'mobile-pos')"
    )
    is_synced = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether this record has been confirmed with the central server"
    )

    class Meta:
        abstract = True


class Branch(BaseModel):
    """Operational branch used to partition users, stock locations, and POS activity."""

    name = models.CharField(max_length=120, unique=True, db_index=True)
    code = models.CharField(max_length=20, unique=True, db_index=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code})"


class POSTerminal(BaseModel):
    """A named POS station within a branch."""

    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='pos_terminals'
    )
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=40, unique=True, db_index=True)
    description = models.TextField(blank=True)
    assigned_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_pos_terminals'
    )
    is_active = models.BooleanField(default=True, db_index=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['branch__name', 'name']
        constraints = [
            models.UniqueConstraint(fields=['branch', 'name'], name='unique_terminal_name_per_branch'),
        ]

    def __str__(self):
        return f"{self.branch.code} - {self.name}"


class AuditLog(models.Model):
    """Immutable application-level audit trail for entity changes."""

    CREATE = 'CREATE'
    UPDATE = 'UPDATE'
    DELETE = 'DELETE'

    ACTION_CHOICES = (
        (CREATE, 'Create'),
        (UPDATE, 'Update'),
        (DELETE, 'Delete'),
    )

    action = models.CharField(max_length=10, choices=ACTION_CHOICES, db_index=True)
    model_label = models.CharField(max_length=120, db_index=True)
    object_pk = models.CharField(max_length=64, db_index=True)
    object_repr = models.CharField(max_length=255)
    changes = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_entries'
    )
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_entries'
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['model_label', 'object_pk']),
            models.Index(fields=['branch', 'created_at']),
        ]

    def __str__(self):
        return f"{self.action} {self.model_label}#{self.object_pk}"
