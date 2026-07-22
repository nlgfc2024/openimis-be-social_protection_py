from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from social_protection.models import BenefitPlan, BenefitPlanDataUploadRecords
from individual.models import IndividualDataSource, IndividualDataSourceUpload
from social_protection.services import BeneficiaryImportService, BeneficiaryService
from core.test_helpers import LogInHelper
from social_protection.tests.data import service_add_payload
from social_protection.tests.test_helpers import (
    create_benefit_plan,
    create_individual,
    add_individual_to_benefit_plan,
)
from individual.models import Individual
from individual.tests.data import service_add_individual_payload
import pandas as pd


class BeneficiaryImportServiceTest(TestCase):
    user = None
    service = None
    benefit_plan = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = BeneficiaryImportService(cls.user)
        cls.benefit_plan = cls.__create_benefit_plan()
        cls.upload = cls.__create_individual_data_source_upload()
        cls.individual_sources = cls.__create_individual_sources(cls.upload)
        cls.upload_record = cls.__create_benefit_plan_data_upload_records(
            cls.upload,
            cls.benefit_plan,
            'test-workflow',
        )

    def test_validate_import_beneficiaries(self):
        result = self.service.validate_import_beneficiaries(
            self.upload.id,
            self.individual_sources,
            self.benefit_plan
        )
        self.assertTrue(result.get('success', True))

    def test_validate_possible_beneficiares(self):
        dataframe = self.service._load_dataframe(self.individual_sources)
        validated_dataframe, invalid_items = self.service._validate_possible_beneficiaries(
            dataframe,
            self.benefit_plan,
            self.upload.id
        )
        self.assertIsInstance(validated_dataframe, list)
        self.assertIsInstance(invalid_items, list)

    def test_load_dataframe(self):
        result = self.service._load_dataframe(self.individual_sources)
        self.assertIsInstance(result, pd.DataFrame)
        self.assertEqual(result.size, len(self.individual_sources))

    def test_create_task_with_importing_valid_items(self):
        self.service.create_task_with_importing_valid_items(self.upload.id, self.benefit_plan)

    @classmethod
    def __create_individual_data_source_upload(cls):
        object_data = {
            'source_name': 'Sample Source',
            'source_type': 'Sample Type',
            'status': IndividualDataSourceUpload.Status.PENDING,
            'error': {},
            'json_ext': {}
        }

        individual_data_source_upload = IndividualDataSourceUpload(**object_data)
        individual_data_source_upload.save(username=cls.user.username)

        return individual_data_source_upload

    @classmethod
    def __create_individual_data_source(cls, individual_data_source_upload_instance):
        individual_instance = cls.__create_individual()

        object_data = {
            'individual': individual_instance,
            'upload': individual_data_source_upload_instance,
            'validations': {},
            'json_ext': {}
        }

        individual_data_source = IndividualDataSource(**object_data)
        individual_data_source.save(username=cls.user.username)

        return individual_data_source

    @classmethod
    def __create_individual(cls):
        object_data = {
            **service_add_individual_payload
        }

        individual = Individual(**object_data)
        individual.save(username=cls.user.username)

        return individual

    @classmethod
    def __create_benefit_plan(cls):
        object_data = {
            **service_add_payload
        }

        benefit_plan = BenefitPlan(**object_data)
        benefit_plan.save(username=cls.user.username)

        return benefit_plan

    @classmethod
    def __create_individual_sources(cls, upload):
        cls.__create_individual_data_source(upload),
        cls.__create_individual_data_source(upload),
        cls.__create_individual_data_source(upload)
        return IndividualDataSource.objects.filter(upload_id=upload.id)

    @classmethod
    def __create_benefit_plan_data_upload_records(cls, data_upload, benefit_plan, workflow):
        record_upload = BenefitPlanDataUploadRecords(
            data_upload=data_upload,
            benefit_plan=benefit_plan,
            workflow=workflow
        )
        record_upload.save(username=cls.user.username)
        return record_upload


UNIQUENESS_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "properties": {
        "national_id": {
            "type": "string",
            "uniqueness": True,
        },
    },
}


