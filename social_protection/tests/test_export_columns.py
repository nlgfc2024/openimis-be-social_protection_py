import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from core.models import ExportableQueryModel
from social_protection.export_mixin import ExportableSocialProtectionQueryMixin
from social_protection.schema import Query


class BeneficiaryExportColumnTest(SimpleTestCase):
    def export_query(self, field_name):
        resolver = Mock()
        query = type('ExportQuery', (ExportableSocialProtectionQueryMixin,), {
            'exportable_fields': [field_name],
            field_name: getattr(Query, field_name),
            f'resolve_{field_name}': resolver,
        })
        query.create_export_function(field_name)
        return query, resolver, getattr(query, f'resolve_{field_name}_export')

    def test_invalid_columns_are_rejected_before_resolver_or_file_generation(self):
        for field_name in ['beneficiary', 'group_beneficiary']:
            query, resolver, export = self.export_query(field_name)
            for file_format in ['csv', 'xlsx']:
                for fields in [None, [], [''], ['unknown'], ['benefitPlan.project.name'],
                               ['benefit_plan__project__name'], ['benefitPlan.beneficiaries.id']]:
                    with self.subTest(field=field_name, format=file_format, fields=fields), \
                            patch.object(ExportableQueryModel, 'create_csv_export') as csv, \
                            patch.object(query, '_create_enrolment_xlsx') as xlsx:
                        with self.assertRaises(ValidationError):
                            export(None, Mock(), fields=fields, file_format=file_format)
                        resolver.assert_not_called()
                        csv.assert_not_called()
                        xlsx.assert_not_called()

    def test_scalar_forward_relation_and_json_key_columns_reach_csv_writer(self):
        for field_name, relation in [('beneficiary', 'individual'), ('group_beneficiary', 'group')]:
            query, resolver, export = self.export_query(field_name)
            columns = ['id', 'benefitPlan.name', f'{relation}.jsonExt.nationalId', 'json_ext__case_ref']
            with self.subTest(field=field_name), \
                    patch.object(ExportableQueryModel, 'create_csv_export') as csv:
                csv.return_value.name = 'beneficiaries.csv'
                info = SimpleNamespace(context=SimpleNamespace(user=Mock()))
                self.assertEqual(export(None, info, fields=columns, fields_columns='{}'), 'beneficiaries.csv')
                self.assertEqual(csv.call_args.args[1], [
                    'id', 'benefit_plan__name', f'{relation}__json_ext__national_id', 'json_ext__case_ref',
                ])
                self.assertEqual(csv.call_args.args[2], info.context.user)
                resolver.assert_called_once()

    def test_valid_xlsx_columns_use_fixed_workbook_export(self):
        for field_name in ['beneficiary', 'group_beneficiary']:
            query, resolver, export = self.export_query(field_name)
            with self.subTest(field=field_name), \
                    patch.object(ExportableQueryModel, 'create_csv_export') as csv, \
                    patch.object(query, '_create_enrolment_xlsx') as xlsx:
                info = SimpleNamespace(context=SimpleNamespace(user=Mock()))
                xlsx.return_value.name = 'enrolments.xlsx'
                result = export(None, info, fields=['id'], fields_columns=json.dumps({}), file_format='xlsx')
                self.assertEqual(result, 'enrolments.xlsx')
                xlsx.assert_called_once_with(resolver.return_value.filter.return_value, field_name, info.context.user)
                csv.assert_not_called()
