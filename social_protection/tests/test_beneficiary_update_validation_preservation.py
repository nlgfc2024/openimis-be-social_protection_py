"""Exercise both real SQL update paths: ordinary uploads cannot alter validation."""
from datetime import date
from contextlib import nullcontext
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TestCase

from core.models import User
from individual.models import Individual, IndividualDataSource, IndividualDataSourceUpload
from social_protection.models import BenefitPlan, Beneficiary
from social_protection.workflows.beneficiary_update_valid import process_update_valid_beneficiaries_workflow


@skipUnless(connection.vendor == "postgresql", "The beneficiary update workflows use PostgreSQL procedures.")
class BeneficiaryUpdateValidationPreservationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create(username="validation-preservation-test")
        cls.plan = BenefitPlan(code="UPDVAL", name="Validation preservation", type="INDIVIDUAL",
                               beneficiary_data_schema={"properties": {
                                   "business_name": {"type": "string"}, "validation_status": {"type": "string"},
                               }})
        cls.plan.save(user=cls.actor)
        cls.people = []
        cls.beneficiaries = []
        for number in range(2):
            person = Individual(first_name=f"Owner {number}", last_name="Before", dob=date(1990, 1, 1),
                                json_ext={"validation_status": "VERIFIED", "data_source": "ubr",
                                          "untouched": {"nested": True}, "business_name": "Before"})
            person.save(user=cls.actor)
            cls.people.append(person)
            beneficiary = Beneficiary(individual=person, benefit_plan=cls.plan, status="ACTIVE",
                                      json_ext={"validation_status": "VERIFIED", "existing": "keep"})
            beneficiary.save(user=cls.actor)
            cls.beneficiaries.append(beneficiary)

    def run_upload(self, partial, status="OMITTED", real_reporting=False):
        upload = IndividualDataSourceUpload(source_name="test.csv", source_type="update", status="PENDING")
        upload.save(user=self.actor)
        rows = []
        for beneficiary in self.beneficiaries:
            data = {"ID": str(beneficiary.pk), "first_name": "Updated", "last_name": "Owner",
                    "dob": "1990-01-01", "location_name": None, "location_code": None,
                    "business_name": "Updated business"}
            if status != "OMITTED":
                data["validation_status"] = status
            row = IndividualDataSource(upload=upload, json_ext=data, validations={"validation_errors": []})
            row.save(user=self.actor)
            rows.append(row)
        accepted = [rows[0].pk] if partial else None
        reporting = nullcontext() if real_reporting else patch(
            "social_protection.services.BeneficiaryImportService._synchronize_data_for_reporting",
        )
        with reporting:
            process_update_valid_beneficiaries_workflow(self.actor.pk, self.plan.pk, upload.pk, accepted)
        return upload

    def assert_preserved(self, index):
        person = Individual.objects.get(pk=self.people[index].pk)
        self.assertEqual(person.json_ext["validation_status"], "VERIFIED")
        self.assertEqual(person.json_ext["untouched"], {"nested": True})
        self.assertEqual(person.json_ext["data_source"], "ubr")
        self.assertEqual(person.json_ext["business_name"], "Updated business")
        self.assertEqual(person.first_name, "Updated")
        self.assertNotIn("ID", person.json_ext)
        beneficiary = Beneficiary.objects.get(pk=self.beneficiaries[index].pk)
        self.assertEqual(beneficiary.json_ext["validation_status"], "VERIFIED")
        self.assertEqual(beneficiary.json_ext["existing"], "keep")

    def test_full_update_preserves_omitted_validation_and_unrelated_fields(self):
        self.run_upload(False)
        self.assert_preserved(0)
        self.assert_preserved(1)

    def test_partial_update_preserves_omitted_validation_and_does_not_touch_unaccepted_rows(self):
        self.run_upload(True)
        self.assert_preserved(0)
        self.people[1].refresh_from_db()
        self.assertEqual(self.people[1].first_name, "Owner 1")
        self.assertEqual(self.people[1].json_ext["business_name"], "Before")

    def test_full_update_cannot_overwrite_or_clear_validation_even_when_schema_allows_it(self):
        for value in ("NOT_VERIFIED", "REJECTED", None, ""):
            with self.subTest(value=value):
                self.run_upload(False, value)
                self.assert_preserved(0)

    def test_partial_update_cannot_overwrite_or_clear_validation_even_when_schema_allows_it(self):
        for value in ("NOT_VERIFIED", "REJECTED", None, ""):
            with self.subTest(value=value):
                self.run_upload(True, value)
                self.assert_preserved(0)

    def test_an_upload_cannot_introduce_validation_on_a_previously_unvalidated_person(self):
        Individual.objects.filter(pk=self.people[0].pk).update(json_ext={"untouched": "keep"})
        self.run_upload(False, "VERIFIED")
        self.people[0].refresh_from_db()
        self.assertNotIn("validation_status", self.people[0].json_ext)
        self.assertEqual(self.people[0].json_ext["untouched"], "keep")

    def test_null_legacy_json_becomes_an_object_without_importing_a_validation_decision(self):
        with connection.cursor() as cursor:
            cursor.execute('UPDATE individual_individual SET "Json_ext" = \'null\'::jsonb WHERE "UUID" = %s',
                           [self.people[0].pk])
        self.run_upload(False, "VERIFIED")
        self.people[0].refresh_from_db()
        self.assertEqual(self.people[0].json_ext, {"business_name": "Updated business"})

    def test_reporting_synchronization_also_keeps_the_validation_result(self):
        self.run_upload(False, "NOT_VERIFIED", real_reporting=True)
        self.assert_preserved(0)
        self.people[0].refresh_from_db()
        self.assertEqual(self.people[0].json_ext["report_synch"], "true")
