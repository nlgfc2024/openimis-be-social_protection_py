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
            "enrolment_ranking": {"*": {"order_by": ["id"]}},
            "private_value": "preserved",
        })

        preserve_hidden_json_ext(user, data, current)

        self.assertEqual(data["json_ext"]["private_value"], "preserved")
        self.assertEqual(data["json_ext"]["advanced_criteria"], {"ACTIVE": []})
        self.assertEqual(
            data["json_ext"]["enrolment_ranking"],
            {"*": {"order_by": ["id"]}},
        )

    def test_ranking_change_requires_criteria_permission(self):
        user = Mock()
        user.has_perms.return_value = False
        current = SimpleNamespace(
            json_ext={"enrolment_ranking": {"*": {"order_by": ["id"]}}}
        )
        with self.assertRaisesMessage(ValidationError, "lack_of_criteria_perms"):
            check_criteria_perms(
                user,
                ["171006"],
                {"json_ext": {"enrolment_ranking": {"*": {"order_by": ["-id"]}}}},
                current,
            )

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

    def test_ranking_resolver_uses_criteria_permission_without_schema_permission(self):
        user = Mock(id=1)
        user.has_perms.side_effect = lambda perms: perms == ["171005"]
        ranking = {"*": {"order_by": ["id"]}}
        benefit_plan = SimpleNamespace(json_ext={"enrolment_ranking": ranking})
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        self.assertEqual(
            BenefitPlanGQLType.resolve_enrolment_ranking(benefit_plan, info),
            ranking,
        )
        self.assertIsNone(BenefitPlanGQLType.resolve_json_ext(benefit_plan, info))

    def test_ranking_resolver_hides_ranking_without_criteria_permission(self):
        user = Mock(id=1)
        user.has_perms.return_value = False
        benefit_plan = SimpleNamespace(
            json_ext={"enrolment_ranking": {"*": {"order_by": ["id"]}}}
        )
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        self.assertIsNone(
            BenefitPlanGQLType.resolve_enrolment_ranking(benefit_plan, info)
        )

    def test_json_ext_resolver_strips_criteria_without_criteria_permission(self):
        user = Mock(id=1)
        user.has_perms.side_effect = lambda perms: perms == ["171001"]
        benefit_plan = SimpleNamespace(json_ext={
            "advanced_criteria": {"POTENTIAL": [{"field": "private"}]},
            "enrolment_ranking": {"*": {"order_by": ["id"]}},
            "public_value": "preserved",
        })
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        result = BenefitPlanGQLType.resolve_json_ext(benefit_plan, info)

        self.assertEqual(result, {"public_value": "preserved"})

    def test_json_ext_resolver_returns_full_value_with_both_permissions(self):
        user = Mock(id=1)
        user.has_perms.return_value = True
        json_ext = {
            "advanced_criteria": {"POTENTIAL": []},
            "public_value": "preserved",
        }
        benefit_plan = SimpleNamespace(json_ext=json_ext)
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        result = BenefitPlanGQLType.resolve_json_ext(benefit_plan, info)

        self.assertEqual(result, json_ext)
