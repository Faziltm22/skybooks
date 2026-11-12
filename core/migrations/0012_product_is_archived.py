from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0011_servicerequest'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='is_archived',
            field=models.BooleanField(default=False),
        ),
    ]
