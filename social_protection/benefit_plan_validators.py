"""Extension registry for optional Benefit Plan validators."""

from collections import OrderedDict


_VALIDATORS = OrderedDict()


def register_benefit_plan_validator(name, validator):
    """Register or replace a named validator supplied by an optional module."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Benefit Plan validator name must be a non-empty string.")
    if not callable(validator):
        raise TypeError("Benefit Plan validator must be callable.")
    _VALIDATORS[name] = validator


def unregister_benefit_plan_validator(name):
    """Remove a validator, primarily for app teardown and isolated tests."""
    _VALIDATORS.pop(name, None)


def run_benefit_plan_validators(data, existing=None):
    """Return validation errors from a stable snapshot of registered callbacks."""
    errors = []
    for validator in tuple(_VALIDATORS.values()):
        validator_errors = validator(data, existing)
        if validator_errors:
            errors.extend(validator_errors)
    return errors
