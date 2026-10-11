from __future__ import annotations

from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management import call_command


class Command(BaseCommand):
    help = 'Create a JSON backup of the application data.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output',
            type=str,
            help='Optional output file path. Defaults to backups/backup-YYYYMMDD-HHMMSS.json',
        )
        parser.add_argument(
            '--indent',
            type=int,
            default=2,
            help='JSON indentation level for readability.',
        )

    def handle(self, *args, **options):
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_dir = Path(settings.BASE_DIR) / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)

        output_path = Path(options['output']) if options['output'] else backup_dir / f'backup-{timestamp}.json'
        output_path.parent.mkdir(parents=True, exist_ok=True)

        app_labels = ['Core', 'Accounts', 'Inventory', 'Sales', 'Expenses', 'Notifications', 'authtoken']
        with output_path.open('w', encoding='utf-8') as backup_file:
            call_command(
                'dumpdata',
                *app_labels,
                format='json',
                indent=options['indent'],
                natural_foreign=True,
                natural_primary=True,
                stdout=backup_file,
            )

        self.stdout.write(self.style.SUCCESS(f'Backup created at {output_path}'))