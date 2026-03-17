from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_holidayrequest'),
    ]

    operations = [
        migrations.AlterField(
            model_name='holidayrequest',
            name='holiday_type',
            field=models.CharField(
                choices=[
                    ('annual', 'Concediu de odihnă anual'),
                    ('medical', 'Concediu medical'),
                    ('maternity', 'Concediu de maternitate'),
                    ('paternal', 'Concediu paternal'),
                    ('study', 'Concediu de studii'),
                    ('partial_child', 'Concediu parțial plătit pentru îngrijirea copilului'),
                    ('supp_unpaid_child', 'Concediu suplimentar neplătit pentru îngrijirea copilului'),
                    ('care_family', 'Concediu pentru îngrijirea unui membru bolnav al familiei'),
                    ('child_disability', 'Concediu pentru îngrijirea copilului cu dizabilități'),
                    ('unpaid_personal', 'Concediu neplătit din cont propriu'),
                ],
                default='annual',
                max_length=20,
            ),
        ),
    ]

