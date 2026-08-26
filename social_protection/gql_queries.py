import json

import graphene
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from graphene import ObjectType
from graphene_django import DjangoObjectType
import graphene_django_optimizer as gql_optimizer
import django_filters

from contribution_plan.models import PaymentPlan
from core import prefix_filterset, ExtendedConnection
from individual.models import GroupIndividual
from individual.gql_queries import IndividualGQLType, GroupGQLType, \
    IndividualDataSourceUploadGQLType
from location.models import Location
from social_protection.apps import SocialProtectionConfig
from social_protection.models import (
    Beneficiary,
    BenefitPlan,
    GroupBeneficiary,
    BenefitPlanDataUploadRecords,
)


def _have_permissions(user, permission):
    if isinstance(user, AnonymousUser):
        return False
    if not user.id:
        return False
    return user.has_perms(permission)


class JsonExtMixin:
    def resolve_json_ext(self, info):
        user = info.context.user
        if not _have_permissions(
            user,
            SocialProtectionConfig.gql_schema_search_perms,
        ):
            return None

        json_ext = self.json_ext or {}
        if _have_permissions(
            user,
            SocialProtectionConfig.gql_benefit_plan_criteria_search_perms,
        ):
            return json_ext

        if isinstance(json_ext, str):
            try:
                json_ext = json.loads(json_ext)
            except (TypeError, json.JSONDecodeError):
                return {}
        if not isinstance(json_ext, dict):
            return {}
        return {
            key: value
            for key, value in json_ext.items()
            if key not in {"advanced_criteria", "enrolment_ranking"}
        }


class BenefitPlanGQLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')
    has_payment_plans = graphene.Boolean()
    advanced_criteria = graphene.JSONString()
    enrolment_ranking = graphene.JSONString()

    class Meta:
        model = BenefitPlan
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "code": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "name": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "max_beneficiaries": ["exact", "lt", "lte", "gt", "gte"],
            "institution": ["exact", "iexact", "startswith", "istartswith",
                            "contains", "icontains"],
            "type": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "description": ["exact", "iexact", "startswith", "istartswith",
                            "contains", "icontains"],
        }
        connection_class = ExtendedConnection

    def resolve_beneficiary_data_schema(self, info):
        perms = SocialProtectionConfig.gql_schema_search_perms
        if _have_permissions(info.context.user, perms):
            return self.beneficiary_data_schema
        return None

    def resolve_has_payment_plans(self, info):
        return PaymentPlan.objects.filter(benefit_plan_id=self.id).exists()

    def resolve_advanced_criteria(self, info):
        perms = SocialProtectionConfig.gql_benefit_plan_criteria_search_perms
        if _have_permissions(info.context.user, perms):
            json_ext = self.json_ext or {}
            if isinstance(json_ext, str):
                try:
                    json_ext = json.loads(json_ext)
                except (TypeError, json.JSONDecodeError):
                    return {}
            if isinstance(json_ext, dict):
                return json_ext.get("advanced_criteria", {})
            return {}
        return None

    def resolve_enrolment_ranking(self, info):
        perms = SocialProtectionConfig.gql_benefit_plan_criteria_search_perms
        if _have_permissions(info.context.user, perms):
            json_ext = self.json_ext or {}
            if isinstance(json_ext, str):
                try:
                    json_ext = json.loads(json_ext)
                except (TypeError, json.JSONDecodeError):
                    return {}
            if isinstance(json_ext, dict):
                return json_ext.get("enrolment_ranking", {})
            return {}
        return None


class BeneficiarySharedFilterMixin:
    location_prefix = None  # must be defined in subclass

    def filter_is_eligible(self, queryset, name, value):
        return queryset.filter(is_eligible=value)

    def filter_location(self, queryset, name, value):
        if not value or not self.location_prefix:
            return queryset

        # Split multiple level filters (format: "level1:term1,level2:term2")
        level_filters = [f.strip() for f in value.split(',') if f.strip()]
        if not level_filters:
            return queryset

        location_q = Q()

        for level_filter in level_filters:
            if ':' not in level_filter:
                continue

            level_str, search_term = level_filter.split(':', 1)
            level = int(level_str.strip())
            search_term = search_term.strip()

            if not search_term or level < 0 or level > 3:
                continue

            # Determine the lookup path based on level (R=0, D=1, W=2, V=3):
            # For level 3 (Village), we look at group's location directly
            # For lower levels, we traverse up the parent chain
            parent_chain = '__'.join(['parent'] * (3 - level))
            if parent_chain:
                lookup = (
                    f"{self.location_prefix}location__"
                    f"{parent_chain}__name__icontains"
                )
            else:
                lookup = f"{self.location_prefix}location__name__icontains"
            location_q &= Q(**{lookup: search_term})

        return queryset.filter(location_q) if location_q else queryset


