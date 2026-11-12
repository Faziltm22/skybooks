from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0006_sale_customer_contact_and_line_discount'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='StockPurchase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(default=django.utils.timezone.localdate)),
                ('supplier', models.CharField(blank=True, max_length=120)),
                ('quantity', models.DecimalField(decimal_places=3, max_digits=12)),
                ('unit_cost', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('payment_method', models.CharField(choices=[('CASH', 'Cash'), ('UPI', 'UPI'), ('BANK', 'Bank transfer'), ('CREDIT', 'Credit')], default='CASH', max_length=8)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('PAID', 'Paid')], default='PAID', max_length=8)),
                ('paid_on', models.DateField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('cash_entry', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='stock_purchase', to='core.cashentry')),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('movement', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='purchase_record', to='core.stockmovement')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='purchases', to='core.product')),
            ],
            options={'ordering': ['-date', '-id']},
        ),
    ]
