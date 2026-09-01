from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("social_protection", "0026_add_criteria_read_right_to_enrolment_officer"),
    ]

    operations = [
        migrations.AlterField(
            model_name="benefitplan",
            name="max_beneficiaries",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="historicalbenefitplan",
            name="max_beneficiaries",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
