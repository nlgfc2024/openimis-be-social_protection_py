from django.db import migrations

from core.utils import insert_role_right_for_system, remove_role_right_for_system


ENROLMENT_OFFICER = 1
CRITERIA_SEARCH_RIGHT = 171005


def add_right(apps, schema_editor):
    insert_role_right_for_system(
        ENROLMENT_OFFICER,
        CRITERIA_SEARCH_RIGHT,
        apps,
    )


def remove_right(apps, schema_editor):
    remove_role_right_for_system(
        ENROLMENT_OFFICER,
        CRITERIA_SEARCH_RIGHT,
        apps,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("social_protection", "0025_add_benefit_plan_criteria_rights"),
    ]

    operations = [
        migrations.RunPython(add_right, remove_right),
    ]
