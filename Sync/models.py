import uuid
from django.db import models

class SyncOperation(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    ]

    device_id = models.CharField(max_length=100, db_index=True)
    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    entity_type = models.CharField(max_length=50, db_index=True)
    action = models.CharField(max_length=20)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    attempt_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.action} {self.entity_type} - {self.status}"
