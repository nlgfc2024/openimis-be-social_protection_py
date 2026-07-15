from django.db import migrations


class Migration(migrations.Migration):
    """
    Hand off ownership of the project-domain models to the new
    project_social_protection app. This is a STATE-ONLY change: the underlying
    social_protection_* tables are left untouched (database_operations=[]) and
    are adopted, unchanged, by project_social_protection/0001_initial.

    Delete order: historical models first (they hold FKs into the main models),
    then the main models in reverse-dependency order.
    """

    dependencies = [
        ('social_protection', '0023_multi_enrollment_and_time_entry'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name='HistoricalBeneficiaryProjectTimeEntry'),
                migrations.DeleteModel(name='HistoricalGroupBeneficiaryProjectTimeEntry'),
                migrations.DeleteModel(name='HistoricalBeneficiaryProjectEnrollment'),
                migrations.DeleteModel(name='HistoricalGroupBeneficiaryProjectEnrollment'),
                migrations.DeleteModel(name='HistoricalProject'),
                migrations.DeleteModel(name='HistoricalActivity'),
                migrations.DeleteModel(name='BeneficiaryProjectTimeEntry'),
                migrations.DeleteModel(name='GroupBeneficiaryProjectTimeEntry'),
                migrations.DeleteModel(name='BeneficiaryProjectEnrollment'),
                migrations.DeleteModel(name='GroupBeneficiaryProjectEnrollment'),
                migrations.DeleteModel(name='ProjectMutation'),
                migrations.DeleteModel(name='Project'),
                migrations.DeleteModel(name='Activity'),
            ],
            database_operations=[],
        ),
    ]
