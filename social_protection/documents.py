import logging

from django.apps import apps
from django.conf import settings

logger = logging.getLogger(__name__)

is_unit_test_env = getattr(settings, 'IS_UNIT_TEST_ENV', False)

# Check if the 'opensearch_reports' app is in INSTALLED_APPS
if 'opensearch_reports' in apps.app_configs and not is_unit_test_env:
    from kombu.exceptions import OperationalError as BrokerOperationalError
    from opensearchpy.exceptions import OpenSearchException
    from opensearch_reports.service import BaseSyncDocument
    from django_opensearch_dsl import fields as opensearch_fields
    from django_opensearch_dsl.registries import registry
    from social_protection.models import Beneficiary, BenefitPlan
    from individual.models import Individual

    @registry.register_document
    class BeneficiaryDocument(BaseSyncDocument):
        DASHBOARD_NAME = 'Beneficiary'

        benefit_plan = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'name': opensearch_fields.KeywordField(),
        })
        individual = opensearch_fields.ObjectField(properties={
            'first_name': opensearch_fields.KeywordField(),
            'last_name': opensearch_fields.KeywordField(),
            'dob': opensearch_fields.DateField(),
        })
        status = opensearch_fields.KeywordField(fields={
            'status_key': opensearch_fields.KeywordField()}
        )
        date_created = opensearch_fields.DateField()
        json_ext = opensearch_fields.ObjectField()

        class Index:
            name = 'beneficiary'
            settings = {
                'number_of_shards': 1,
                'number_of_replicas': 0
            }
            auto_refresh = True

        class Django:
            model = Beneficiary
            related_models = [BenefitPlan, Individual]
            fields = [
                'id'
            ]
            queryset_pagination = 5000

        def bulk(self, actions, using=None, from_celery=False, **kwargs):
            try:
                return super().bulk(
                    actions,
                    using=using,
                    from_celery=from_celery,
                    **kwargs,
                )
            except (
                BrokerOperationalError,
                OpenSearchException,
                ConnectionError,
                TimeoutError,
                OSError,
            ):
                # Search indexing is secondary to the database transaction.
                # A stopped Celery broker must not roll back an approved Phase
                # update (or any other beneficiary-related database change).
                logger.warning(
                    "Unable to queue beneficiary OpenSearch synchronization; "
                    "the database change will still be committed.",
                    exc_info=True,
                )
                return None

        def get_instances_from_related(self, related_instance):
            if isinstance(related_instance, BenefitPlan):
                return Beneficiary.objects.filter(benefit_plan=related_instance)
            elif isinstance(related_instance, Individual):
                return Beneficiary.objects.filter(individual=related_instance)

        def prepare_json_ext(self, instance):
            json_ext_data = instance.json_ext
            json_data = self.__flatten_dict(json_ext_data)
            return json_data

        def __flatten_dict(self, d, parent_key='', sep='__'):
            items = {}
            for k, v in d.items():
                new_key = f"{parent_key}{sep}{k}" if parent_key else k
                if isinstance(v, dict):
                    items.update(self.__flatten_dict(v, new_key, sep=sep))
                else:
                    items[new_key] = v
            return items
