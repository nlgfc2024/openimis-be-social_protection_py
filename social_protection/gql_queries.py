import json

import graphene
from django.contrib.auth.models import AnonymousUser
from django.db.models import OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from graphene import ObjectType
from graphene_django import DjangoObjectType
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
    UserBenefitPlanAccess,
)
from social_protection.benefit_plan_access import (
    can_manage_benefit_plan_assignments,
    filter_benefit_plan_assignments_for_management,
    filter_queryset_by_benefit_plan,
    require_benefit_plan_assignment_management_permission,
)


def _have_permissions(user, permission):
    if isinstance(user, AnonymousUser):
        return False
    if not user.id:
        return False
    return user.has_perms(permission)


def _saved_advanced_criteria(benefit_plan):
    json_ext = benefit_plan.json_ext or {}
    if isinstance(json_ext, str):
        try:
            json_ext = json.loads(json_ext)
        except (TypeError, json.JSONDecodeError):
            return {}
    if not isinstance(json_ext, dict):
        return {}
    saved = json_ext.get("advanced_criteria", {}) or {}
    return saved if isinstance(saved, (dict, list)) else {}


def annotate_group_beneficiary_primary_recipient(queryset):
    from individual.primary_recipients import authoritative_primary_worker_memberships

    primary_memberships = authoritative_primary_worker_memberships().filter(
        group_id=OuterRef("group_id"),
    ).order_by("id")
    head_memberships = GroupIndividual.objects.filter(
        group_id=OuterRef("group_id"),
        role=GroupIndividual.Role.HEAD,
        is_deleted=False,
    ).order_by("id")

    return queryset.annotate(
        primary_recipient_first_name=Coalesce(
            Subquery(primary_memberships.values("individual__first_name")[:1]),
            Subquery(head_memberships.values("individual__first_name")[:1]),
        ),
        primary_recipient_last_name=Coalesce(
            Subquery(primary_memberships.values("individual__last_name")[:1]),
            Subquery(head_memberships.values("individual__last_name")[:1]),
        ),
        primary_recipient_dob=Coalesce(
            Subquery(primary_memberships.values("individual__dob")[:1]),
            Subquery(head_memberships.values("individual__dob")[:1]),
        ),
    )


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
            # enrolment_ranking is retired but may still be stored on a Phase.
            if key not in {"advanced_criteria", "enrolment_ranking"}
        }


class BenefitPlanGQLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')
    has_payment_plans = graphene.Boolean()
    advanced_criteria = graphene.JSONString()
    updates_require_approval = graphene.Boolean()

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

    @classmethod
    def get_queryset(cls, queryset, info):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return queryset
        return filter_queryset_by_benefit_plan(
            queryset,
            info.context.user,
            "id",
        )

    @classmethod
    def get_node(cls, info, id):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return BenefitPlan.objects.filter(pk=id).first()
        user = info.context.user
        if not _have_permissions(
            user,
            SocialProtectionConfig.gql_benefit_plan_search_perms,
        ):
            return None
        return cls.get_queryset(BenefitPlan.objects.all(), info).filter(
            pk=id,
        ).first()

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
            return _saved_advanced_criteria(self)
        return None

    def resolve_updates_require_approval(self, info):
        if _have_permissions(info.context.user, SocialProtectionConfig.gql_benefit_plan_criteria_search_perms):
            return bool(SocialProtectionConfig.gql_check_benefit_plan_update)
        return None


class BenefitPlanAssignmentUserGQLType(graphene.ObjectType):
    id = graphene.ID(required=True)
    username = graphene.String(required=True)
    first_name = graphene.String()
    last_name = graphene.String()

    def resolve_id(self, info):
        return str(self.id)

    def _resolve_name(self, field):
        for relation in ("i_user", "officer", "claim_admin"):
            related_user = getattr(self, relation, None)
            value = getattr(related_user, field, None)
            if value and value.strip():
                return value
        if field == "other_names":
            return self.username
        return None

    def resolve_first_name(self, info):
        return BenefitPlanAssignmentUserGQLType._resolve_name(
            self,
            "other_names",
        )

    def resolve_last_name(self, info):
        return BenefitPlanAssignmentUserGQLType._resolve_name(
            self,
            "last_name",
        )


