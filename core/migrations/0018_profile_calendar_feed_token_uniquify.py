from django.db import migrations
import uuid


def ensure_unique_calendar_tokens(apps, schema_editor):
    Profile = apps.get_model('core', 'Profile')
    seen = set()
    for prof in Profile.objects.all().order_by('id'):
        token = str(prof.calendar_feed_token) if prof.calendar_feed_token else ''
        if not token or token in seen:
            new_token = uuid.uuid4()
            while str(new_token) in seen:
                new_token = uuid.uuid4()
            prof.calendar_feed_token = new_token
            prof.save(update_fields=['calendar_feed_token'])
            token = str(new_token)
        seen.add(token)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_profile_calendar_feed_token'),
    ]

    operations = [
        migrations.RunPython(ensure_unique_calendar_tokens, migrations.RunPython.noop),
    ]