class BeneficiaryImportDeduplicationTest(TestCase):
    """
    Covers the restored DB-level uniqueness check in
    BeneficiaryImportService._validate_possible_beneficiaries /
    process_chunk: duplicates against existing beneficiaries, duplicates
    within the same uploaded batch, and that the query count doesn't scale
    with the number of rows validated.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = BeneficiaryImportService(cls.user)
        cls.benefit_plan = create_benefit_plan(
            cls.user.username,
            payload_override={
                'code': 'DEDUP1',
                'type': 'INDIVIDUAL',
                'beneficiary_data_schema': UNIQUENESS_SCHEMA,
            },
        )

        existing_individual = create_individual(
            cls.user.username, {'first_name': 'Existing'}
        )
        add_individual_to_benefit_plan(
            BeneficiaryService(cls.user),
            existing_individual,
            cls.benefit_plan,
            {
                'status': 'ACTIVE',
                'json_ext': {
                    'national_id': 'DUPDB',
                    'educated_level': 'higher education',
                },
            },
        )

    def _create_upload(self):
        upload = IndividualDataSourceUpload(
            source_name='dedup test upload', source_type='beneficiary import'
        )
        upload.save(username=self.user.username)
        return upload

    def _sources_dataframe(self, upload, national_ids):
        sources = []
        for national_id in national_ids:
            source = IndividualDataSource(
                upload=upload,
                json_ext={'national_id': national_id},
                validations={},
            )
            source.save(username=self.user.username)
            sources.append(source)
        return self.service._load_dataframe(sources)

    def test_duplicate_only_in_database_is_flagged(self):
        upload = self._create_upload()
        dataframe = self._sources_dataframe(upload, ['DUPDB'])

        validated, _ = self.service._validate_possible_beneficiaries(
            dataframe, self.benefit_plan, upload.id
        )

        validation = validated[0]['validations']['national_id_uniqueness']
        self.assertFalse(validation['success'])
        db_matches = validation['duplications']['duplicates_amoung_database']
        self.assertEqual(len(db_matches), 1)
        # The extra json_ext field on the existing beneficiary is
        # unpacked into the duplicate report.
        self.assertEqual(db_matches[0]['educated_level'], 'higher education')
        self.assertEqual(
            validation['duplications']['incoming_duplicates'], []
        )

    def test_duplications_detail_does_not_reach_persisted_validation_errors(self):
        upload = self._create_upload()
        dataframe = self._sources_dataframe(upload, ['DUPDB'])
        source_id = dataframe['id'].iloc[0]

        self.service._validate_possible_beneficiaries(
            dataframe, self.benefit_plan, upload.id
        )

        source = IndividualDataSource.objects.get(id=source_id)
        error_fields = source.validations['validation_errors']
        self.assertEqual(len(error_fields), 1)
        self.assertEqual(set(error_fields[0].keys()), {'field_name', 'note'})

    def test_duplicate_with_mismatched_types_is_flagged(self):
        # json_ext stores an int; the incoming row parses the same value as a string.
        mismatched_individual = create_individual(
            self.user.username, {'first_name': 'Mismatched'}
        )
        add_individual_to_benefit_plan(
            BeneficiaryService(self.user),
            mismatched_individual,
            self.benefit_plan,
            {
                'status': 'ACTIVE',
                'json_ext': {'national_id': 12345},
            },
        )

        upload = self._create_upload()
        dataframe = self._sources_dataframe(upload, ['12345'])

        validated, _ = self.service._validate_possible_beneficiaries(
            dataframe, self.benefit_plan, upload.id
        )

        validation = validated[0]['validations']['national_id_uniqueness']
        self.assertFalse(validation['success'])
        db_matches = validation['duplications']['duplicates_amoung_database']
        self.assertEqual(len(db_matches), 1)

    def test_duplicate_only_within_batch_is_flagged(self):
        upload = self._create_upload()
        dataframe = self._sources_dataframe(
            upload, ['BATCHDUP', 'BATCHDUP']
        )

        validated, _ = self.service._validate_possible_beneficiaries(
            dataframe, self.benefit_plan, upload.id
        )

        for entry in validated:
            validation = entry['validations']['national_id_uniqueness']
            self.assertFalse(validation['success'])
            self.assertEqual(
                validation['duplications']['duplicates_amoung_database'], []
            )
            self.assertEqual(
                len(validation['duplications']['incoming_duplicates']), 1
            )

    def test_no_duplicate_succeeds(self):
        upload = self._create_upload()
        dataframe = self._sources_dataframe(upload, ['UNIQUE1'])

        validated, _ = self.service._validate_possible_beneficiaries(
            dataframe, self.benefit_plan, upload.id
        )

        validation = validated[0]['validations']['national_id_uniqueness']
        self.assertTrue(validation['success'])
        self.assertIsNone(validation['duplications'])

    def test_query_count_does_not_scale_with_row_count(self):
        small_upload = self._create_upload()
        small_dataframe = self._sources_dataframe(
            small_upload, ['SMALL1', 'SMALL2']
        )
        large_upload = self._create_upload()
        large_dataframe = self._sources_dataframe(
            large_upload, [f'LARGE{i}' for i in range(20)]
        )

        with CaptureQueriesContext(connection) as small_ctx:
            self.service._validate_possible_beneficiaries(
                small_dataframe, self.benefit_plan, small_upload.id
            )
        with CaptureQueriesContext(connection) as large_ctx:
            self.service._validate_possible_beneficiaries(
                large_dataframe, self.benefit_plan, large_upload.id
            )

        self.assertEqual(
            len(small_ctx.captured_queries), len(large_ctx.captured_queries)
        )


CORE_FIELD_UNIQUENESS_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "properties": {
        "first_name": {
            "type": "string",
            "uniqueness": True,
        },
        "dob": {
            "type": "string",
            "uniqueness": True,
        },
    },
}


class BeneficiaryImportCoreFieldUniquenessTest(TestCase):
    """Uniqueness on first_name/last_name/dob, which live on Individual, not json_ext."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = BeneficiaryImportService(cls.user)
        cls.benefit_plan = create_benefit_plan(
            cls.user.username,
            payload_override={
                'code': 'DEDUP2',
                'type': 'INDIVIDUAL',
                'beneficiary_data_schema': CORE_FIELD_UNIQUENESS_SCHEMA,
            },
        )

        existing_individual = create_individual(
            cls.user.username,
            {'first_name': 'DupFirst', 'dob': '1990-01-01'},
        )
        add_individual_to_benefit_plan(
            BeneficiaryService(cls.user),
            existing_individual,
            cls.benefit_plan,
            {'status': 'ACTIVE', 'json_ext': {}},
        )

    def _create_upload(self):
        upload = IndividualDataSourceUpload(
            source_name='core field dedup test upload',
            source_type='beneficiary import',
        )
        upload.save(username=self.user.username)
        return upload

    def _sources_dataframe(self, upload, rows):
        sources = []
        for row in rows:
            source = IndividualDataSource(
                upload=upload, json_ext=row, validations={}
            )
            source.save(username=self.user.username)
            sources.append(source)
        return self.service._load_dataframe(sources)

    def test_duplicate_on_first_name_against_database_is_flagged(self):
        upload = self._create_upload()
        dataframe = self._sources_dataframe(
            upload, [{'first_name': 'DupFirst', 'dob': '2000-05-05'}]
        )

        validated, _ = self.service._validate_possible_beneficiaries(
            dataframe, self.benefit_plan, upload.id
        )

        validation = validated[0]['validations']['first_name_uniqueness']
        self.assertFalse(validation['success'])
        self.assertEqual(
            len(validation['duplications']['duplicates_amoung_database']), 1
        )

    def test_duplicate_on_dob_against_database_is_flagged(self):
        upload = self._create_upload()
        dataframe = self._sources_dataframe(
            upload, [{'first_name': 'SomeoneElse', 'dob': '1990-01-01'}]
        )

        validated, _ = self.service._validate_possible_beneficiaries(
            dataframe, self.benefit_plan, upload.id
        )

        validation = validated[0]['validations']['dob_uniqueness']
        self.assertFalse(validation['success'])
        self.assertEqual(
            len(validation['duplications']['duplicates_amoung_database']), 1
        )

    def test_no_duplicate_on_core_fields_succeeds(self):
        upload = self._create_upload()
        dataframe = self._sources_dataframe(
            upload, [{'first_name': 'Unrelated', 'dob': '1985-03-03'}]
        )

        validated, _ = self.service._validate_possible_beneficiaries(
            dataframe, self.benefit_plan, upload.id
        )

        first_name_validation = (
            validated[0]['validations']['first_name_uniqueness']
        )
        dob_validation = validated[0]['validations']['dob_uniqueness']
        self.assertTrue(first_name_validation['success'])
        self.assertTrue(dob_validation['success'])
