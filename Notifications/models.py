from django.db import models
from django.conf import settings
from django.utils import timezone


class NotificationType(models.TextChoices):
    LOW_STOCK = 'low_stock', 'Low Stock'
    OUT_OF_STOCK = 'out_of_stock', 'Out Of Stock'
    FAILED_PAYMENT = 'failed_payment', 'Failed Payment'
    FAILED_SALE = 'failed_sale', 'Failed Sale'
    RETURN_REFUND = 'return_refund', 'Return/Refund'
    CASH_MISMATCH = 'cash_mismatch', 'Cash Mismatch'
    DAILY_SALES_SUMMARY = 'daily_sales_summary', 'Daily Sales Summary'
    LAYBY_PAYMENT_DUE = 'layby_payment_due', 'Layby Payment Due'
    LAYBY_OVERDUE = 'layby_overdue', 'Layby Overdue'
    LAYBY_FINAL_PAYMENT = 'layby_final_payment', 'Layby Final Payment'
    GENERIC = 'generic', 'Generic'


class NotificationLevel(models.TextChoices):
    INFO = 'info', 'Info'
    WARNING = 'warning', 'Warning'
    ERROR = 'error', 'Error'


class Notification(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='notifications',
    )
    notification_type = models.CharField(max_length=64, choices=NotificationType.choices, default=NotificationType.GENERIC)
    level = models.CharField(max_length=16, choices=NotificationLevel.choices, default=NotificationLevel.INFO)
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True)
    payload = models.JSONField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    related_object_type = models.CharField(max_length=128, null=True, blank=True)
    related_object_id = models.CharField(max_length=128, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'is_read'])]

    def mark_read(self):
        if not self.is_read:
            self.is_read = True
            self.save(update_fields=['is_read'])

    def mark_resolved(self):
        if not self.resolved_at:
            self.resolved_at = timezone.now()
            self.save(update_fields=['resolved_at'])

    def __str__(self):
        return f"{self.get_notification_type_display()} - {self.title}"


class NotificationPreference(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_preferences')
    notification_type = models.CharField(max_length=64, choices=NotificationType.choices, default=NotificationType.GENERIC)
    # channels: list like ['in_app', 'email', 'sms']
    channels = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'notification_type')

    def __str__(self):
        return f"{self.user} - {self.notification_type}"


class NotificationLog(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]
    CHANNEL_CHOICES = [
        ('in_app', 'In App'),
        ('email', 'Email'),
        ('sms', 'SMS'),
    ]

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name='logs')
    channel = models.CharField(max_length=32, choices=CHANNEL_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    attempted_at = models.DateTimeField(auto_now_add=True)
    response = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ['-attempted_at']

    def mark_sent(self, response=None):
        self.status = 'sent'
        self.response = response
        self.save(update_fields=['status', 'response'])

    def mark_failed(self, response=None):
        self.status = 'failed'
        self.response = response
        self.save(update_fields=['status', 'response'])

    def __str__(self):
        return f"{self.notification} via {self.channel} [{self.status}]"
