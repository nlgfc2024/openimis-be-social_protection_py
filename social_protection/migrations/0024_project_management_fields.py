from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("location", "0023_hotspot_micro_catchment_villages"),
        ("social_protection", "0023_multi_enrollment_and_time_entry"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectPhase",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
                ("phase_number", models.PositiveSmallIntegerField(unique=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={
                "verbose_name": "Project Phase",
                "verbose_name_plural": "Project Phases",
                "ordering": ("phase_number",),
            },
        ),
        migrations.CreateModel(
            name="ProjectSector",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={
                "verbose_name": "Project Sector",
                "verbose_name_plural": "Project Sectors",
            },
        ),
        migrations.AlterField(
            model_name="project",
            name="benefit_plan",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, to="social_protection.benefitplan"),
        ),
        migrations.AlterField(
            model_name="project",
            name="activity",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, to="social_protection.activity"),
        ),
        migrations.AlterField(
            model_name="project",
            name="location",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, to="location.location"),
        ),
        migrations.AlterField(
            model_name="project",
            name="target_beneficiaries",
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="project",
            name="status",
            field=models.CharField(
                choices=[
                    ("PREPARATION", "PREPARATION"),
                    ("INITIATED", "INITIATED"),
                    ("IN_PROGRESS", "IN PROGRESS"),
                    ("COMPLETED", "COMPLETED"),
                ],
                default="PREPARATION",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="district",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="projects_as_district", to="location.location"),
        ),
        migrations.AddField(
            model_name="project",
            name="hotspot",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="projects", to="location.hotspot"),
        ),
        migrations.AddField(
            model_name="project",
            name="known_place",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="project",
            name="micro_catchment",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="projects_as_micro_catchment", to="location.location"),
        ),
        migrations.AddField(
            model_name="project",
            name="phase",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, to="social_protection.projectphase"),
        ),
        migrations.AddField(
            model_name="project",
            name="sector",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, to="social_protection.projectsector"),
        ),
        migrations.AddField(
            model_name="project",
            name="target_households",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="historicalproject",
            name="benefit_plan",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to="social_protection.benefitplan"),
        ),
        migrations.AlterField(
            model_name="historicalproject",
            name="activity",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to="social_protection.activity"),
        ),
        migrations.AlterField(
            model_name="historicalproject",
            name="location",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to="location.location"),
        ),
        migrations.AlterField(
            model_name="historicalproject",
            name="target_beneficiaries",
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="historicalproject",
            name="status",
            field=models.CharField(
                choices=[
                    ("PREPARATION", "PREPARATION"),
                    ("INITIATED", "INITIATED"),
                    ("IN_PROGRESS", "IN PROGRESS"),
                    ("COMPLETED", "COMPLETED"),
                ],
                default="PREPARATION",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="historicalproject",
            name="district",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to="location.location"),
        ),
        migrations.AddField(
            model_name="historicalproject",
            name="hotspot",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to="location.hotspot"),
        ),
        migrations.AddField(
            model_name="historicalproject",
            name="known_place",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="historicalproject",
            name="micro_catchment",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to="location.location"),
        ),
        migrations.AddField(
            model_name="historicalproject",
            name="phase",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to="social_protection.projectphase"),
        ),
        migrations.AddField(
            model_name="historicalproject",
            name="sector",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to="social_protection.projectsector"),
        ),
        migrations.AddField(
            model_name="historicalproject",
            name="target_households",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
