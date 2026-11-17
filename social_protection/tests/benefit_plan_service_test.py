import copy
import uuid
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from social_protection.models import BenefitPlan, UserBenefitPlanAccess
from social_protection.services import BenefitPlanService
from social_protection.tests.data import (
    service_add_payload,
    service_add_payload_no_ext,
    service_update_payload,
    service_add_payload_same_code,
    service_add_payload_same_name,
    service_add_payload_invalid_schema,
)
from core.test_helpers import LogInHelper


class BenefitPlanServiceTest(TestCase):
    user = None
    service = None
    query_all = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = BenefitPlanService(cls.user)
        cls.query_all = BenefitPlan.objects.filter(is_deleted=False)

    def test_add_benefit_plan(self):
        result = self.service.create(service_add_payload)
        self.assertTrue(
            result.get('success', False),
            result.get('detail', "No details provided")
        )
        uuid = result.get('data', {}).get('uuid', None)
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)
        self.assertTrue(UserBenefitPlanAccess.objects.filter(
            user=self.user,
            benefit_plan_id=uuid,
            is_deleted=False,
        ).exists())

    def test_failed_creator_assignment_rolls_back_benefit_plan(self):
        with patch.object(
            UserBenefitPlanAccess,
            "save",
            side_effect=IntegrityError("assignment failed"),
        ):
            result = self.service.create(service_add_payload)

        self.assertFalse(result.get("success", True))
        self.assertFalse(
            self.query_all.filter(code=service_add_payload["code"]).exists()
        )

    def test_add_benefit_plan_no_ext(self):
        result = self.service.create(service_add_payload_no_ext)
        self.assertTrue(
            result.get('success', False),
            result.get('detail', "No details provided")
        )
        uuid = result.get('data', {}).get('uuid')
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)

    def test_update_benefit_plan(self):
        result = self.service.create(service_add_payload)
        self.assertTrue(
            result.get('success', False),
            result.get('detail', "No details provided")
        )
        uuid = result.get('data', {}).get('uuid')
        update_payload = copy.deepcopy(service_update_payload)
        update_payload['id'] = uuid
        result = self.service.update(update_payload)
        self.assertTrue(
            result.get('success', False),
            result.get('detail', "No details provided")
        )
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)
        self.assertEqual(query.first().name, update_payload.get('name'))

    def test_delete_benefit_plan(self):
        result = self.service.create(service_add_payload)
        self.assertTrue(
            result.get('success', False),
            result.get('detail', "No details provided")
        )
        uuid = result.get('data', {}).get('uuid')
        delete_payload = {'id': uuid}
        result = self.service.delete(delete_payload)
        self.assertTrue(
            result.get('success', False),
            result.get('detail', "No details provided")
        )
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 0)

    def test_add_not_unique_code_benefit_plan(self):
        first_bf = self.service.create(service_add_payload)
        self.assertTrue(
            first_bf.get('success', False),
            first_bf.get('detail', "No details provided")
        )
        uuid = first_bf.get('data', {}).get('uuid', None)
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)
        second_bf = self.service.create(service_add_payload_same_code)
        self.assertFalse(second_bf.get('success', True))
        code = first_bf['data']['code']
        code_query = self.query_all.filter(code=code)
        self.assertEqual(code_query.count(), 1)

    def test_add_not_unique_name_benefit_plan(self):
        first_bf = self.service.create(service_add_payload)
        self.assertTrue(
            first_bf.get('success', False),
            first_bf.get('detail', "No details provided")
        )
        uuid = first_bf.get('data', {}).get('uuid', None)
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)
        second_bf = self.service.create(service_add_payload_same_name)
        self.assertFalse(second_bf.get('success', True))
        name = first_bf['data']['name']
        name_query = self.query_all.filter(name=name)
        self.assertEqual(name_query.count(), 1)

    def test_add_invalid_schema_benefit_plan(self):
        result = self.service.create(service_add_payload_invalid_schema)
        self.assertFalse(result.get('success', True))

    def test_undo_delete_benefit_plan(self):
        result = self.service.create(service_add_payload)
        self.assertTrue(
            result.get('success', False),
            result.get('detail', "No details provided")
        )
        bp_uuid = result.get('data', {}).get('uuid')

        delete_result = self.service.delete({'id': bp_uuid})
        self.assertTrue(delete_result.get('success', False))
        self.assertFalse(
            BenefitPlan.objects.filter(id=bp_uuid, is_deleted=False).exists()
        )

        undo_result = self.service.undo_delete({'id': bp_uuid})
        self.assertTrue(undo_result.get('success', False))
        self.assertTrue(
            BenefitPlan.objects.filter(id=bp_uuid, is_deleted=False).exists()
        )

    def test_undo_delete_benefit_plan_nonexistent(self):
        with self.assertRaises(ValidationError):
            self.service.undo_delete({'id': uuid.uuid4()})

    def test_undo_delete_benefit_plan_duplicate_code(self):
        result = self.service.create(service_add_payload)
        self.assertTrue(result.get('success', False))
        bp_uuid = result.get('data', {}).get('uuid')

        self.service.delete({'id': bp_uuid})

        # Create another plan with the same code while the first is deleted
        duplicate = self.service.create(service_add_payload)
        self.assertTrue(duplicate.get('success', False))

        with self.assertRaises(ValidationError):
            self.service.undo_delete({'id': bp_uuid})
