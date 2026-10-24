from __future__ import annotations

from pathlib import Path

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction

# Must match the apps written by the ``backup_system`` command.
BACKUP_APP_LABELS = ['Core', 'Accounts', 'Inventory', 'Sales', 'Expenses', 'Notifications', 'authtoken']


def _backup_tables() -> list[str]:
    tables: list[str] = []
    for label in BACKUP_APP_LABELS:
        config = apps.get_app_config(label)
        for model in config.get_models(include_auto_created=True):
            if model._meta.managed and not model._meta.proxy:
                tables.append(model._meta.db_table)
    return tables


class Command(BaseCommand):
    help = 'Restore application data from a JSON backup.'

    def add_arguments(self, parser):
        parser.add_argument('input', type=str, help='Path to the JSON backup file.')
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear the data covered by backups before loading the backup.',
        )

    def handle(self, *args, **options):
        input_path = Path(options['input'])
        if not input_path.exists():
            raise CommandError(f'Backup file does not exist: {input_path}')

        # Everything happens in one transaction: if loading fails, the existing data is kept.
        with transaction.atomic():
            if options['clear']:
                # Only wipe the tables a backup contains. A full ``flush`` would also delete
                # unrelated data such as the installed licence.
                sql_list = connection.ops.sql_flush(
                    no_style(), _backup_tables(), reset_sequences=True, allow_cascade=True
                )
                connection.ops.execute_sql_flush(sql_list)

            call_command('loaddata', str(input_path))

        self.stdout.write(self.style.SUCCESS(f'Backup restored from {input_path}'))
