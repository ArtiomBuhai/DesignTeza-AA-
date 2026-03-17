from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_chatthread_chatmessage_thread'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='about',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='profile',
            name='education',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='profile',
            name='experience',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='profile',
            name='languages',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='profile',
            name='skills',
            field=models.TextField(blank=True),
        ),
    ]
