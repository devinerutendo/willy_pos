from django.contrib import admin
from .models import Notification, NotificationPreference, NotificationLog


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('id', 'notification_type', 'title', 'user', 'level', 'is_read', 'created_at')
    list_filter = ('notification_type', 'level', 'is_read')
    search_fields = ('title', 'message')


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'notification_type', 'enabled')
    search_fields = ('user__email',)


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'notification', 'channel', 'status', 'attempted_at')
    list_filter = ('channel', 'status')
    search_fields = ('notification__title',)
