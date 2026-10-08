import json

from django.contrib import admin
from django.test import SimpleTestCase

from social_protection.admin import BenefitPlanAdmin
from social_protection.forms import BenefitPlanEnrollmentConfigurationForm
from social_protection.models import BenefitPlan


class BenefitPlanEnrollmentConfigurationFormTest(SimpleTestCase):
    def benefit_plan(self):
        return BenefitPlan(
            code="RMEP",
            name="RMEP",
            type=BenefitPlan.BenefitPlanType.GROUP_TYPE,
            beneficiary_data_schema={
                "type": "object",
                "properties": {
                    "rmep_eligible": {"type": "boolean"},
                    "business_period": {"type": "number"},
                },
            },
            json_ext={"unrelated": {"preserve": True}},
        )

    def configuration(self):
        return {
            "advanced_criteria": {
                "ACTIVE": [{
                    "field": "rmep_eligible",
                    "filter": "exact",
                    "type": "boolean",
                    "value": True,
                }],
            },
            "enrolment_ranking": {
                "ACTIVE": {
                    "score_source": {
                        "type": "PRIMARY_WORKER",
                        "data_field": "individual.json_ext",
                    },
                    "weighted_score": {
                        "normalise_to": 100,
                        "components": [{
                            "field": "business_period",
                            "weight": 100,
                            "scoring": {
                                "type": "numeric_bands",
                                "bands": [{"min": 0, "points": 1}],
                                "maximum_points": 1,
                            },
                        }],
                    },
                    "order_by": ["-_weighted_score"],
                    "tie_breaker": "id",
                    "limit": {
                        "percentage": 100,
                        "respect_max_beneficiaries": True,
                    },
                },
            },
        }

    def test_admin_registers_benefit_plan_with_configuration_form(self):
        registered = admin.site._registry[BenefitPlan]
        self.assertIsInstance(registered, BenefitPlanAdmin)
        self.assertIs(registered.form, BenefitPlanEnrollmentConfigurationForm)

    def test_valid_configuration_merges_without_erasing_other_json(self):
        benefit_plan = self.benefit_plan()
        form = BenefitPlanEnrollmentConfigurationForm(
            data={
                "max_beneficiaries": 30,
                "enrollment_configuration": json.dumps(self.configuration()),
            },
            instance=benefit_plan,
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        result = form.save(commit=False)

        self.assertEqual(result.max_beneficiaries, 30)
        self.assertEqual(result.json_ext["unrelated"], {"preserve": True})
        self.assertEqual(
            result.json_ext["enrolment_ranking"],
            self.configuration()["enrolment_ranking"],
        )

    def test_invalid_weighted_field_is_rejected(self):
        benefit_plan = self.benefit_plan()
        configuration = self.configuration()
        configuration["enrolment_ranking"]["ACTIVE"]["weighted_score"][
            "components"
        ][0]["field"] = "unknown_field"
        form = BenefitPlanEnrollmentConfigurationForm(
            data={
                "max_beneficiaries": 30,
                "enrollment_configuration": json.dumps(configuration),
            },
            instance=benefit_plan,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("not defined", str(form.errors["enrollment_configuration"]))

    def test_clearing_configuration_preserves_unrelated_json(self):
        benefit_plan = self.benefit_plan()
        benefit_plan.json_ext.update(self.configuration())
        form = BenefitPlanEnrollmentConfigurationForm(
            data={
                "max_beneficiaries": "",
                "enrollment_configuration": "{}",
            },
            instance=benefit_plan,
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        result = form.save(commit=False)

        self.assertEqual(result.json_ext, {"unrelated": {"preserve": True}})
