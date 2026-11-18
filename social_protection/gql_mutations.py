import json

import graphene as graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core.gql.gql_mutations.base_mutation import (
    BaseHistoryModelCreateMutationMixin, BaseMutation,
    BaseHistoryModelUpdateMutationMixin, BaseHistoryModelDeleteMutationMixin
)
from core.schema import OpenIMISMutation
from core.models import User
from social_protection.benefit_plan_access import (
    has_all_benefit_plans_access,
    require_benefit_plan_assignment_management,
    require_benefit_plan_assignment_management_permission,
)
from social_protection.apps import SocialProtectionConfig
from social_protection.models import (
    BenefitPlan, Beneficiary, GroupBeneficiary, BeneficiaryStatus,
    BenefitPlanMutation, UserBenefitPlanAccess,
)
from social_protection.services import (
    BenefitPlanService,
    BeneficiaryService, GroupBeneficiaryService,
)


def check_perms_for_field(
    user,
    permission,
    data,
    field_string,
    error_message="mutation.lack_of_schema_perms",
):
    if field_string in data and not user.has_perms(permission):
        raise ValidationError(error_message)


def _json_object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    return value if isinstance(value, dict) else None


# json_ext keys only users with the Benefit Plan criteria rights may change.
CRITERIA_KEYS = {"advanced_criteria"}
# Rankings moved to the Enrolment module's programme profiles. A Phase no longer
# stores one, so the key is dropped whenever a Phase's json_ext is saved.
RETIRED_JSON_EXT_KEYS = {"enrolment_ranking"}


def _submitted_criteria_config(data):
    if "json_ext" not in data:
        return False, None
    json_ext = _json_object(data.get("json_ext"))
    submitted_keys = CRITERIA_KEYS.intersection(json_ext or {})
    if json_ext is None or not submitted_keys:
        return False, None
    return True, {key: json_ext[key] for key in submitted_keys}


def check_criteria_perms(user, permission, data, current=None):
    submitted, criteria_config = _submitted_criteria_config(data)
    if not submitted:
        return
    current_json_ext = _json_object(current.json_ext) if current else None
    current_config = {
        key: (current_json_ext or {}).get(key)
        for key in criteria_config
    }
    if criteria_config != current_config and not user.has_perms(permission):
        raise ValidationError("mutation.lack_of_criteria_perms")


def preserve_hidden_json_ext(user, data, current):
    """Merge criteria-only updates without erasing undisclosed extension data."""
    submitted = _json_object(data.get("json_ext"))
    submitted_keys = CRITERIA_KEYS.intersection(submitted or {})
    if submitted is None or not submitted_keys or not current:
        return
    if user.has_perms(SocialProtectionConfig.gql_schema_update_perms):
        return
    current_json_ext = _json_object(current.json_ext) or {}
    data["json_ext"] = {
        **current_json_ext,
        **{key: submitted[key] for key in submitted_keys},
    }


def drop_retired_json_ext(data, current=None):
    """Remove retired keys from the json_ext being saved, submitted or merged.

    An update that sends no json_ext still clears them from the stored one, so
    saving a Phase clears a stored enrolment_ranking whatever the user can see.
    """
    if "json_ext" in data:
        json_ext = _json_object(data.get("json_ext"))
    else:
        json_ext = _json_object(current.json_ext) if current else None
    if json_ext is None or not RETIRED_JSON_EXT_KEYS.intersection(json_ext):
        return
    data["json_ext"] = {
        key: value for key, value in json_ext.items()
        if key not in RETIRED_JSON_EXT_KEYS
    }


class CreateBenefitPlanInputType(OpenIMISMutation.Input):
    class BenefitPlanTypeEnum(graphene.Enum):
        INDIVIDUAL = BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE
        GROUP = BenefitPlan.BenefitPlanType.GROUP_TYPE

    code = graphene.String(required=False, max_length=9)
    name = graphene.String(required=True, max_length=255)
    max_beneficiaries = graphene.Int(default_value=None)
    ceiling_per_beneficiary = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    institution = graphene.String(required=False, max_length=255)
    beneficiary_data_schema = graphene.types.json.JSONString(required=False)
    type = graphene.Field(BenefitPlanTypeEnum, required=True)

    date_valid_from = graphene.Date(required=True)
    date_valid_to = graphene.Date(required=True)
    json_ext = graphene.types.json.JSONString(required=False)
    description = graphene.String(required=False, max_length=1024)

    def resolve_type(self, info):
        return self.type


class UpdateBenefitPlanInputType(CreateBenefitPlanInputType):
    id = graphene.UUID(required=True)


class AssignBenefitPlanUserInputType(OpenIMISMutation.Input):
    benefit_plan_id = graphene.UUID(required=True)
    user_id = graphene.UUID(required=True)


