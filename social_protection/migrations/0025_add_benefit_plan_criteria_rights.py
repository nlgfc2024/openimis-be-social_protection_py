from django.db import migrations

from core.utils import insert_role_right_for_system, remove_role_right_for_system


IMIS_ADMIN = 64
CRITERIA_RIGHTS = [171005, 171006]


def add_rights(apps, schema_editor):
    for right_id in CRITERIA_RIGHTS:
        insert_role_right_for_system(IMIS_ADMIN, right_id, apps)


def remove_rights(apps, schema_editor):
    for right_id in CRITERIA_RIGHTS:
        remove_role_right_for_system(IMIS_ADMIN, right_id, apps)


class Migration(migrations.Migration):
    dependencies = [
        ("social_protection", "0024_move_project_to_project_social_protection"),
    ]

    operations = [
        migrations.RunPython(add_rights, remove_rights),
    ]
