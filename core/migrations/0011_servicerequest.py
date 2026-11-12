from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0010_supplierreturn_activity_snapshots'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ServiceRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=160)),
                ('service_type', models.CharField(choices=[('DRONE','Drone service'),('REPAIR','Repair'),('TRAINING','Training'),('INSTALL','Installation'),('CONSULT','Consulting'),('OTHER','Other')], default='DRONE', max_length=10)),
                ('customer_name', models.CharField(max_length=140)),
                ('email', models.EmailField(blank=True, max_length=254)),
                ('contact', models.CharField(max_length=80)),
                ('description', models.TextField(blank=True)),
                ('status', models.CharField(choices=[('WAITING','Waiting for review'),('ACCEPTED','Accepted'),('REJECTED','Rejected')], default='WAITING', max_length=10)),
                ('admin_note', models.CharField(blank=True, max_length=240)),
                ('requested_at', models.DateTimeField(auto_now_add=True)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_service_requests', to=settings.AUTH_USER_MODEL)),
                ('service', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='client_request', to='core.servicerecord')),
                ('submitted_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='service_requests', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-requested_at', '-id']},
        ),
    ]