class RevokeBenefitPlanUserAccessInputType(OpenIMISMutation.Input):
    id = graphene.UUID(required=True)


class AssignBenefitPlanUserMutation(BaseMutation):
    _mutation_class = "AssignBenefitPlanUserMutation"
    _mutation_module = "social_protection"
    _model = UserBenefitPlanAccess

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not getattr(user, "id", None):
            raise PermissionDenied(_("mutation.authentication_required"))

        benefit_plan_id = data.get("benefit_plan_id")
        require_benefit_plan_assignment_management_permission(user)
        if not BenefitPlan.objects.filter(
            id=benefit_plan_id,
            is_deleted=False,
        ).exists():
            raise ValidationError("mutation.benefit_plan_not_found")
        require_benefit_plan_assignment_management(user, benefit_plan_id)
        target_user = User.objects.select_related(
            "i_user", "t_user", "officer", "claim_admin",
        ).filter(id=data.get("user_id")).first()
        underlying_user = target_user._u if target_user else None
        interactive_user = target_user.i_user if target_user else None
        interactive_user_expired = (
            interactive_user is not None
            and interactive_user.validity_to is not None
            and interactive_user.validity_to < timezone.now()
        )
        if (
            target_user is None
            or underlying_user is None
            or not target_user.is_active
            or interactive_user_expired
        ):
            raise ValidationError("mutation.user_not_found")
        if UserBenefitPlanAccess.objects.filter(
            user_id=data.get("user_id"),
            benefit_plan_id=benefit_plan_id,
            is_deleted=False,
        ).exists():
            raise ValidationError("mutation.benefit_plan_user_assignment_exists")

    @classmethod
    def _mutate(cls, user, **data):
        assignment = UserBenefitPlanAccess(
            user_id=data["user_id"],
            benefit_plan_id=data["benefit_plan_id"],
        )
        try:
            with transaction.atomic():
                assignment.save(user=user)
        except IntegrityError as exc:
            raise ValidationError(
                "mutation.benefit_plan_user_assignment_exists"
            ) from exc
        return None

    class Input(AssignBenefitPlanUserInputType):
        pass


class RevokeBenefitPlanUserAccessMutation(BaseMutation):
    _mutation_class = "RevokeBenefitPlanUserAccessMutation"
    _mutation_module = "social_protection"
    _model = UserBenefitPlanAccess

    @classmethod
    def _active_assignment(cls, assignment_id):
        return UserBenefitPlanAccess.objects.filter(
            id=assignment_id,
            is_deleted=False,
        ).first()

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not getattr(user, "id", None):
            raise PermissionDenied(_("mutation.authentication_required"))
        require_benefit_plan_assignment_management_permission(user)

    @classmethod
    def _mutate(cls, user, **data):
        require_benefit_plan_assignment_management_permission(user)
        assignment = cls._active_assignment(data["id"])
        if assignment is None:
            raise ValidationError(
                "mutation.benefit_plan_user_assignment_not_found"
            )
        require_benefit_plan_assignment_management(
            user,
            assignment.benefit_plan_id,
        )
        if (
            SocialProtectionConfig.enable_benefit_plan_access_control
            and assignment.user_id == user.id
            and not has_all_benefit_plans_access(user)
        ):
            raise ValidationError(
                "mutation.benefit_plan_user_assignment_self_revoke"
            )
        assignment.delete(user=user)
        return None

    class Input(RevokeBenefitPlanUserAccessInputType):
        pass


class CreateGenericBeneficiaryInputType(OpenIMISMutation.Input):
    class BeneficiaryStatusEnum(graphene.Enum):
        POTENTIAL = BeneficiaryStatus.POTENTIAL
        ACTIVE = BeneficiaryStatus.ACTIVE
        GRADUATED = BeneficiaryStatus.GRADUATED
        SUSPENDED = BeneficiaryStatus.SUSPENDED

    status = graphene.Field(BeneficiaryStatusEnum, required=True)
    benefit_plan_id = graphene.UUID(required=False)

    date_valid_from = graphene.Date(required=False)
    date_valid_to = graphene.Date(required=False)
    json_ext = graphene.types.json.JSONString(required=False)

    def resolve_status(self, info):
        return self.status


class UpdateGenericBeneficiaryInputType(CreateGenericBeneficiaryInputType):
    id = graphene.UUID(required=True)


class CreateBeneficiaryInputType(CreateGenericBeneficiaryInputType):
    individual_id = graphene.UUID(required=False)


class CreateGroupBeneficiaryInputType(CreateGenericBeneficiaryInputType):
    group_id = graphene.UUID(required=False)


class UpdateBeneficiaryInputType(UpdateGenericBeneficiaryInputType):
    pass


class UpdateGroupBeneficiaryInputType(UpdateGenericBeneficiaryInputType):
    pass


