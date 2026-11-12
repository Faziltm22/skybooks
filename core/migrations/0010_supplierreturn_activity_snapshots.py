from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0009_expenseclaim_internshippayment_serviceimage'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='activitylog', name='before_data',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='activitylog', name='after_data',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='activitylog', name='recovered_from',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='recoveries', to='core.activitylog'),
        ),
        migrations.CreateModel(
            name='SupplierReturn',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('supplier', models.CharField(max_length=120)),
                ('date', models.DateField(default=django.utils.timezone.localdate)),
                ('quantity', models.DecimalField(decimal_places=3, max_digits=12)),
                ('amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('settlement', models.CharField(choices=[('CASH','Cash refund'),('UPI','UPI refund'),('BANK','Bank refund'),('CREDIT','Supplier credit note')], default='CASH', max_length=8)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('cash_entry', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='supplier_return', to='core.cashentry')),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('movement', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='supplier_return', to='core.stockmovement')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='supplier_returns', to='core.product')),
                ('purchase', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='returns', to='core.stockpurchase')),
            ],
            options={'ordering': ['-date', '-id']},
        ),
    ]
