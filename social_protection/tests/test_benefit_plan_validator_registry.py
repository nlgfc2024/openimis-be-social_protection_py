from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from social_protection import benefit_plan_validators
from social_protection.phase_defaults import validate_benefit_plan_creation_defaults
from social_protection.validation import (
    register_benefit_plan_validator,
    validate_benefit_plan,
)


class BenefitPlanValidatorRegistryTest(SimpleTestCase):
    def _validate(self, **overrides):
        data = {
            "code": "PLAN01",
            "name": "Plan",
            "type": "INDIVIDUAL",
            "json_ext": {},
            **overrides,
        }
        with (
            patch(
                "social_protection.validation.validate_bf_unique_code",
                return_value=[],
            ),
            patch(
                "social_protection.validation.validate_bf_unique_name",
                return_value=[],
            ),
        ):
            return validate_benefit_plan(data)

    def test_registered_validator_errors_reach_benefit_plan_validation(self):
        def validator(data, existing=None):
            self.assertIsNone(existing)
            self.assertEqual(data["code"], "PLAN01")
            return [{"message": "optional validator rejected the plan"}]

        with patch.dict(benefit_plan_validators._VALIDATORS, clear=True):
            register_benefit_plan_validator("test.optional", validator)

            self.assertIn(
                {"message": "optional validator rejected the plan"},
                self._validate(),
            )

    def test_creation_defaults_use_registered_validators(self):
        def validator(data, existing=None):
            if data["type"] == "GROUP":
                return [{"message": "invalid group defaults"}]
            return []

        with patch.dict(benefit_plan_validators._VALIDATORS, clear=True):
            register_benefit_plan_validator("test.defaults", validator)

            with self.assertRaisesMessage(
                ValidationError,
                "invalid group defaults",
            ):
                validate_benefit_plan_creation_defaults({
                    "common": {},
                    "INDIVIDUAL": {},
                    "GROUP": {},
                })

    def test_ranking_data_is_accepted_when_no_optional_validator_is_installed(self):
        with patch.dict(benefit_plan_validators._VALIDATORS, clear=True):
            errors = self._validate(json_ext={
                "enrolment_ranking": {
                    "ACTIVE": {"implementation_owned": "by-enrolment"},
                },
            })

        self.assertEqual(errors, [])

    def test_creation_defaults_validate_without_enrolment_installed(self):
        with patch.dict(benefit_plan_validators._VALIDATORS, clear=True):
            validate_benefit_plan_creation_defaults({
                "common": {
                    "json_ext": {
                        "enrolment_ranking": {
                            "ACTIVE": {"implementation_owned": "by-enrolment"},
                        },
                    },
                },
                "INDIVIDUAL": {},
                "GROUP": {},
            })
