from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_profile_last_seen'),
    ]

    operations = [
        migrations.AddField(
            model_name='holidayrequest',
            name='admin_note',
            field=models.TextField(blank=True),
        ),
    ]

