"""
Celery configuration for the Core Django project.
Handles async task processing for inventory operations.
"""

import os
from celery import Celery
from celery.schedules import crontab

# Set default Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Core.settings')

app = Celery('Core')

# Load config from Django settings, prefixed with CELERY
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks from installed apps
app.autodiscover_tasks()

app.conf.beat_schedule = {
    'daily-sales-summary-alert': {
        'task': 'Notifications.tasks.daily_sales_summary_alert_task',
        'schedule': crontab(hour=23, minute=55),
    },
    'layby-payment-due-reminder': {
        'task': 'Notifications.tasks.layby_payment_due_reminder_task',
        'schedule': crontab(hour=7, minute=0),
    },
    'layby-overdue-alert': {
        'task': 'Notifications.tasks.layby_overdue_alert_task',
        'schedule': crontab(hour=8, minute=0),
    },
    'cash-mismatch-alert': {
        'task': 'Notifications.tasks.cash_mismatch_alert_task',
        'schedule': crontab(hour=1, minute=0),
    },
    'stock-level-alert-scan': {
        'task': 'Notifications.tasks.generate_stock_level_alerts',
        'schedule': crontab(minute='*/30'),
    },
}

# Celery Configuration
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

@app.task(bind=True)
def debug_task(self):
    """Debug task for testing Celery setup."""
    print(f'Request: {self.request!r}')
