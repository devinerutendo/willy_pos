from __future__ import annotations

from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Restore application data from a JSON backup.'

    def add_arguments(self, parser):
        parser.add_argument('input', type=str, help='Path to the JSON backup file.')
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing data before loading the backup.',
        )

    def handle(self, *args, **options):
        input_path = Path(options['input'])
        if not input_path.exists():
            raise CommandError(f'Backup file does not exist: {input_path}')

        if options['clear']:
            call_command('flush', interactive=False)

        call_command('loaddata', str(input_path))

        self.stdout.write(self.style.SUCCESS(f'Backup restored from {input_path}'))