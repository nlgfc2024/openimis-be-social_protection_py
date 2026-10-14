from unittest.mock import patch

from django.test import SimpleTestCase

from social_protection.validation import validate_benefit_plan


class TestBenefitPlanRankingValidation(SimpleTestCase):
    def _validate(self, **overrides):
        data = {
            "code": "RANK01",
            "name": "Ranked plan",
            "type": "INDIVIDUAL",
            "beneficiary_data_schema": {
                "type": "object",
                "properties": {"score": {"type": "number"}},
            },
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

    def test_rejects_invalid_ranking_on_benefit_plan_write(self):
        errors = self._validate(json_ext={
            "enrolment_ranking": {
                "ACTIVE": {
                    "order_by": [{"field": ["score"]}],
                },
            },
        })

        messages = [error["message"] for error in errors]
        self.assertTrue(
            any(
                "field must be a non-empty string" in message
                for message in messages
            ),
            messages,
        )

    def test_rejects_unknown_self_weighted_field_on_benefit_plan_write(self):
        errors = self._validate(json_ext={
            "enrolment_ranking": {
                "ACTIVE": {
                    "weighted_score": {
                        "components": [{
                            "field": "unknown_score",
                            "weight": 100,
                            "scoring": {
                                "type": "mapping",
                                "values": {"high": 1},
                            },
                        }],
                    },
                    "order_by": ["-_weighted_score"],
                },
            },
        })

        self.assertTrue(
            any(
                "not defined in the beneficiary schema" in error["message"]
                for error in errors
            ),
            errors,
        )

    def test_group_primary_worker_fields_skip_group_schema_validation(self):
        errors = self._validate(
            type="GROUP",
            beneficiary_data_schema={
                "type": "object",
                "properties": {"household_size": {"type": "integer"}},
            },
            json_ext={
                "enrolment_ranking": {
                    "ACTIVE": {
                        "score_source": {
                            "type": "PRIMARY_WORKER",
                            "data_field": "individual.json_ext",
                        },
                        "weighted_score": {
                            "components": [{
                                "field": "business_experience",
                                "weight": 100,
                                "scoring": {
                                    "type": "mapping",
                                    "values": {"Yes": 1},
                                },
                            }],
                        },
                        "order_by": ["-_weighted_score"],
                    },
                },
            },
        )

        self.assertEqual(errors, [])
