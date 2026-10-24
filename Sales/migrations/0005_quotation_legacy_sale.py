"""Step 1 of the invoice approval workflow.

Quotations used to point straight at a Sale. Move that link to ``legacy_sale``
(no data is lost) so ``invoice`` can be repointed at the new Invoice model in
the next migration.
"""

import django.db.models.deletion
from django.db import migrations, models


def move_sale_links(apps, schema_editor):
    Quotation = apps.get_model('Sales', 'Quotation')
    for quotation in Quotation.objects.filter(invoice__isnull=False):
        Quotation.objects.filter(pk=quotation.pk).update(
            legacy_sale_id=quotation.invoice_id,
            invoice=None,
        )


def restore_sale_links(apps, schema_editor):
    Quotation = apps.get_model('Sales', 'Quotation')
    for quotation in Quotation.objects.filter(legacy_sale__isnull=False):
        Quotation.objects.filter(pk=quotation.pk).update(
            invoice_id=quotation.legacy_sale_id,
            legacy_sale=None,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('Sales', '0004_quotation_invoice_conversion'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='quotation',
            name='quotation_converted_matches_invoice',
        ),
        migrations.AddField(
            model_name='quotation',
            name='legacy_sale',
            field=models.OneToOneField(
                blank=True, null=True,
                help_text='Sale created by the pre-approval conversion flow',
                on_delete=django.db.models.deletion.PROTECT,
                related_name='legacy_source_quotation', to='Sales.sale',
            ),
        ),
        migrations.RunPython(move_sale_links, restore_sale_links),
    ]
