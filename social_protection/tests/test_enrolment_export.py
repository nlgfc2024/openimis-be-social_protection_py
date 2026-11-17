import csv
from datetime import date
from io import BytesIO
import io
import json
from tempfile import TemporaryDirectory

import graphene
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from openpyxl import load_workbook

from core.models import ExportableQueryModel
from core.test_helpers import create_test_interactive_user
from individual.models import Group, GroupIndividual, Individual
from location.models import Location
from social_protection.export_mixin import (
    ENROLMENT_EXPORT_COLUMNS,
    ExportableSocialProtectionQueryMixin,
    KYC_EXPORT_COLUMNS,
)
from social_protection.models import Beneficiary, BenefitPlan, GroupBeneficiary
from social_protection.schema import Query


class EnrolmentExportWorkbookTest(SimpleTestCase):
    def test_workbook_contains_every_required_reporting_column(self):
        workbook_content = ExportableSocialProtectionQueryMixin._enrolment_workbook([])
        worksheet = load_workbook(BytesIO(workbook_content))["Enrolments"]
        headers = [cell.value for cell in next(worksheet.iter_rows(max_row=1))]

        self.assertEqual(headers, list(ENROLMENT_EXPORT_COLUMNS))
        self.assertIn("date_of_birth", headers)
        self.assertIn("project_code", headers)
        self.assertIn("relationship", headers)
        self.assertIn("catchment_name", headers)
        self.assertIn("account_number", headers)
        self.assertIn("account_name", headers)
        self.assertIn("bank", headers)

    def test_kyc_workbook_has_only_kyc_columns(self):
        workbook_content = ExportableSocialProtectionQueryMixin._enrolment_workbook(
            [], KYC_EXPORT_COLUMNS,
        )
        worksheet = load_workbook(BytesIO(workbook_content))["Enrolments"]
        headers = [cell.value for cell in next(worksheet.iter_rows(max_row=1))]

        self.assertEqual(headers, list(KYC_EXPORT_COLUMNS))
        self.assertIn("phase_name", headers)
        self.assertNotIn("project_code", headers)
        self.assertNotIn("enrolment_type", headers)

    def test_kyc_account_number_cells_are_text_formatted(self):
        workbook_content = ExportableSocialProtectionQueryMixin._enrolment_workbook(
            [{"account_number": "00012345"}], KYC_EXPORT_COLUMNS,
        )
        worksheet = load_workbook(BytesIO(workbook_content))["Enrolments"]
        account_number_column = list(KYC_EXPORT_COLUMNS).index("account_number") + 1

        self.assertEqual(worksheet.cell(2, account_number_column).number_format, "@")


