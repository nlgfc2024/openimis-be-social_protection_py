from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from social_protection.signals.on_confirm_enrollment_of_individual import (
    on_confirm_enrollment_of_individual,
)


class EnrollmentCapSignalTest(SimpleTestCase):
    def _result(self, selected):
        return {
            "benefit_plan_id": "plan-id",
            "status": "ACTIVE",
            "user": SimpleNamespace(
                login_name="operator",
                username="operator",
            ),
            "individuals_not_assigned_to_selected_programme": selected,
        }

    @patch(
        "social_protection.signals.on_confirm_enrollment_of_individual."
        "SocialProtectionConfig.enable_maker_checker_logic_enrollment",
        False,
    )
    @patch("social_protection.signals.on_confirm_enrollment_of_individual.Beneficiary")
    def test_direct_branch_creates_only_capped_candidates(self, beneficiary):
        selected = [
            SimpleNamespace(json_ext={"rank": 1}),
            SimpleNamespace(json_ext={"rank": 2}),
        ]

        on_confirm_enrollment_of_individual(result=self._result(selected))

        self.assertEqual(beneficiary.call_count, 2)
        created = beneficiary.objects.bulk_create.call_args.args[0]
        self.assertEqual(len(created), 2)

    @patch(
        "social_protection.signals.on_confirm_enrollment_of_individual."
        "SocialProtectionConfig.enable_maker_checker_logic_enrollment",
        True,
    )
    @patch("social_protection.signals.on_confirm_enrollment_of_individual.TaskService")
    @patch("social_protection.signals.on_confirm_enrollment_of_individual.calculate_percentage_of_invalid_items", return_value=0)
    @patch("social_protection.signals.on_confirm_enrollment_of_individual.BenefitPlanDataUploadRecords")
    @patch("social_protection.signals.on_confirm_enrollment_of_individual.IndividualDataSource")
    @patch("social_protection.signals.on_confirm_enrollment_of_individual.IndividualDataSourceUpload")
    @patch("social_protection.signals.on_confirm_enrollment_of_individual.BenefitPlan")
    def test_maker_checker_branch_stages_only_capped_candidates(
        self,
        benefit_plan,
        upload_model,
        data_source,
        upload_record_model,
        _percentage,
        _task_service,
    ):
        benefit_plan.objects.get.return_value = SimpleNamespace(code="PLAN")
        upload_model.return_value = Mock(id="upload-id", source_name="Enrollment")
        upload_record_model.return_value = Mock(
            id="record-id",
            workflow="Enrollment",
            data_upload=SimpleNamespace(source_name="Enrollment"),
        )
        selected = [
            SimpleNamespace(json_ext={"rank": 1}),
            SimpleNamespace(json_ext={"rank": 2}),
        ]

        on_confirm_enrollment_of_individual(result=self._result(selected))

        self.assertEqual(data_source.call_count, 2)
        staged = data_source.objects.bulk_create.call_args.args[0]
        self.assertEqual(len(staged), 2)
