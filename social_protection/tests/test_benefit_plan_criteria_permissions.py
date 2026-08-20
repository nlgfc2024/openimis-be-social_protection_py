from types import SimpleNamespace
from unittest.mock import Mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from social_protection.gql_mutations import (
    check_criteria_perms,
    check_perms_for_field,
    preserve_hidden_json_ext,
)
from social_protection.gql_queries import BenefitPlanGQLType


class BenefitPlanCriteriaPermissionTest(SimpleTestCase):
    def test_criteria_only_update_preserves_hidden_json_ext(self):
        user = Mock()
        user.has_perms.return_value = False
        data = {"json_ext": {"advanced_criteria": {"ACTIVE": []}}}
        current = SimpleNamespace(json_ext={
            "advanced_criteria": {"POTENTIAL": []},
            "private_value": "preserved",
        })

        preserve_hidden_json_ext(user, data, current)

        self.assertEqual(data["json_ext"]["private_value"], "preserved")
        self.assertEqual(data["json_ext"]["advanced_criteria"], {"ACTIVE": []})

    def test_empty_schema_still_requires_permission(self):
        user = Mock()
        user.has_perms.return_value = False
        with self.assertRaisesMessage(ValidationError, "lack_of_schema_perms"):
            check_perms_for_field(
                user,
                ["171003"],
                {"beneficiary_data_schema": {}},
                "beneficiary_data_schema",
            )

    def test_explicit_criteria_clear_requires_permission(self):
        user = Mock()
        user.has_perms.return_value = False
        current = SimpleNamespace(
            json_ext={"advanced_criteria": {"POTENTIAL": [{"field": "x"}]}}
        )
        with self.assertRaisesMessage(ValidationError, "lack_of_criteria_perms"):
            check_criteria_perms(
                user,
                ["171006"],
                {"json_ext": {"advanced_criteria": {}}},
                current,
            )

    def test_unchanged_criteria_do_not_require_permission(self):
        user = Mock()
        user.has_perms.return_value = False
        criteria = {"POTENTIAL": []}
        current = SimpleNamespace(json_ext={"advanced_criteria": criteria})
        check_criteria_perms(
            user,
            ["171006"],
            {"json_ext": {"advanced_criteria": criteria, "other": "value"}},
            current,
        )

    def test_criteria_resolver_returns_only_criteria(self):
        user = Mock(id=1)
        user.has_perms.return_value = True
        benefit_plan = SimpleNamespace(json_ext={
            "advanced_criteria": {"POTENTIAL": []},
            "private_value": "not returned",
        })
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        result = BenefitPlanGQLType.resolve_advanced_criteria(benefit_plan, info)

        self.assertEqual(result, {"POTENTIAL": []})

    def test_criteria_resolver_hides_criteria_without_permission(self):
        user = Mock(id=1)
        user.has_perms.return_value = False
        benefit_plan = SimpleNamespace(
            json_ext={"advanced_criteria": {"POTENTIAL": []}}
        )
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        result = BenefitPlanGQLType.resolve_advanced_criteria(benefit_plan, info)

        self.assertIsNone(result)
