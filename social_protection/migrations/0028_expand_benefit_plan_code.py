from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("social_protection", "0027_expand_benefit_plan_max_beneficiaries"),
    ]

    operations = [
        migrations.AlterField(
            model_name="benefitplan",
            name="code",
            field=models.CharField(max_length=9),
        ),
        migrations.AlterField(
            model_name="historicalbenefitplan",
            name="code",
            field=models.CharField(max_length=9),
        ),
    ]
