from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import core.models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0007_stockpurchase'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='StockPurchasePayment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(default=django.utils.timezone.localdate)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('payment_method', models.CharField(choices=[('CASH', 'Cash'), ('UPI', 'UPI'), ('BANK', 'Bank transfer')], max_length=8)),
                ('transaction_id', models.CharField(max_length=100)),
                ('proof', models.FileField(upload_to='stock_purchase_proofs/%Y/%m/', validators=[core.models.validate_screenshot])),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('cash_entry', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='stock_purchase_payment', to='core.cashentry')),
                ('paid_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('purchase', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payments', to='core.stockpurchase')),
            ],
            options={'ordering': ['-date', '-id']},
        ),
    ]
