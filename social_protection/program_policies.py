from django.core.exceptions import ValidationError


SUPPORTED_SELECTION_CRITERIA = {
    "data_source", "validation_status", "primary_worker", "fit_for_work",
    "business_experience", "willingness_to_participate", "Age_limit",
    "age_limit", "selection_strategy",
}


def validate_program_specific_enrolment_criteria(policies):
    """Validate the deployable policy shape before it is used at enrollment."""
    if not isinstance(policies, dict):
        raise ValidationError("program_specific_enrolment_criteria must be an object.")
    for code, policy in policies.items():
        if not isinstance(code, str) or not code:
            raise ValidationError("Program policy codes must be non-empty strings.")
        if not isinstance(policy, dict):
            raise ValidationError(f"Enrolment policy for Benefit Plan code {code} must be an object.")
        unexpected_policy = set(policy) - {"sorting_strategy", "selection_criteria"}
        if unexpected_policy:
            raise ValidationError(
                f"Unsupported policy keys for {code}: " + ", ".join(sorted(unexpected_policy))
            )
        sorting = policy.get("sorting_strategy", [])
        if not isinstance(sorting, list):
            raise ValidationError(f"sorting_strategy for {code} must be a list.")
        for index, rule in enumerate(sorting):
            if not isinstance(rule, dict) or set(rule) != {"key", "weight"}:
                raise ValidationError(
                    f"sorting_strategy[{index}] for {code} must contain key and weight."
                )
            if not isinstance(rule["key"], str) or not rule["key"]:
                raise ValidationError(f"sorting_strategy[{index}].key for {code} must be a string.")
            if not isinstance(rule["weight"], dict):
                raise ValidationError(f"sorting_strategy[{index}].weight for {code} must be an object.")
            for label, value in rule["weight"].items():
                if not isinstance(label, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValidationError(
                        f"sorting_strategy[{index}].weight for {code} must map labels to numbers."
                    )

        selection = policy.get("selection_criteria", {})
        policy_criteria(policy)
        strategy = selection.get("selection_strategy")
        if strategy is None:
            continue
        if not isinstance(strategy, dict):
            raise ValidationError(f"selection_strategy for {code} must be an object.")
        allowed_strategy = {
            "female", "youth", "female_headed", "youth_headed",
            "female_value", "youth_min_age", "youth_max_age",
        }
        unknown = set(strategy) - allowed_strategy
        if unknown:
            raise ValidationError(
                f"Unsupported selection_strategy keys for {code}: " + ", ".join(sorted(unknown))
            )
        female = strategy.get("female", strategy.get("female_headed"))
        youth = strategy.get("youth", strategy.get("youth_headed"))
        if female is None or youth is None:
            raise ValidationError(f"selection_strategy for {code} must define female and youth percentages.")
        for key in ("female", "youth", "female_headed", "youth_headed"):
            if key in strategy:
                value = strategy[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                    raise ValidationError(f"selection_strategy.{key} for {code} must be between 0 and 100.")


def _pwp_policy(policies):
    """Return the deployment PWP policy, with the application default as fallback."""
    policy = policies.get("PWP")
    if policy is None:
        from social_protection.apps import DEFAULT_CONFIG
        policy = DEFAULT_CONFIG["program_specific_enrolment_criteria"]["PWP"]
    if not isinstance(policy, dict):
        raise ValidationError("Enrolment policy for PWP must be an object.")
    return policy


def resolve_program_policy(benefit_plan):
    """Resolve the applicable runtime policy.

    Unless program-specific policies are explicitly enabled, every Benefit Plan
    uses PWP eligibility and selection. When enabled, a non-empty Benefit Plan
    code is mandatory and must be a key in the deployment configuration.
    """
    from social_protection.apps import SocialProtectionConfig

    policies = SocialProtectionConfig.program_specific_enrolment_criteria or {}
    if not isinstance(policies, dict):
        raise ValidationError("program_specific_enrolment_criteria must be an object.")

    if not SocialProtectionConfig.use_program_specific_enrolment_criteria:
        return _pwp_policy(policies)

    code = benefit_plan.code
    if not code:
        raise ValidationError(
            "A Benefit Plan code is required when program-specific enrolment criteria are enabled."
        )
    if code not in policies:
        raise ValidationError(
            f"No enrolment policy is configured for Benefit Plan code {code}."
        )
    policy = policies[code]
    if not isinstance(policy, dict):
        raise ValidationError(f"Enrolment policy for Benefit Plan code {code} must be an object.")
    return policy


def policy_criteria(policy):
    selection = policy.get("selection_criteria", {})
    if not isinstance(selection, dict):
        raise ValidationError("selection_criteria must be an object.")
    unsupported = set(selection) - SUPPORTED_SELECTION_CRITERIA
    if unsupported:
        raise ValidationError(
            "Unsupported selection_criteria keys: " + ", ".join(sorted(unsupported))
        )
    criteria = []
    mapping = {
        "data_source": ("beneficiary_data_source", "string"),
        "validation_status": ("validation_status", "string"),
        "primary_worker": ("is_primary_worker", "boolean"),
        "fit_for_work": ("fit_for_work", "integer"),
        "willingness_to_participate": ("willingness_to_participate", "boolean"),
    }
    for key, (field, value_type) in mapping.items():
        if key in selection:
            value = selection[key]
            if key == "fit_for_work" and isinstance(value, bool):
                value = int(value)
            criteria.append({
                "field": field,
                "filter": "exact",
                "type": value_type,
                "value": value,
            })
    if selection.get("business_experience") is True:
        criteria.append({
            "field": "business_experience",
            "filter": "exact",
            "type": "string",
            "value": "Yes",
        })
    age_limit = selection.get("Age_limit") or selection.get("age_limit")
    if age_limit:
        if not isinstance(age_limit, dict) or {"min_age", "max_age"} - set(age_limit):
            raise ValidationError("Age_limit must define min_age and max_age.")
        criteria.extend([
            {"field": "age", "filter": "gte", "type": "integer", "value": age_limit["min_age"]},
            {"field": "age", "filter": "lte", "type": "integer", "value": age_limit["max_age"]},
        ])
    return criteria


def runtime_policy_criteria(benefit_plan, status):
    """Social Protection programme rules exposed through the generic extension."""
    return policy_criteria(resolve_program_policy(benefit_plan)) if status == "ACTIVE" else []


def _weight_for(value, weights):
    if value is None:
        return 0
    if isinstance(value, (list, tuple, set)):
        return sum(_weight_for(item, weights) for item in value)
    for label, weight in weights.items():
        if label == str(value):
            return weight
        if isinstance(value, (int, float)):
            if label.startswith("< ") and value < float(label[2:].replace(" years", "").replace(" year", "")):
                return weight
            if label.startswith("> ") and value > float(label[2:].replace(" years", "").replace(" year", "")):
                return weight
            if "-" in label:
                try:
                    low, high = label.replace(" years", "").split("-")
                    if float(low) <= value <= float(high):
                        return weight
                except ValueError:
                    pass
    return 0


def _weighted_rows(queryset, policy):
    strategy = policy.get("sorting_strategy", [])
    rows = list(queryset.values("id", "json_ext"))
    for row in rows:
        data = row["json_ext"] or {}
        score = 0
        for rule in strategy:
            key = rule.get("key")
            score += _weight_for(data.get(key), rule.get("weight", {}))
        row["_policy_score"] = score
    return sorted(rows, key=lambda row: (-row["_policy_score"], str(row["id"])))


def runtime_policy_selection(queryset, benefit_plan, status, cap):
    """Return selected IDs in policy order, or ``None`` for generic ranking.

    A sorting strategy is meaningful even when no quota strategy is configured:
    UPG therefore returns every capped eligible candidate in weighted order.
    """
    if status != "ACTIVE":
        return None
    policy = resolve_program_policy(benefit_plan)
    has_sorting = bool(policy.get("sorting_strategy"))
    strategy = policy.get("selection_criteria", {}).get("selection_strategy")
    if not has_sorting and not strategy:
        return None

    rows = _weighted_rows(queryset, policy) if has_sorting else list(queryset.values("id", "json_ext"))
    if not strategy:
        return [row["id"] for row in rows[:cap]]
    if not isinstance(strategy, dict):
        raise ValidationError("selection_strategy must be an object.")

    female = strategy.get("female", strategy.get("female_headed"))
    youth = strategy.get("youth", strategy.get("youth_headed"))
    if female is None or youth is None:
        raise ValidationError("selection_strategy must define both female and youth percentages.")
    female_value = strategy.get("female_value", "famale")
    youth_min = strategy.get("youth_min_age", 18)
    youth_max = strategy.get("youth_max_age", 35)
    female_target = cap * female // 100
    youth_target = cap * youth // 100

    females = [row for row in rows if (row.get("json_ext") or {}).get("gender") == female_value]
    selected = females[:female_target]
    selected_ids = {row["id"] for row in selected}
    youth_rows = [
        row for row in rows
        if row["id"] not in selected_ids
        and youth_min <= (row.get("json_ext") or {}).get("age", -1) <= youth_max
    ]
    selected += youth_rows[:youth_target]
    selected_ids = {row["id"] for row in selected}
    others = [row for row in rows if row["id"] not in selected_ids]
    selected_ids = {row["id"] for row in selected}
    selected += [row for row in others if row["id"] not in selected_ids][:cap - len(selected)]
    return [row["id"] for row in selected[:cap]]
