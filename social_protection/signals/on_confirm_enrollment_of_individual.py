import uuid

from django.db import transaction
from individual.models import (
    IndividualDataSourceUpload,
    IndividualDataSource
)
from social_protection.apps import SocialProtectionConfig
from social_protection.models import (
    Beneficiary,
    BenefitPlanDataUploadRecords,
    BenefitPlan
)
from social_protection.utils import bulk_create_in_batches, calculate_percentage_of_invalid_items
from tasks_management.models import Task
from tasks_management.apps import TasksManagementConfig
from tasks_management.services import (
    TaskService
)


@transaction.atomic
def on_confirm_enrollment_of_individual(**kwargs):
    from core import datetime
    result = kwargs.get('result', None)
    benefit_plan_id = result['benefit_plan_id']
    status = result['status']
    user = result['user']
    individuals_to_upload = result['individuals_not_assigned_to_selected_programme']
    if SocialProtectionConfig.enable_maker_checker_logic_enrollment:
        benefit_plan = BenefitPlan.objects.get(id=benefit_plan_id)
        upload = IndividualDataSourceUpload(
            source_name=f"Enrollment into {benefit_plan.code} {datetime.date.today()}",
            source_type='beneficiary import'
        )
        upload.save(username=user.login_name)
        upload_record = BenefitPlanDataUploadRecords(
            data_upload=upload,
            benefit_plan_id=benefit_plan_id,
            workflow="Enrollment"
        )
        upload_record.save(username=user.username)
        data_source_objects = (
            IndividualDataSource(
                uuid=uuid.uuid4(),
                user_created=user,
                user_updated=user,
                upload=upload,
                individual=individual,
                json_ext=individual.json_ext,
                validations={}
            ) for individual in individuals_to_upload
        )
        bulk_create_in_batches(IndividualDataSource.objects, data_source_objects)
        json_ext = {
            'source_name': upload_record.data_upload.source_name,
            'workflow': upload_record.workflow,
            'percentage_of_invalid_items': calculate_percentage_of_invalid_items(upload_record.id),
            'data_upload_id': str(upload.id),
            'benefit_plan_id': benefit_plan_id,
            'beneficiary_status': status
        }
        TaskService(user).create({
            'source': 'import_valid_items',
            'entity': upload_record,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': SocialProtectionConfig.validation_enrollment,
            'json_ext': json_ext
        })
    else:
        new_beneficiaries = (
            Beneficiary(
                individual=individual,
                benefit_plan_id=benefit_plan_id,
                status=status,
                json_ext=individual.json_ext,
                user_created=user,
                user_updated=user,
                uuid=uuid.uuid4(),
            ) for individual in individuals_to_upload
        )
        bulk_create_in_batches(Beneficiary.objects, new_beneficiaries)
