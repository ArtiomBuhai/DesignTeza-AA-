from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_profile_calendar_feed_token_uniquify'),
    ]

    operations = [
        migrations.AlterField(
            model_name='profile',
            name='calendar_feed_token',
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
    ]

