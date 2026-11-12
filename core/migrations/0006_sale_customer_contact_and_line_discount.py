from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0005_cashentry_party_cashentry_payment_method_and_more')]

    operations = [
        migrations.AddField('sale', 'customer_address', models.CharField(blank=True, max_length=240)),
        migrations.AddField('sale', 'customer_phone', models.CharField(blank=True, max_length=40)),
        migrations.AddField('sale', 'customer_email', models.EmailField(blank=True, max_length=254)),
        migrations.AddField('saleline', 'discount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
    ]