@override_settings(ROW_SECURITY=False, MODE='dev')
class BeneficiaryExportFileTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        from project_social_protection.models import (
            Activity, BeneficiaryProjectEnrollment, GroupBeneficiaryProjectEnrollment, Project,
        )

        cls.user = create_test_interactive_user(username='expadmin')
        location = None
        for location_type in ['R', 'D', 'W', 'V']:
            location = Location.objects.create(type=location_type, code=f'EXP-{location_type}',
                                               name=f'Export {location_type}', parent=location, audit_user_id=-1)
        activity = Activity(name='Export activity')
        activity.save(username=cls.user.username)
        cls.records = {}
        for field_name, plan_type in [('beneficiary', 'INDIVIDUAL'), ('group_beneficiary', 'GROUP')]:
            plan = BenefitPlan(code=plan_type[:3], name=f'{plan_type} export plan', type=plan_type)
            plan.save(username=cls.user.username)
            project = Project(code=plan_type[:3], name=f'{plan_type} export project', benefit_plan=plan,
                              activity=activity, location=location, target_beneficiaries=10, working_days=5)
            project.save(username=cls.user.username)
            records = []
            # Only one beneficiary is selected for export. Its workbook count must
            # not include the other enrollment in the same project.
            for index in range(2):
                person = Individual(first_name=f'Person{index}', last_name='Export', dob=date(2000, 1, 2),
                                    json_ext={'national_id': f'MW-{index}'}, location=location)
                person.save(username=cls.user.username)
                if field_name == 'group_beneficiary':
                    group = Group(code=f'EXPORT-{index}', location=location)
                    group.save(username=cls.user.username)
                    member = GroupIndividual(group=group, individual=person, role='HEAD')
                    member.save(username=cls.user.username)
                    record = GroupBeneficiary(group=group, benefit_plan=plan, status='ACTIVE',
                                              json_ext={'case_ref': f'CASE-{index}'})
                else:
                    record = Beneficiary(individual=person, benefit_plan=plan, status='ACTIVE',
                                         json_ext={'case_ref': f'CASE-{index}'})
                record.save(username=cls.user.username)
                enrollment = (GroupBeneficiaryProjectEnrollment(group_beneficiary=record, project=project)
                              if field_name == 'group_beneficiary'
                              else BeneficiaryProjectEnrollment(beneficiary=record, project=project))
                enrollment.save(username=cls.user.username)
                records.append(record)
            cls.records[field_name] = records

    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(MEDIA_ROOT=media.name)
        settings.enable()
        self.addCleanup(settings.disable)

    def export(self, field_name, columns, file_format):
        request = RequestFactory().get('/graphql')
        request.user = self.user
        gql_field = 'groupBeneficiaryExport' if field_name == 'group_beneficiary' else 'beneficiaryExport'
        record = self.records[field_name][0]
        result = graphene.Schema(query=Query).execute(
            'query($columns:GenericScalar) {' + gql_field + '(id:' + json.dumps(str(record.pk))
            + ',fields:' + json.dumps(columns) + ',fieldsColumns:$columns,fileFormat:'
            + json.dumps(file_format) + ')}',
            context_value=request, variable_values={'columns': '{}'},
        )
        self.assertIsNone(result.errors, result.errors)
        export = ExportableQueryModel.objects.get(name=result.data[gql_field])
        self.assertEqual(export.user, self.user)
        with export.content.open('rb') as content:
            data = content.read()
        if file_format == 'csv':
            return list(csv.DictReader(io.StringIO(data.decode())))
        workbook = load_workbook(BytesIO(data), read_only=True)
        rows = list(workbook['Enrolments'].values)
        workbook.close()
        self.assertEqual(list(rows[0]), list(ENROLMENT_EXPORT_COLUMNS))
        return [dict(zip(rows[0], row)) for row in rows[1:]]

    def check_csv(self, field_name):
        relation = 'group' if field_name == 'group_beneficiary' else 'individual'
        rows = self.export(field_name, [
            'id', 'benefitPlan.name', 'json_ext', 'json_ext__case_ref', f'{relation}.jsonExt',
        ], 'csv')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], str(self.records[field_name][0].pk))
        self.assertEqual(rows[0]['json_ext__case_ref'], 'CASE-0')
        self.assertEqual(rows[0]['benefit_plan__name'], self.records[field_name][0].benefit_plan.name)

    def check_xlsx(self, field_name):
        rows = self.export(field_name, ['id'], 'xlsx')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['national_id'], 'MW-0')
        self.assertEqual(rows[0]['full_name'], 'Person0 Export')
        self.assertEqual(rows[0]['enrolled_HHs'], 1)
        self.assertEqual(rows[0]['target_HHs'], 10)
        self.assertEqual(rows[0]['project_code'], self.records[field_name][0].benefit_plan.type[:3])

    def test_individual_beneficiary_csv_preserves_json_keys_and_forward_columns(self):
        self.check_csv('beneficiary')

    def test_group_beneficiary_csv_preserves_json_keys_and_forward_columns(self):
        self.check_csv('group_beneficiary')

    def test_individual_beneficiary_xlsx_contains_scoped_enrollment_counts(self):
        self.check_xlsx('beneficiary')

    def test_group_beneficiary_xlsx_contains_scoped_enrollment_counts(self):
        self.check_xlsx('group_beneficiary')
