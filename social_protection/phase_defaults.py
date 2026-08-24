import copy
from datetime import date
from decimal import Decimal, InvalidOperation

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
RANKING_STATUSES = BENEFICIARY_STATUSES | {"*"}
RANKING_CASTS = {"int", "float", "date", "str"}
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


def validate_mandatory_enrollment_criteria(criteria):
    """Validate live system criteria without coupling them to a Phase schema."""
    errors = []
    if not isinstance(criteria, dict):
        raise ValidationError({"config": [
            "mandatory_enrollment_criteria must be an object."
        ]})

    unexpected_types = set(criteria) - {"INDIVIDUAL", "GROUP"}
    if unexpected_types:
        errors.append(
            "Unsupported mandatory_enrollment_criteria types: "
            + ", ".join(sorted(unexpected_types))
        )

    for benefit_plan_type in ("INDIVIDUAL", "GROUP"):
        type_criteria = criteria.get(benefit_plan_type, {})
        _validate_advanced_criteria(
            f"mandatory_enrollment_criteria.{benefit_plan_type}",
            type_criteria,
            {},
            errors,
        )

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
    if isinstance(json_ext, dict) and "enrolment_ranking" in json_ext:
        _validate_enrolment_ranking(
            benefit_plan_type,
            json_ext["enrolment_ranking"],
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


def _validate_enrolment_ranking(benefit_plan_type, rankings, errors):
    prefix = f"{benefit_plan_type} enrolment_ranking"
    if not isinstance(rankings, dict):
        errors.append(f"{prefix} must be an object.")
        return
    for status, ranking in rankings.items():
        status_prefix = f"{prefix}.{status}"
        if status not in RANKING_STATUSES:
            errors.append(f"{prefix} has unsupported status {status}.")
            continue
        if not isinstance(ranking, dict):
            errors.append(f"{status_prefix} must be an object.")
            continue
        unexpected = set(ranking) - {"order_by", "tie_breaker", "limit"}
        if unexpected:
            errors.append(
                f"{status_prefix} has unsupported keys: {', '.join(sorted(unexpected))}."
            )
        order_by = ranking.get("order_by", [])
        if not isinstance(order_by, list):
            errors.append(f"{status_prefix}.order_by must be a list.")
        else:
            for index, item in enumerate(order_by):
                item_prefix = f"{status_prefix}.order_by[{index}]"
                if isinstance(item, str):
                    if not item or item == "-":
                        errors.append(f"{item_prefix} must name a field.")
                    continue
                if not isinstance(item, dict):
                    errors.append(f"{item_prefix} must be a string or object.")
                    continue
                unexpected_item = set(item) - {"field", "direction", "cast", "nulls"}
                if unexpected_item:
                    errors.append(
                        f"{item_prefix} has unsupported keys: "
                        f"{', '.join(sorted(unexpected_item))}."
                    )
                if not isinstance(item.get("field"), str) or not item.get("field"):
                    errors.append(f"{item_prefix}.field must be a non-empty string.")
                if item.get("direction", "asc") not in {"asc", "desc"}:
                    errors.append(f"{item_prefix}.direction must be asc or desc.")
                if item.get("cast") not in ({None} | RANKING_CASTS):
                    errors.append(f"{item_prefix}.cast is unsupported.")
                if item.get("nulls") not in {None, "first", "last"}:
                    errors.append(f"{item_prefix}.nulls must be first or last.")
        tie_breaker = ranking.get("tie_breaker", "id")
        if not isinstance(tie_breaker, str) or not tie_breaker:
            errors.append(f"{status_prefix}.tie_breaker must be a non-empty string.")
        limit = ranking.get("limit", {})
        if not isinstance(limit, dict):
            errors.append(f"{status_prefix}.limit must be an object.")
            continue
        unexpected_limit = set(limit) - {"percentage", "respect_max_beneficiaries"}
        if unexpected_limit:
            errors.append(
                f"{status_prefix}.limit has unsupported keys: "
                f"{', '.join(sorted(unexpected_limit))}."
            )
        percentage = limit.get("percentage")
        if percentage is not None and (
            isinstance(percentage, bool)
            or not isinstance(percentage, (int, float))
            or not 1 <= percentage <= 100
        ):
            errors.append(f"{status_prefix}.limit.percentage must be between 1 and 100.")
        respect_max = limit.get("respect_max_beneficiaries", True)
        if not isinstance(respect_max, bool):
            errors.append(
                f"{status_prefix}.limit.respect_max_beneficiaries must be boolean."
            )


def _validate_criterion(prefix, criterion, properties, errors):
    if not isinstance(criterion, dict):
        errors.append(f"{prefix} must be an object.")
        return
    if set(criterion) == {"custom_filter_condition"}:
        condition = criterion["custom_filter_condition"]
        if not isinstance(condition, str) or "=" not in condition:
            errors.append(f"{prefix}.custom_filter_condition is malformed.")
            return
        expression, value = condition.split("=", 1)
        parts = expression.rsplit("__", 2)
        if len(parts) == 3:
            field, filter_name, value_type = parts
        elif len(parts) == 2:
            field, value_type = parts
            filter_name = "exact"
        else:
            errors.append(f"{prefix}.custom_filter_condition is malformed.")
            return
        _validate_criterion_parts(
            prefix,
            field,
            filter_name,
            value_type,
            value,
            properties,
            errors,
        )
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

    _validate_criterion_parts(
        prefix,
        field,
        filter_name,
        value_type,
        criterion.get("value"),
        properties,
        errors,
    )


def _validate_criterion_parts(
    prefix,
    field,
    filter_name,
    value_type,
    value,
    properties,
    errors,
):
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
    elif not _value_can_be_cast(value, value_type):
        errors.append(
            f"{prefix}.value cannot be cast to {value_type}."
        )


def _value_can_be_cast(value, value_type):
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "boolean":
        return isinstance(value, bool) or (
            isinstance(value, str) and value.lower() in {"true", "false"}
        )
    if value_type == "integer":
        if isinstance(value, bool):
            return False
        try:
            return Decimal(str(value)) == int(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            return False
    if value_type in {"decimal", "number", "numeric"}:
        if isinstance(value, bool):
            return False
        try:
            Decimal(str(value))
            return True
        except (InvalidOperation, TypeError, ValueError):
            return False
    if value_type == "date":
        if isinstance(value, date):
            return True
        try:
            date.fromisoformat(value)
            return True
        except (TypeError, ValueError):
            return False
    return False
