import copy

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from core.test_helpers import LogInHelper
from social_protection.apps import SocialProtectionConfig
from social_protection.models import BenefitPlan
from social_protection.phase_defaults import (
    apply_benefit_plan_creation_defaults,
    deep_merge,
    validate_benefit_plan_creation_defaults,
)
from social_protection.services import BenefitPlanService
from social_protection.tests.data import service_add_payload_no_ext


class PhaseDefaultsValidationTest(SimpleTestCase):
    def test_deep_merge_does_not_mutate_inputs(self):
        original = {"json_ext": {"advanced_criteria": {"POTENTIAL": []}}}
        override = {"json_ext": {"source": "request"}}

        merged = deep_merge(original, override)
        merged["json_ext"]["advanced_criteria"]["POTENTIAL"].append("changed")

        self.assertEqual(original["json_ext"]["advanced_criteria"]["POTENTIAL"], [])
        self.assertEqual(override, {"json_ext": {"source": "request"}})

    def test_common_type_and_request_values_are_deep_merged(self):
        config = {
            "common": {
                "description": "Configured",
                "json_ext": {"advanced_criteria": {"POTENTIAL": []}},
            },
            "INDIVIDUAL": {
                "json_ext": {"advanced_criteria": {"ACTIVE": []}},
            },
            "GROUP": {},
        }

        result = apply_benefit_plan_creation_defaults(config, {
            "type": "INDIVIDUAL",
            "description": "Requested",
            "json_ext": {"source": "request"},
        })

        self.assertEqual(result["description"], "Requested")
        self.assertEqual(result["json_ext"]["source"], "request")
        self.assertEqual(
            result["json_ext"]["advanced_criteria"],
            {"POTENTIAL": [], "ACTIVE": []},
        )

    def test_rejects_internal_fields(self):
        with self.assertRaisesMessage(ValidationError, "Unsupported fields"):
            validate_benefit_plan_creation_defaults({
                "common": {"code": "SYSTEM"},
                "INDIVIDUAL": {},
                "GROUP": {},
            })

    def test_rejects_criteria_field_missing_from_schema(self):
        with self.assertRaisesMessage(ValidationError, "not defined"):
            validate_benefit_plan_creation_defaults({
                "common": {},
                "INDIVIDUAL": {
                    "beneficiary_data_schema": {
                        "properties": {"district": {"type": "string"}}
                    },
                    "json_ext": {
                        "advanced_criteria": {
                            "POTENTIAL": [{
                                "field": "validation_status",
                                "filter": "iexact",
                                "type": "string",
                                "value": "VERIFIED",
                            }]
                        }
                    },
                },
                "GROUP": {},
            })

    def test_accepts_legacy_potential_criteria_list(self):
        validate_benefit_plan_creation_defaults({
            "common": {
                "json_ext": {
                    "advanced_criteria": [{
                        "custom_filter_condition": "able_bodied__boolean=True"
                    }]
                }
            },
            "INDIVIDUAL": {},
            "GROUP": {},
        })


class BenefitPlanCreationDefaultsTest(TestCase):
    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api()
        self.service = BenefitPlanService(self.user)
        self.original_config = copy.deepcopy(
            SocialProtectionConfig.benefit_plan_creation_defaults
        )

    def tearDown(self):
        SocialProtectionConfig.benefit_plan_creation_defaults = self.original_config

    def test_service_applies_snapshot_defaults(self):
        configured_criteria = {
            "POTENTIAL": [{
                "field": "district",
                "filter": "iexact",
                "type": "string",
                "value": "Lilongwe",
            }]
        }
        SocialProtectionConfig.benefit_plan_creation_defaults = {
            "common": {
                "description": "Default description",
                "json_ext": {"advanced_criteria": configured_criteria},
            },
            "INDIVIDUAL": {"max_beneficiaries": 25},
            "GROUP": {"max_beneficiaries": 10},
        }

        payload = copy.deepcopy(service_add_payload_no_ext)
        payload.pop("max_beneficiaries")
        result = self.service.create(payload)

        self.assertTrue(result["success"], result.get("detail"))
        benefit_plan = BenefitPlan.objects.get(id=result["data"]["uuid"])
        self.assertEqual(benefit_plan.max_beneficiaries, 25)
        self.assertEqual(benefit_plan.description, "Default description")
        self.assertEqual(
            benefit_plan.json_ext["advanced_criteria"], configured_criteria
        )

        SocialProtectionConfig.benefit_plan_creation_defaults["common"][
            "json_ext"
        ]["advanced_criteria"]["POTENTIAL"][0]["value"] = "Blantyre"
        benefit_plan.refresh_from_db()
        self.assertEqual(
            benefit_plan.json_ext["advanced_criteria"]["POTENTIAL"][0]["value"],
            "Lilongwe",
        )
