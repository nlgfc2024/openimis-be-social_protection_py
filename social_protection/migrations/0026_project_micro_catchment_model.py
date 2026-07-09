from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("location", "0027_remove_hotspot_villages_hotspotvillage"),
        ("social_protection", "0025_alter_activity_date_created_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="project",
            name="micro_catchment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="projects",
                to="location.microcatchment",
            ),
        ),
        migrations.AlterField(
            model_name="historicalproject",
            name="micro_catchment",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="+",
                to="location.microcatchment",
            ),
        ),
    ]
