import copy

from django.core.exceptions import ValidationError

from core.custom_filters import CustomFilterWizardInterface
from core.utils import validate_json_schema


DEFAULT_SECTIONS = {"common", "INDIVIDUAL", "GROUP"}
DEFAULT_FIELDS = {
    "beneficiary_data_schema",
    "json_ext",
    "max_beneficiaries",
    "ceiling_per_beneficiary",
    "institution",
    "description",
}
BENEFICIARY_STATUSES = {"POTENTIAL", "ACTIVE", "SUSPENDED", "GRADUATED"}
CRITERION_FIELDS = {"field", "filter", "type", "value"}
FILTERS_BY_TYPE = {
    **CustomFilterWizardInterface.FILTERS_BASED_ON_FIELD_TYPE,
    "string": [
        "exact", "iexact", "startswith", "istartswith", "contains", "icontains"
    ],
    "number": ["exact", "lt", "lte", "gt", "gte"],
    "numeric": ["exact", "lt", "lte", "gt", "gte"],
}


def deep_merge(original, override):
    """Return a defensive recursive merge without mutating either input."""
    merged = copy.deepcopy(original)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def build_benefit_plan_defaults(config, benefit_plan_type):
    defaults = config or {}
    common = defaults.get("common", {})
    type_defaults = defaults.get(benefit_plan_type, {})
    return deep_merge(common, type_defaults)


def apply_benefit_plan_creation_defaults(config, requested_data):
    configured = build_benefit_plan_defaults(
        config,
        requested_data.get("type", "INDIVIDUAL"),
    )
    permitted_defaults = {
        key: value for key, value in configured.items() if key in DEFAULT_FIELDS
    }
    return deep_merge(permitted_defaults, requested_data)


def validate_benefit_plan_creation_defaults(defaults):
    errors = []
    if not isinstance(defaults, dict):
        raise ValidationError({"config": [
            "benefit_plan_creation_defaults must be an object."
        ]})

    unexpected_sections = set(defaults) - DEFAULT_SECTIONS
    if unexpected_sections:
        section_names = ", ".join(sorted(unexpected_sections))
        errors.append(
            f"Unsupported benefit_plan_creation_defaults sections: {section_names}"
        )

    for section_name, section in defaults.items():
        if section_name not in DEFAULT_SECTIONS:
            continue
        if not isinstance(section, dict):
            errors.append(f"{section_name} defaults must be an object.")
            continue
        unexpected_fields = set(section) - DEFAULT_FIELDS
        if unexpected_fields:
            field_names = ", ".join(sorted(unexpected_fields))
            errors.append(
                f"Unsupported fields in {section_name} defaults: {field_names}"
            )

    for benefit_plan_type in ("INDIVIDUAL", "GROUP"):
        merged = build_benefit_plan_defaults(defaults, benefit_plan_type)
        _validate_merged_defaults(benefit_plan_type, merged, errors)

    if errors:
        raise ValidationError({"config": errors})


def advanced_criteria_validation_errors(
    criteria,
    schema=None,
    benefit_plan_type="BenefitPlan",
):
    errors = []
    _validate_advanced_criteria(
        benefit_plan_type,
        criteria,
        schema or {},
        errors,
    )
    return errors


def _validate_merged_defaults(benefit_plan_type, defaults, errors):
    schema = defaults.get("beneficiary_data_schema")
    if schema is not None:
        if not isinstance(schema, dict):
            errors.append(
                f"{benefit_plan_type} beneficiary_data_schema must be an object."
            )
        else:
            errors.extend(
                f"{benefit_plan_type} beneficiary_data_schema: {error['message']}"
                for error in validate_json_schema(schema)
            )

    json_ext = defaults.get("json_ext")
    if json_ext is not None and not isinstance(json_ext, dict):
        errors.append(f"{benefit_plan_type} json_ext must be an object.")
    elif isinstance(json_ext, dict) and "advanced_criteria" in json_ext:
        _validate_advanced_criteria(
            benefit_plan_type,
            json_ext["advanced_criteria"],
            schema or {},
            errors,
        )

    max_beneficiaries = defaults.get("max_beneficiaries")
    invalid_max_beneficiaries = any((
        not isinstance(max_beneficiaries, int),
        isinstance(max_beneficiaries, bool),
        isinstance(max_beneficiaries, int) and max_beneficiaries < 0,
    ))
    if max_beneficiaries is not None and invalid_max_beneficiaries:
        errors.append(
            f"{benefit_plan_type} max_beneficiaries must be a non-negative integer."
        )


def _validate_advanced_criteria(benefit_plan_type, criteria, schema, errors):
    if isinstance(criteria, list):
        criteria_by_status = {"POTENTIAL": criteria}
    elif isinstance(criteria, dict):
        criteria_by_status = criteria
    else:
        errors.append(
            f"{benefit_plan_type} advanced_criteria must be an object or legacy list."
        )
        return

    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    for status, status_criteria in criteria_by_status.items():
        if status not in BENEFICIARY_STATUSES:
            errors.append(
                f"{benefit_plan_type} advanced_criteria has unsupported status {status}."
            )
            continue
        if not isinstance(status_criteria, list):
            errors.append(
                f"{benefit_plan_type} advanced_criteria.{status} must be a list."
            )
            continue
        for index, criterion in enumerate(status_criteria):
            prefix = f"{benefit_plan_type} advanced_criteria.{status}[{index}]"
            _validate_criterion(prefix, criterion, properties, errors)


def _validate_criterion(prefix, criterion, properties, errors):
    if not isinstance(criterion, dict):
        errors.append(f"{prefix} must be an object.")
        return
    if set(criterion) == {"custom_filter_condition"}:
        condition = criterion["custom_filter_condition"]
        if not isinstance(condition, str) or "=" not in condition:
            errors.append(f"{prefix}.custom_filter_condition is malformed.")
        return

    missing = CRITERION_FIELDS - set(criterion)
    if missing:
        errors.append(f"{prefix} is missing: {', '.join(sorted(missing))}.")
        return

    field = criterion["field"]
    value_type = criterion["type"]
    filter_name = criterion["filter"]
    if not all(isinstance(criterion[key], str) for key in ("field", "filter", "type")):
        errors.append(f"{prefix} field, filter and type must be strings.")
        return

    schema_property = properties.get(field)
    if properties and schema_property is None:
        errors.append(f"{prefix}.field {field} is not defined in the beneficiary schema.")
        return
    schema_type = schema_property.get("type") if isinstance(schema_property, dict) else None
    if schema_type and value_type != schema_type:
        errors.append(
            f"{prefix}.type {value_type} does not match schema type {schema_type}."
        )

    supported_filters = FILTERS_BY_TYPE.get(value_type)
    if not supported_filters:
        errors.append(f"{prefix}.type {value_type} is unsupported.")
    elif filter_name not in supported_filters:
        errors.append(
            f"{prefix}.filter {filter_name} is unsupported for {value_type}."
        )