class UserBenefitPlanAccessGQLType(DjangoObjectType):
    uuid = graphene.String(source="uuid")
    user = graphene.Field(BenefitPlanAssignmentUserGQLType, required=True)

    class Meta:
        model = UserBenefitPlanAccess
        fields = ("id", "user", "date_created")
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "user__username": ["exact", "icontains"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        queryset = queryset.filter(is_deleted=False)
        return filter_benefit_plan_assignments_for_management(
            queryset,
            info.context.user,
        )

    @classmethod
    def get_node(cls, info, id):
        user = info.context.user
        require_benefit_plan_assignment_management_permission(user)
        assignment = UserBenefitPlanAccess.objects.filter(
            id=id,
            is_deleted=False,
        ).first()
        if assignment is None:
            return None
        if not can_manage_benefit_plan_assignments(
            user,
            assignment.benefit_plan_id,
        ):
            return None
        return assignment


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

    @classmethod
    def get_queryset(cls, queryset, info):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return queryset
        if not _have_permissions(
            info.context.user,
            SocialProtectionConfig.gql_beneficiary_search_perms,
        ):
            return queryset.none()
        return Beneficiary.get_queryset(queryset, info.context.user)

    @classmethod
    def get_node(cls, info, id):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return Beneficiary.objects.filter(pk=id).first()
        user = info.context.user
        if not _have_permissions(
            user,
            SocialProtectionConfig.gql_beneficiary_search_perms,
        ):
            return None
        return Beneficiary.get_queryset(None, user).filter(pk=id).first()

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
        from individual.primary_recipients import authoritative_primary_worker_memberships

        primary_matches = authoritative_primary_worker_memberships().filter(
            Q(individual__first_name__icontains=value)
            | Q(individual__last_name__icontains=value),
        ).values_list("group_id", flat=True)

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
            | Q(group__id__in=primary_matches)
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

    @classmethod
    def get_queryset(cls, queryset, info):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return queryset
        if not _have_permissions(
            info.context.user,
            SocialProtectionConfig.gql_beneficiary_search_perms,
        ):
            return queryset.none()
        return GroupBeneficiary.get_queryset(queryset, info.context.user)

    @classmethod
    def get_node(cls, info, id):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return GroupBeneficiary.objects.filter(pk=id).first()
        user = info.context.user
        if not _have_permissions(
            user,
            SocialProtectionConfig.gql_beneficiary_search_perms,
        ):
            return None
        return GroupBeneficiary.get_queryset(None, user).filter(pk=id).first()

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

    @classmethod
    def get_queryset(cls, queryset, info):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return queryset
        if not _have_permissions(
            info.context.user,
            SocialProtectionConfig.gql_beneficiary_search_perms,
        ):
            return queryset.none()
        return filter_queryset_by_benefit_plan(
            queryset,
            info.context.user,
        )

    @classmethod
    def get_node(cls, info, id):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return BenefitPlanDataUploadRecords.objects.filter(pk=id).first()
        user = info.context.user
        if not _have_permissions(
            user,
            SocialProtectionConfig.gql_beneficiary_search_perms,
        ):
            return None
        return cls.get_queryset(
            BenefitPlanDataUploadRecords.objects.all(),
            info,
        ).filter(pk=id).first()


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

    @classmethod
    def get_queryset(cls, queryset, info):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return queryset
        return filter_queryset_by_benefit_plan(
            queryset,
            info.context.user,
            "id",
        )

    @classmethod
    def get_node(cls, info, id):
        if not SocialProtectionConfig.enable_benefit_plan_access_control:
            return BenefitPlan.history.filter(pk=id).first()
        user = info.context.user
        if not _have_permissions(
            user,
            SocialProtectionConfig.gql_benefit_plan_search_perms,
        ):
            return None
        return cls.get_queryset(BenefitPlan.history.all(), info).filter(
            pk=id,
        ).first()

    def resolve_beneficiary_data_schema(self, info):
        perms = SocialProtectionConfig.gql_schema_search_perms
        if _have_permissions(info.context.user, perms):
            return self.beneficiary_data_schema
        return None

    def resolve_has_payment_plans(self, info):
        return PaymentPlan.objects.filter(benefit_plan_id=self.id).exists()
