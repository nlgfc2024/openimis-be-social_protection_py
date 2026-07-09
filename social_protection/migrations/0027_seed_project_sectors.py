from django.db import migrations


SECTORS = (
    "Social and Water Conservation",
    "Fisheries",
)


def seed_project_sectors(apps, schema_editor):
    ProjectSector = apps.get_model("social_protection", "ProjectSector")
    for name in SECTORS:
        ProjectSector.objects.update_or_create(
            name=name,
            defaults={"is_active": True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("social_protection", "0026_project_micro_catchment_model"),
    ]

    operations = [
        migrations.RunPython(seed_project_sectors, migrations.RunPython.noop),
    ]
