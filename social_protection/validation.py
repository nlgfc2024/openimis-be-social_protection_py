import json

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from core.utils import validate_json_schema
from core.validation import BaseModelValidation, ObjectExistsValidationMixin
from individual.models import Group, Individual
from social_protection.models import Beneficiary, BenefitPlan, GroupBeneficiary
from social_protection.phase_defaults import advanced_criteria_validation_errors


class BenefitPlanValidation(BaseModelValidation, ObjectExistsValidationMixin):
    OBJECT_TYPE = BenefitPlan

    @classmethod
    def validate_create(cls, user, **data):
        errors = validate_benefit_plan(data)
        if errors:
            raise ValidationError(errors)
        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        uuid = data.get('id')
        errors = validate_benefit_plan(data, uuid)
        if errors:
            raise ValidationError(errors)
        super().validate_update(user, **data)

    @classmethod
    def validate_delete(cls, user, **data):
        super().validate_delete(user, **data)

    @classmethod
    def validate_undo_delete(cls, data):
        obj_id = data.get('id')
        cls.validate_object_exists(obj_id)
        obj = BenefitPlan.objects.get(id=obj_id)
        errors = [
            *validate_bf_unique_code(obj.code, obj_id),
            *validate_bf_unique_name(obj.name, obj_id),
        ]
        if errors:
            raise ValidationError(errors)


def validate_benefit_plan(data, uuid=None):
    validations = [
        *validate_not_empty_field(data.get("code"), "code"),
        *validate_bf_unique_code(data.get('code'), uuid),
        *validate_not_empty_field(data.get("name"), "name"),
        *validate_bf_unique_name(data.get('name'), uuid)
    ]

    existing = BenefitPlan.objects.filter(id=uuid).first() if uuid else None
    beneficiary_data_schema = data.get(
        'beneficiary_data_schema',
        existing.beneficiary_data_schema if existing else None,
    )
    if beneficiary_data_schema:
        validations.extend(validate_json_schema(beneficiary_data_schema))

    json_ext = data.get('json_ext', existing.json_ext if existing else None)
    if isinstance(json_ext, str):
        try:
            json_ext = json.loads(json_ext)
        except (TypeError, json.JSONDecodeError):
            validations.append({"message": "json_ext must be a JSON object."})
            json_ext = None
    if json_ext is not None and not isinstance(json_ext, dict):
        validations.append({"message": "json_ext must be a JSON object."})
    elif isinstance(json_ext, dict) and 'advanced_criteria' in json_ext:
        criteria_errors = advanced_criteria_validation_errors(
            json_ext['advanced_criteria'],
            beneficiary_data_schema,
            data.get('type', existing.type if existing else 'BenefitPlan'),
        )
        validations.extend({"message": error} for error in criteria_errors)

    return validations


def validate_bf_unique_code(code, uuid=None):
    instance = BenefitPlan.objects.filter(
        code=code, is_deleted=False
    ).exclude(id=uuid).first()
    if instance:
        msg = "social_protection.validation.benefit_plan.code_exists"
        return [{"message": _(msg % {'code': code})}]  # noqa: F504
    return []


def validate_bf_unique_name(name, uuid=None):
    instance = BenefitPlan.objects.filter(
        name=name, is_deleted=False
    ).exclude(id=uuid).first()
    if instance:
        msg = "social_protection.validation.benefit_plan.name_exists"
        return [{"message": _(msg % {'name': name})}]  # noqa: F504
    return []


def validate_not_empty_field(string, field):
    if not string:
        return [{"message": _("social_protection.validation.field_empty") % {
            'field': field
        }}]
    return []


def validate_beneficiary_data_source(benefit_plan_id, record):
    """A plan only takes people or households from its own data source, so programmes on one
    instance never share them (enrollment selection applies the same rule)."""
    from individual.data_source import DATA_SOURCE_FIELD, DEFAULT_DATA_SOURCE, benefit_plan_data_source

    benefit_plan = BenefitPlan.objects.filter(id=benefit_plan_id).first() if benefit_plan_id else None
    if not benefit_plan or record is None:
        return []
    plan_source = benefit_plan_data_source(benefit_plan)
    record_source = (record.json_ext or {}).get(DATA_SOURCE_FIELD) or DEFAULT_DATA_SOURCE
    if record_source == plan_source:
        return []
    return [{"message": _("This record comes from data source '%(record_source)s', but benefit plan "
                          "'%(plan)s' only takes data from '%(plan_source)s'.") % {
        "record_source": record_source, "plan": benefit_plan.code, "plan_source": plan_source,
    }}]


def _validate_beneficiary_source(data, model, record_model, record_field):
    existing = model.objects.filter(id=data.get("id")).first() if data.get("id") else None
    benefit_plan_id = data.get("benefit_plan_id") or (existing.benefit_plan_id if existing else None)
    record_id = data.get(f"{record_field}_id") or (getattr(existing, f"{record_field}_id") if existing else None)
    record = record_model.objects.filter(id=record_id).first() if record_id else None
    errors = validate_beneficiary_data_source(benefit_plan_id, record)
    if errors:
        raise ValidationError(errors)


class BeneficiaryValidation(BaseModelValidation):
    OBJECT_TYPE = Beneficiary

    @classmethod
    def validate_create(cls, user, **data):
        _validate_beneficiary_source(data, Beneficiary, Individual, "individual")
        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        _validate_beneficiary_source(data, Beneficiary, Individual, "individual")
        super().validate_update(user, **data)


class GroupBeneficiaryValidation(BaseModelValidation):
    OBJECT_TYPE = Beneficiary

    @classmethod
    def validate_create(cls, user, **data):
        _validate_beneficiary_source(data, GroupBeneficiary, Group, "group")
        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        _validate_beneficiary_source(data, GroupBeneficiary, Group, "group")
        super().validate_update(user, **data)
