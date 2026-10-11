from django.core.management.base import BaseCommand

from Notifications.tasks import generate_stock_level_alerts


class Command(BaseCommand):
    help = 'Generate low stock and out-of-stock notifications for current inventory.'

    def handle(self, *args, **options):
        result = generate_stock_level_alerts.apply()
        if result and result.result:
            self.stdout.write(self.style.SUCCESS(
                f"Generated {result.result.get('notifications_created', 0)} stock alert notifications."
            ))
        else:
            self.stdout.write(self.style.WARNING('No stock alert notifications were generated.'))