class CreateBenefitPlanMutation(
    BaseHistoryModelCreateMutationMixin, BaseMutation
):
    _mutation_class = "CreateBenefitPlanMutation"
    _mutation_module = "social_protection"
    _model = BenefitPlan

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                SocialProtectionConfig.gql_benefit_plan_create_perms):
            raise ValidationError("mutation.authentication_required")
        check_perms_for_field(
            user, SocialProtectionConfig.gql_schema_create_perms,
            data, 'beneficiary_data_schema', "mutation.lack_of_schema_perms"
        )
        check_criteria_perms(
            user, SocialProtectionConfig.gql_benefit_plan_criteria_update_perms,
            data,
        )

    @classmethod
    def _mutate(cls, user, **data):
        client_mutation_id = data.pop('client_mutation_id', None)
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        drop_retired_json_ext(data)
        service = BenefitPlanService(user)
        res = service.create(data)
        if client_mutation_id and res['success']:
            payroll_id = res['data']['id']
            benefit_plan = BenefitPlan.objects.get(id=payroll_id)
            BenefitPlanMutation.object_mutated(
                user, client_mutation_id=client_mutation_id,
                benefit_plan=benefit_plan
            )
        if not res['success']:
            return res
        return None

    class Input(CreateBenefitPlanInputType):
        pass


class UpdateBenefitPlanMutation(
    BaseHistoryModelUpdateMutationMixin, BaseMutation
):
    _mutation_class = "UpdateBenefitPlanMutation"
    _mutation_module = "social_protection"
    _model = BenefitPlan

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if type(user) is AnonymousUser or not user.has_perms(
                SocialProtectionConfig.gql_benefit_plan_update_perms):
            raise ValidationError("mutation.authentication_required")
        check_perms_for_field(
            user, SocialProtectionConfig.gql_schema_update_perms,
            data, 'beneficiary_data_schema', "mutation.lack_of_schema_perms"
        )
        current = BenefitPlan.objects.filter(id=data.get("id")).first()
        check_criteria_perms(
            user, SocialProtectionConfig.gql_benefit_plan_criteria_update_perms,
            data, current,
        )

    @classmethod
    def _mutate(cls, user, **data):
        if "date_valid_to" not in data:
            data['date_valid_to'] = None
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        current = BenefitPlan.objects.filter(id=data.get("id")).first()
        preserve_hidden_json_ext(user, data, current)
        drop_retired_json_ext(data, current)

        service = BenefitPlanService(user)
        if SocialProtectionConfig.gql_check_benefit_plan_update:
            if 'max_beneficiaries' not in data:
                data['max_beneficiaries'] = None
            res = service.create_update_task(data)
        else:
            res = service.update(data)

        return res if not res['success'] else None

    class Input(UpdateBenefitPlanInputType):
        pass


class DeleteBenefitPlanMutation(
    BaseHistoryModelDeleteMutationMixin, BaseMutation
):
    _mutation_class = "DeleteBenefitPlanMutation"
    _mutation_module = "social_protection"
    _model = BenefitPlan

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                SocialProtectionConfig.gql_benefit_plan_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = BenefitPlanService(user)
        ids = data.get('ids')
        if not ids:
            return {
                'success': False, 'message': 'No IDs to delete', 'details': ''
            }

        with transaction.atomic():
            for obj_id in ids:
                res = service.delete({'id': obj_id, 'user': user})
                if not res['success']:
                    transaction.rollback()
                    return res

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class UndoDeleteBenefitPlanMutation(
    BaseHistoryModelDeleteMutationMixin, BaseMutation
):
    _mutation_class = "UndoDeleteBenefitPlanMutation"
    _mutation_module = "social_protection"
    _model = BenefitPlan

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                SocialProtectionConfig.gql_benefit_plan_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = BenefitPlanService(user)
        ids = data.get('ids')
        if not ids:
            return {
                'success': False,
                'message': 'No IDs to undo delete',
                'details': ''
            }

        with transaction.atomic():
            for obj_id in ids:
                res = service.undo_delete({'id': obj_id})
                if not res['success']:
                    return res

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class CreateBeneficiaryMutation(
    BaseHistoryModelCreateMutationMixin, BaseMutation
):
    _mutation_class = "CreateBeneficiaryMutation"
    _mutation_module = "social_protection"
    _model = Beneficiary

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                SocialProtectionConfig.gql_beneficiary_create_perms):
            raise ValidationError("mutation.authentication_required")
        check_perms_for_field(
            user, SocialProtectionConfig.gql_schema_create_perms,
            data, 'json_ext'
        )

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = BeneficiaryService(user)
        if SocialProtectionConfig.gql_check_beneficiary_crud:
            res = service.create_create_task(data)
        else:
            res = service.create(data)

        return res if not res['success'] else None

    class Input(CreateBeneficiaryInputType):
        pass


