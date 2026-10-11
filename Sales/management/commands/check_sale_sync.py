"""
Django management command to check inventory synchronization for sales.

Usage:
    python manage.py check_sale_sync SALE-20260325120000-AB12
    python manage.py check_sale_sync --last-hour
    python manage.py check_sale_sync --status failed
"""

import logging
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from datetime import timedelta
from Sales.models import Sale
from Sales.log_utils import SalesLogAnalyzer, check_sale_inventory_sync
from Inventory.models import Stock


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Check inventory synchronization status for sales. '
        'Diagnoses if inventory was properly deducted when sales were completed.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'sale_ref',
            nargs='?',
            type=str,
            help='Sale reference number (e.g., SALE-20260325120000-AB12)'
        )
        
        parser.add_argument(
            '--last-hour',
            action='store_true',
            help='Check all sales from the last hour'
        )
        
        parser.add_argument(
            '--last-day',
            action='store_true',
            help='Check all sales from the last 24 hours'
        )
        
        parser.add_argument(
            '--status',
            type=str,
            choices=['pending', 'completed', 'cancelled', 'failed'],
            help='Filter by sale status or sync status'
        )
        
        parser.add_argument(
            '--summary',
            action='store_true',
            help='Show summary of all operations from logs'
        )

    def handle(self, *args, **options):
        sale_ref = options.get('sale_ref')
        last_hour = options.get('last_hour')
        last_day = options.get('last_day')
        status_filter = options.get('status')
        show_summary = options.get('summary')

        # Show summary of all log operations
        if show_summary:
            self._show_summary()
            return

        # Check specific sale
        if sale_ref:
            self._check_single_sale(sale_ref)
            return

        # Check multiple sales
        if last_hour or last_day or status_filter:
            hours = 1 if last_hour else (24 if last_day else None)
            self._check_multiple_sales(hours, status_filter)
            return

        # Default: show help
        self.print_help('manage.py', 'check_sale_sync')

    def _check_single_sale(self, sale_ref: str):
        """Check a single sale's inventory synchronization."""
        self.stdout.write(f"\n🔍 Checking sale: {sale_ref}\n")

        try:
            sale = Sale.objects.get(reference=sale_ref)
        except Sale.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"❌ Sale not found: {sale_ref}"))
            return

        # Get log information
        log_status = check_sale_inventory_sync(sale_ref)

        # Get database information
        self.stdout.write(self.style.SUCCESS("═" * 70))
        self.stdout.write(f"{'SALE INVENTORY SYNC CHECK':^70}")
        self.stdout.write(self.style.SUCCESS("═" * 70))

        # Sale details
        self.stdout.write(f"\nSale Reference: {sale.reference}")
        self.stdout.write(f"Status:         {sale.status}")
        self.stdout.write(f"Created:        {sale.created_at}")
        self.stdout.write(f"Items Count:    {sale.items.count()}")
        self.stdout.write(f"Total Amount:   ${sale.total_amount}")

        # Inventory sync status
        self.stdout.write("\n" + "─" * 70)
        self.stdout.write("INVENTORY SYNCHRONIZATION STATUS")
        self.stdout.write("─" * 70)

        if not log_status['found_in_logs']:
            self.stdout.write(self.style.WARNING("⚠ Sale not found in signal logs"))
        else:
            self.stdout.write(self.style.SUCCESS("✓ Sale found in signal logs"))

        if log_status['has_errors']:
            self.stdout.write(self.style.ERROR(f"✗ Errors detected ({len(log_status['error_details'])})"))
            for error in log_status['error_details'][:3]:  # Show first 3 errors
                self.stdout.write(f"  - {error[:100]}")
        else:
            self.stdout.write(self.style.SUCCESS("✓ No errors in logs"))

        if log_status['completion_confirmed']:
            self.stdout.write(self.style.SUCCESS("✓ Inventory completion confirmed in logs"))
        else:
            self.stdout.write(self.style.WARNING("⚠ No completion confirmation found in logs"))

        # Item-by-item inventory check
        self.stdout.write("\n" + "─" * 70)
        self.stdout.write("ITEM-BY-ITEM INVENTORY VERIFICATION")
        self.stdout.write("─" * 70)

        for item in sale.items.all():
            stock_records = Stock.objects.filter(product=item.product)
            
            self.stdout.write(f"\nProduct: {item.product.name}")
            self.stdout.write(f"  Sold Quantity: {item.quantity} @ ${item.unit_price} = ${item.line_total}")

            if stock_records.exists():
                for stock in stock_records:
                    self.stdout.write(
                        f"  Stock at {stock.location.name}: {stock.quantity} "
                        f"(Reserved: {stock.reserved_quantity}, Available: {stock.available_stock})"
                    )
            else:
                self.stdout.write(self.style.WARNING("  ⚠ No stock records found"))

        # Summary recommendation
        self.stdout.write("\n" + "─" * 70)
        self.stdout.write("RECOMMENDATION")
        self.stdout.write("─" * 70)

        if sale.status == Sale.COMPLETED:
            if log_status['completion_confirmed']:
                self.stdout.write(self.style.SUCCESS("✓ Sale appears to be properly synchronized"))
            else:
                self.stdout.write(
                    self.style.ERROR("❌ ISSUE: Sale marked COMPLETED but inventory may not have been deducted")
                )
                self.stdout.write("   Check logs for errors or manually verify stock levels")
                if log_status['has_errors']:
                    self.stdout.write("   Errors in processing detected - review signals log")
        elif sale.status == Sale.PENDING:
            self.stdout.write(self.style.WARNING("⚠ Sale is PENDING - inventory should be reserved but not deducted"))

        self.stdout.write(self.style.SUCCESS("═" * 70 + "\n"))

    def _check_multiple_sales(self, hours: int = None, status_filter: str = None):
        """Check multiple sales' synchronization."""
        query = Sale.objects.all()

        if hours:
            cutoff = timezone.now() - timedelta(hours=hours)
            query = query.filter(created_at__gte=cutoff)

        if status_filter == 'pending':
            query = query.filter(status=Sale.PENDING)
        elif status_filter == 'completed':
            query = query.filter(status=Sale.COMPLETED)
        elif status_filter == 'cancelled':
            query = query.filter(status=Sale.CANCELLED)

        self.stdout.write(f"\n📊 Checking {query.count()} sales...\n")

        if query.count() == 0:
            self.stdout.write(self.style.WARNING("No sales found matching criteria"))
            return

        # Load logs once
        analyzer = SalesLogAnalyzer()
        analyzer.load_logs(hours_back=hours or 24)

        results = {
            'total': 0,
            'with_errors': 0,
            'unconfirmed': 0,
            'synced': 0
        }

        self.stdout.write(self.style.SUCCESS("Reference".ljust(30)) + "Status".ljust(15) + "Sync Status")
        self.stdout.write("─" * 70)

        for sale in query.order_by('-created_at')[:50]:  # Limit to 50
            log_status = check_sale_inventory_sync(sale.reference)
            results['total'] += 1

            if log_status['has_errors']:
                results['with_errors'] += 1
                sync_status = self.style.ERROR("❌ ERRORS")
            elif not log_status['completion_confirmed']:
                results['unconfirmed'] += 1
                sync_status = self.style.WARNING("⚠ UNCONFIRMED")
            else:
                results['synced'] += 1
                sync_status = self.style.SUCCESS("✓ SYNCED")

            self.stdout.write(
                sale.reference.ljust(30) + 
                sale.status.ljust(15) + 
                sync_status
            )

        self.stdout.write("─" * 70)
        self.stdout.write(f"\nTotal:       {results['total']}")
        self.stdout.write(self.style.SUCCESS(f"✓ Synced:    {results['synced']}"))
        self.stdout.write(self.style.WARNING(f"⚠ Unconfirmed: {results['unconfirmed']}"))
        self.stdout.write(self.style.ERROR(f"❌ With Errors: {results['with_errors']}"))
        self.stdout.write()

    def _show_summary(self):
        """Show summary of all signal operations from logs."""
        analyzer = SalesLogAnalyzer()
        if analyzer.load_logs(hours_back=24):
            analyzer.print_summary()
        else:
            self.stdout.write(self.style.WARNING("Could not load logs"))
