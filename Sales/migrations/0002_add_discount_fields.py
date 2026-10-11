from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Sales', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleitem',
            name='discount_percentage',
            field=models.DecimalField(decimal_places=2, default=0, help_text='Discount percentage (0-100%, admin only)', max_digits=5),
        ),
        migrations.AddField(
            model_name='saleitem',
            name='discount_amount',
            field=models.DecimalField(decimal_places=2, default=0, help_text='Fixed discount amount per unit (admin only)', max_digits=12),
        ),
        migrations.AddField(
            model_name='quotationitem',
            name='discount_percentage',
            field=models.DecimalField(decimal_places=2, default=0, help_text='Discount percentage (0-100%, admin only)', max_digits=5),
        ),
        migrations.AddField(
            model_name='quotationitem',
            name='discount_amount',
            field=models.DecimalField(decimal_places=2, default=0, help_text='Fixed discount amount per unit (admin only)', max_digits=12),
        ),
        migrations.AddConstraint(
            model_name='saleitem',
            constraint=models.CheckConstraint(condition=models.Q(('discount_percentage__gte', 0), ('discount_percentage__lte', 100)), name='sale_discount_percentage_valid'),
        ),
        migrations.AddConstraint(
            model_name='quotationitem',
            constraint=models.CheckConstraint(condition=models.Q(('discount_percentage__gte', 0), ('discount_percentage__lte', 100)), name='quotation_discount_percentage_valid'),
        ),
    ]
