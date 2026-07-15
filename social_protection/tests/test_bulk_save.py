"""
Tests for core.models.HistoryBusinessModel.bulk_save method.

These tests are located in social_protection because core
doesn't have models inheriting from HistoryBusinessModel.
"""
import uuid

from django.test import TestCase

from project_social_protection.models import BeneficiaryProjectEnrollment
from social_protection.services import BeneficiaryService
from core.test_helpers import LogInHelper
from social_protection.tests.test_helpers import (
    create_benefit_plan,
    create_individual,
)
from project_social_protection.tests.test_helpers import create_project


class BulkSaveTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_benefit_plan(cls.user.username, {
            'code': 'BULKSAVE',
            'type': 'INDIVIDUAL',
        })

    def create_test_project(self, name):
        return create_project(name, self.benefit_plan, self.user.username)

    def create_test_beneficiary(self, first_name):
        individual = create_individual(self.user.username, {'first_name': first_name})
        service = BeneficiaryService(self.user)
        result = service.create({
            'individual_id': individual.id,
            'benefit_plan_id': self.benefit_plan.id,
            'status': 'ACTIVE',
        })
        return uuid.UUID(result['data']['uuid'])

    def test_bulk_save_create(self):
        project = self.create_test_project('test_bulk_save_create')
        beneficiary1_id = self.create_test_beneficiary('Create1')
        beneficiary2_id = self.create_test_beneficiary('Create2')

        data_list = [
            {'beneficiary_id': beneficiary1_id, 'project_id': project.id},
            {'beneficiary_id': beneficiary2_id, 'project_id': project.id},
        ]

        result = BeneficiaryProjectEnrollment.bulk_save(data_list, self.user)

        self.assertEqual(result['created'], 2)
        self.assertEqual(result['updated'], 0)

        enrollments = BeneficiaryProjectEnrollment.objects.filter(
            project_id=project.id, is_deleted=False
        )
        self.assertEqual(enrollments.count(), 2)

    def test_bulk_save_update(self):
        project = self.create_test_project('test_bulk_save_update')
        beneficiary_id = self.create_test_beneficiary('Update1')

        enrollment = BeneficiaryProjectEnrollment(
            beneficiary_id=beneficiary_id, project_id=project.id
        )
        enrollment.save(user=self.user)
        original_version = enrollment.version

        data_list = [{
            'id': enrollment.id,
            'beneficiary_id': beneficiary_id,
            'project_id': project.id
        }]

        result = BeneficiaryProjectEnrollment.bulk_save(data_list, self.user)

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['updated'], 1)

        enrollment.refresh_from_db()
        self.assertEqual(enrollment.version, original_version + 1)

    def test_bulk_save_include_deleted_queries_deleted_records(self):
        project = self.create_test_project('test_include_deleted')
        beneficiary_id = self.create_test_beneficiary('IncludeDel1')

        enrollment = BeneficiaryProjectEnrollment(
            beneficiary_id=beneficiary_id, project_id=project.id
        )
        enrollment.save(user=self.user)
        enrollment.is_deleted = True
        enrollment.save(user=self.user)
        original_id = enrollment.id

        data_list = [{'id': enrollment.id, 'is_deleted': False}]

        result = BeneficiaryProjectEnrollment.bulk_save(
            data_list, self.user, include_deleted=True
        )

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['updated'], 1)

        enrollment.refresh_from_db()
        self.assertEqual(enrollment.id, original_id)
        self.assertFalse(enrollment.is_deleted)

    def test_bulk_save_include_deleted_can_soft_delete(self):
        project = self.create_test_project('test_soft_delete')
        beneficiary_id = self.create_test_beneficiary('SoftDel1')

        enrollment = BeneficiaryProjectEnrollment(
            beneficiary_id=beneficiary_id, project_id=project.id
        )
        enrollment.save(user=self.user)
        self.assertFalse(enrollment.is_deleted)

        data_list = [{'id': enrollment.id, 'is_deleted': True}]

        result = BeneficiaryProjectEnrollment.bulk_save(
            data_list, self.user, include_deleted=True
        )

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['updated'], 1)

        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_deleted)

    def test_bulk_save_mixed_create_and_update(self):
        project = self.create_test_project('test_mixed')
        beneficiary1_id = self.create_test_beneficiary('Mixed1')
        beneficiary2_id = self.create_test_beneficiary('Mixed2')

        enrollment = BeneficiaryProjectEnrollment(
            beneficiary_id=beneficiary1_id, project_id=project.id
        )
        enrollment.save(user=self.user)

        data_list = [
            {'id': enrollment.id, 'beneficiary_id': beneficiary1_id, 'project_id': project.id},
            {'beneficiary_id': beneficiary2_id, 'project_id': project.id},
        ]

        result = BeneficiaryProjectEnrollment.bulk_save(data_list, self.user)

        self.assertEqual(result['created'], 1)
        self.assertEqual(result['updated'], 1)

    def test_bulk_save_empty_list(self):
        result = BeneficiaryProjectEnrollment.bulk_save([], self.user)

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['updated'], 0)

    def test_bulk_save_history_created(self):
        project = self.create_test_project('test_history')
        beneficiary_id = self.create_test_beneficiary('History1')

        enrollment = BeneficiaryProjectEnrollment(
            beneficiary_id=beneficiary_id, project_id=project.id
        )
        enrollment.save(user=self.user)
        initial_history_count = enrollment.history.count()

        data_list = [{'id': enrollment.id, 'is_deleted': True}]

        BeneficiaryProjectEnrollment.bulk_save(
            data_list, self.user, include_deleted=True
        )

        enrollment.refresh_from_db()
        self.assertEqual(enrollment.history.count(), initial_history_count + 1)
