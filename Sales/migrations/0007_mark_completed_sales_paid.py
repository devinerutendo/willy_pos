"""Existing completed sales were all paid at the point of sale; keep them paid."""

from django.db import migrations


def mark_existing_completed_sales_paid(apps, schema_editor):
    Sale = apps.get_model('Sales', 'Sale')
    Sale.objects.filter(status='COMPLETED').update(payment_status='PAID')


class Migration(migrations.Migration):

    dependencies = [
        ('Sales', '0006_invoice_approval_workflow'),
    ]

    operations = [
        migrations.RunPython(mark_existing_completed_sales_paid, migrations.RunPython.noop),
    ]