class BeneficiaryFilter(
    django_filters.FilterSet, BeneficiarySharedFilterMixin
):
    location_prefix = "individual__"
    is_eligible = django_filters.BooleanFilter(method='filter_is_eligible')
    search = django_filters.CharFilter(method='filter_search')
    location = django_filters.CharFilter(method='filter_location')

    class Meta:
        model = Beneficiary
        fields = {
            "id": ["exact"],
            "status": ["exact", "iexact", "startswith", "istartswith",
                       "contains", "icontains"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            **prefix_filterset("individual__",
                               IndividualGQLType._meta.filter_fields),
            **prefix_filterset("benefit_plan__",
                               BenefitPlanGQLType._meta.filter_fields),
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }

    def filter_search(self, queryset, name, value):
        if not value:
            return queryset

        village_matches = Location.objects.filter(
            type='V',
            validity_to__isnull=True,
        ).filter(
            Q(name__icontains=value)
            | Q(parent__name__icontains=value)
            | Q(parent__parent__name__icontains=value)
            | Q(parent__parent__parent__name__icontains=value)
        ).values_list('id', flat=True)

        return queryset.filter(
            Q(individual__first_name__icontains=value)
            | Q(individual__last_name__icontains=value)
            | Q(json_ext__icontains=value)
            | Q(individual__location__id__in=village_matches)
        )


class BeneficiaryGQLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')
    is_eligible = graphene.Boolean()

    class Meta:
        model = Beneficiary
        interfaces = (graphene.relay.Node,)
        filterset_class = BeneficiaryFilter
        connection_class = ExtendedConnection
        # Project enrollment is owned by project_social_protection; do not expose
        # the reverse relation here (query it via that module instead).
        exclude = ("project_enrollments",)

    def resolve_is_eligible(self, info):
        return self.is_eligible


class GroupBeneficiaryFilter(
    django_filters.FilterSet, BeneficiarySharedFilterMixin
):
    location_prefix = "group__"
    is_eligible = django_filters.BooleanFilter(method='filter_is_eligible')
    search = django_filters.CharFilter(method='filter_search')
    location = django_filters.CharFilter(method='filter_location')

    class Meta:
        model = GroupBeneficiary
        fields = {
            "id": ["exact"],
            "status": ["exact", "iexact", "startswith", "istartswith",
                       "contains", "icontains"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            **prefix_filterset("group__",
                               GroupGQLType._meta.filter_fields),
            **prefix_filterset("benefit_plan__",
                               BenefitPlanGQLType._meta.filter_fields),
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }

    def filter_search(self, queryset, name, value):
        if not value:
            return queryset

        head_matches = GroupIndividual.objects.filter(
            Q(individual__first_name__icontains=value)
            | Q(individual__last_name__icontains=value),
            role=GroupIndividual.Role.HEAD,
            is_deleted=False
        ).values_list('group_id', flat=True)

        village_matches = Location.objects.filter(
            type='V',
            validity_to__isnull=True,
        ).filter(
            Q(name__icontains=value)
            | Q(parent__name__icontains=value)
            | Q(parent__parent__name__icontains=value)
            | Q(parent__parent__parent__name__icontains=value)
        ).values_list('id', flat=True)

        return queryset.filter(
            Q(group__code__icontains=value)
            | Q(json_ext__icontains=value)
            | Q(group__id__in=head_matches)
            | Q(group__location__id__in=village_matches)
        )


class GroupBeneficiaryGQLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')
    is_eligible = graphene.Boolean()

    class Meta:
        model = GroupBeneficiary
        interfaces = (graphene.relay.Node,)
        filterset_class = GroupBeneficiaryFilter
        connection_class = ExtendedConnection
        # Project enrollment is owned by project_social_protection; do not expose
        # the reverse relation here (query it via that module instead).
        exclude = ("project_enrollments",)

    def resolve_is_eligible(self, info):
        return self.is_eligible


class BenefitPlanDataUploadQGLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = BenefitPlanDataUploadRecords
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "workflow": ["exact", "iexact", "startswith", "istartswith",
                         "contains", "icontains"],
            **prefix_filterset(
                "data_upload__",
                IndividualDataSourceUploadGQLType._meta.filter_fields),
            **prefix_filterset("benefit_plan__",
                               BenefitPlanGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class BenefitPlanSchemaFieldsGQLType(ObjectType):
    schema_fields = graphene.List(graphene.String)

    def resolve_schema_fields(self, info, **kwargs):
        schemas = self.values_list(
            "beneficiary_data_schema__properties", flat=True)
        field_list = set(
            f'json_ext__{field}'
            for schema in schemas  # Iterate over each schema
            if schema  # Ensure the schema is not None or empty
            for field in schema  # Iterate over fields in the schema
        )
        return field_list


class BenefitPlanHistoryGQLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')
    has_payment_plans = graphene.Boolean()

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = BenefitPlan.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "code": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "name": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "max_beneficiaries": ["exact", "lt", "lte", "gt", "gte"],
            "institution": ["exact", "iexact", "startswith", "istartswith",
                            "contains", "icontains"],

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "description": ["exact", "iexact", "startswith", "istartswith",
                            "contains", "icontains"],
        }
        connection_class = ExtendedConnection

    def resolve_beneficiary_data_schema(self, info):
        perms = SocialProtectionConfig.gql_schema_search_perms
        if _have_permissions(info.context.user, perms):
            return self.beneficiary_data_schema
        return None

    def resolve_has_payment_plans(self, info):
        return PaymentPlan.objects.filter(benefit_plan_id=self.id).exists()
