from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import core.models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0008_stockpurchasepayment'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ExpenseClaim',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(default=django.utils.timezone.localdate)),
                ('category', models.CharField(choices=[('TRAVEL','Travel'),('FOOD','Food'),('LODGING','Lodging'),('TRANSPORT','Local transport'),('SUPPLIES','Supplies'),('OTHER','Other')], max_length=16)),
                ('reason', models.CharField(max_length=200)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('vendor', models.CharField(blank=True, max_length=120)),
                ('reference', models.CharField(blank=True, max_length=100)),
                ('receipt', models.FileField(blank=True, upload_to='expense_claim_receipts/%Y/%m/', validators=[core.models.validate_screenshot])),
                ('status', models.CharField(choices=[('PENDING','Pending'),('APPROVED','Approved'),('REJECTED','Rejected')], default='PENDING', max_length=10)),
                ('reviewer_note', models.CharField(blank=True, max_length=240)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('cash_entry', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='expense_claim', to='core.cashentry')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_expense_claims', to=settings.AUTH_USER_MODEL)),
                ('submitted_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='expense_claims', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-date', '-id']},
        ),
        migrations.CreateModel(
            name='InternshipPayment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(default=django.utils.timezone.localdate)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('payment_method', models.CharField(choices=[('CASH','Cash'),('UPI','UPI'),('BANK','Bank transfer'),('OTHER','Other')], default='CASH', max_length=8)),
                ('reference', models.CharField(blank=True, max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('cash_entry', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='internship_payment', to='core.cashentry')),
                ('internship', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payments', to='core.internshiprecord')),
                ('received_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-date', '-id']},
        ),
        migrations.CreateModel(
            name='ServiceImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.FileField(upload_to='service_screenshots/%Y/%m/', validators=[core.models.validate_screenshot])),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('service', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='images', to='core.servicerecord')),
                ('uploaded_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['uploaded_at', 'id']},
        ),
    ]
