from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase

from social_protection.services import EnrollmentPersistenceService
from social_protection.utils import bulk_create_in_batches


class BulkEnrollmentBatchTest(TestCase):
    def test_large_iterable_is_written_in_bounded_batches(self):
        manager = Mock()

        bulk_create_in_batches(manager, range(2501), batch_size=1000)

        batch_sizes = [
            len(call.args[0]) for call in manager.bulk_create.call_args_list
        ]
        self.assertEqual(batch_sizes, [1000, 1000, 501])

    def test_later_batch_failure_rolls_back_earlier_batches(self):
        class FailingManager:
            calls = 0

            def bulk_create(self, objects, batch_size):
                self.calls += 1
                if self.calls == 2:
                    raise ValidationError("second batch failed")
                return Group.objects.bulk_create(objects, batch_size=batch_size)

        groups = (Group(name=f"atomic-batch-test-{index}") for index in range(3))

        with self.assertRaises(ValidationError):
            bulk_create_in_batches(FailingManager(), groups, batch_size=2)

        self.assertFalse(
            Group.objects.filter(name__startswith="atomic-batch-test-").exists()
        )


class EnrollmentPersistenceServiceTest(TestCase):
    def setUp(self):
        self.user = SimpleNamespace(
            login_name="operator",
            username="operator",
        )
        self.service = EnrollmentPersistenceService(self.user)

    @patch("social_protection.services.bulk_create_in_batches")
    @patch("social_protection.services.Beneficiary")
    def test_persist_individuals_creates_only_supplied_candidates(
        self, beneficiary, bulk_create
    ):
        bulk_create.side_effect = lambda _manager, objects: list(objects)
        selected = Mock()
        selected.iterator.return_value = iter([
            SimpleNamespace(json_ext={"rank": 1}),
            SimpleNamespace(json_ext={"rank": 2}),
        ])

        self.service.persist_individuals("plan-id", "ACTIVE", selected)

        self.assertEqual(beneficiary.call_count, 2)
        bulk_create.assert_called_once()

    @patch("social_protection.services.bulk_create_in_batches")
    @patch("social_protection.services.BenefitPlanDataUploadRecords")
    @patch("social_protection.services.IndividualDataSource")
    @patch("social_protection.services.IndividualDataSourceUpload")
    def test_stage_individuals_returns_persisted_upload_record(
        self, upload_model, data_source, upload_record_model, bulk_create
    ):
        bulk_create.side_effect = lambda _manager, objects: list(objects)
        upload = Mock(id="upload-id", source_name="Enrollment")
        upload_model.return_value = upload
        upload_record = Mock(data_upload=upload)
        upload_record_model.return_value = upload_record
        selected = Mock()
        selected.iterator.return_value = iter([
            SimpleNamespace(json_ext={"rank": 1}),
            SimpleNamespace(json_ext={"rank": 2}),
        ])

        result = self.service.stage_individuals(
            SimpleNamespace(code="PLAN"),
            selected,
        )

        self.assertIs(result, upload_record)
        self.assertEqual(data_source.call_count, 2)
        upload.save.assert_called_once_with(username="operator")
        upload_record.save.assert_called_once_with(username="operator")

    @patch("social_protection.services.bulk_create_in_batches")
    @patch("social_protection.services.GroupBeneficiary")
    @patch("social_protection.services.GroupIndividual")
    def test_persist_groups_uses_exactly_one_head_per_group(
        self, group_individual, group_beneficiary, bulk_create
    ):
        bulk_create.side_effect = lambda _manager, objects: list(objects)
        groups = Mock()
        groups.count.return_value = 1
        groups.values_list.return_value = ["group-id"]
        head = SimpleNamespace(
            group=Mock(),
            individual=SimpleNamespace(json_ext={"rank": 1}),
        )
        heads = Mock()
        heads.values.return_value.distinct.return_value.count.return_value = 1
        heads.count.return_value = 1
        heads.iterator.return_value = iter([head])
        group_individual.objects.filter.return_value.select_related.return_value.distinct.return_value = heads

        self.service.persist_groups("plan-id", "ACTIVE", groups)

        self.assertEqual(group_beneficiary.call_count, 1)
        bulk_create.assert_called_once()

    @patch("social_protection.services.GroupIndividual")
    def test_group_without_head_is_rejected(self, group_individual):
        groups = Mock()
        groups.count.return_value = 1
        groups.values_list.return_value = ["group-id"]
        heads = Mock()
        heads.values.return_value.distinct.return_value.count.return_value = 0
        heads.count.return_value = 0
        group_individual.objects.filter.return_value.select_related.return_value.distinct.return_value = heads

        with self.assertRaisesMessage(ValidationError, "household head"):
            self.service.persist_groups("plan-id", "ACTIVE", groups)