class UpdateBeneficiaryMutation(
    BaseHistoryModelUpdateMutationMixin, BaseMutation
):
    _mutation_class = "UpdateBeneficiaryMutation"
    _mutation_module = "social_protection"
    _model = Beneficiary

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if type(user) is AnonymousUser or not user.has_perms(
                SocialProtectionConfig.gql_beneficiary_update_perms):
            raise ValidationError("mutation.authentication_required")
        check_perms_for_field(
            user, SocialProtectionConfig.gql_schema_update_perms,
            data, 'json_ext'
        )

    @classmethod
    def _mutate(cls, user, **data):
        if "date_valid_to" not in data:
            data['date_valid_to'] = None
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = BeneficiaryService(user)
        if SocialProtectionConfig.gql_check_beneficiary_crud:
            res = service.create_update_task(data)
        else:
            res = service.update(data)

        return res if not res['success'] else None

    class Input(UpdateBeneficiaryInputType):
        pass


class DeleteBeneficiaryMutation(
    BaseHistoryModelDeleteMutationMixin, BaseMutation
):
    _mutation_class = "DeleteBeneficiaryMutation"
    _mutation_module = "social_protection"
    _model = Beneficiary

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                SocialProtectionConfig.gql_beneficiary_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = BeneficiaryService(user)

        ids = data.get('ids')
        if not ids:
            return {
                'success': False, 'message': 'No IDs to delete', 'details': ''
            }

        with transaction.atomic():
            for obj_id in ids:
                if SocialProtectionConfig.gql_check_beneficiary_crud:
                    res = service.create_delete_task({'id': obj_id})
                else:
                    res = service.delete({'id': obj_id, 'user': user})
                if not res['success']:
                    transaction.rollback()
                    return res

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class CloseBenefitPlanMutation(
    BaseHistoryModelDeleteMutationMixin, BaseMutation
):
    _mutation_class = "CloseBenefitPlanMutation"
    _mutation_module = "social_protection"
    _model = BenefitPlan

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                SocialProtectionConfig.gql_benefit_plan_close_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = BenefitPlanService(user)
        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for id in ids:
                    service.close_benefit_plan({'id': id})

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class CreateGroupBeneficiaryMutation(
    BaseHistoryModelCreateMutationMixin, BaseMutation
):
    _mutation_class = "CreateGroupBeneficiaryMutation"
    _mutation_module = "social_protection"
    _model = GroupBeneficiary

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                SocialProtectionConfig.gql_beneficiary_create_perms):
            raise ValidationError("mutation.authentication_required")
        check_perms_for_field(
            user, SocialProtectionConfig.gql_schema_create_perms,
            data, 'json_ext'
        )

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupBeneficiaryService(user)
        if SocialProtectionConfig.gql_check_group_beneficiary_crud:
            res = service.create_create_task(data)
        else:
            res = service.create(data)

        return res if not res['success'] else None

    class Input(CreateGroupBeneficiaryInputType):
        pass


class UpdateGroupBeneficiaryMutation(
    BaseHistoryModelUpdateMutationMixin, BaseMutation
):
    _mutation_class = "UpdateGroupBeneficiaryMutation"
    _mutation_module = "social_protection"
    _model = GroupBeneficiary

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if type(user) is AnonymousUser or not user.has_perms(
                SocialProtectionConfig.gql_beneficiary_update_perms):
            raise ValidationError("mutation.authentication_required")
        check_perms_for_field(
            user, SocialProtectionConfig.gql_schema_update_perms,
            data, 'json_ext'
        )

    @classmethod
    def _mutate(cls, user, **data):
        if "date_valid_to" not in data:
            data['date_valid_to'] = None
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupBeneficiaryService(user)
        if SocialProtectionConfig.gql_check_group_beneficiary_crud:
            res = service.create_update_task(data)
        else:
            res = service.update(data)

        return res if not res['success'] else None

    class Input(UpdateGroupBeneficiaryInputType):
        pass


class DeleteGroupBeneficiaryMutation(
    BaseHistoryModelDeleteMutationMixin, BaseMutation
):
    _mutation_class = "DeleteGroupBeneficiaryMutation"
    _mutation_module = "social_protection"
    _model = GroupBeneficiary

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                SocialProtectionConfig.gql_beneficiary_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupBeneficiaryService(user)

        ids = data.get('ids')
        if not ids:
            return {
                'success': False, 'message': 'No IDs to delete', 'details': ''
            }

        with transaction.atomic():
            for obj_id in ids:
                if SocialProtectionConfig.gql_check_group_beneficiary_crud:
                    res = service.create_delete_task({'id': obj_id})
                else:
                    res = service.delete({'id': obj_id, 'user': user})
                if not res['success']:
                    transaction.rollback()
                    return res

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)
