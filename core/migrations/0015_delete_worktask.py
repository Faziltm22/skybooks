from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_worktask'),
    ]

    operations = [
        migrations.DeleteModel(
            name='WorkTask',
        ),
    ]
