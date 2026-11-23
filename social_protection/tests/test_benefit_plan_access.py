from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from core.test_helpers import create_test_technical_user
from social_protection.apps import DEFAULT_CONFIG, SocialProtectionConfig
from social_protection.benefit_plan_access import (
    benefit_plan_access_control_enabled,
    can_access_benefit_plan,
    can_manage_benefit_plan_assignments,
    filter_queryset_by_benefit_plan,
    get_accessible_benefit_plan_ids,
    has_all_benefit_plans_access,
    require_benefit_plan_access,
)
from social_protection.models import BenefitPlan, UserBenefitPlanAccess


class BenefitPlanAccessConfigTest(SimpleTestCase):
    def test_defaults_are_disabled_and_permissions_are_separate(self):
        self.assertFalse(DEFAULT_CONFIG["enable_benefit_plan_access_control"])
        self.assertEqual(DEFAULT_CONFIG["gql_benefit_plan_all_access_perms"], ["160006"])
        self.assertEqual(
            DEFAULT_CONFIG["gql_benefit_plan_assignment_manage_perms"],
            ["160007"],
        )
        self.assertNotIn(
            "160003",
            DEFAULT_CONFIG["gql_benefit_plan_assignment_manage_perms"],
        )

    def test_invalid_access_config_is_rejected(self):
        config = dict(DEFAULT_CONFIG)
        config["enable_benefit_plan_access_control"] = "true"
        config["gql_benefit_plan_all_access_perms"] = []
        with self.assertRaises(ValidationError):
            SocialProtectionConfig._SocialProtectionConfig__validate_config(config)


class BenefitPlanAccessPolicyTest(SimpleTestCase):
    def setUp(self):
        self.original_enabled = SocialProtectionConfig.enable_benefit_plan_access_control
        self.original_all = SocialProtectionConfig.gql_benefit_plan_all_access_perms
        self.original_manage = SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = ["160006"]
        SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms = ["160007"]

    def tearDown(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = self.original_enabled
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = self.original_all
        SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms = self.original_manage

    def test_feature_disabled_preserves_queryset_and_object_access(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        queryset = Mock()
        user = SimpleNamespace(is_superuser=False, has_perms=Mock(return_value=False))
        self.assertTrue(can_access_benefit_plan(user, "plan"))
        self.assertIsNone(get_accessible_benefit_plan_ids(user))
        self.assertIs(filter_queryset_by_benefit_plan(queryset, user), queryset)

    def test_shared_feature_state_does_not_load_authorization_models(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        with patch(
            "social_protection.benefit_plan_access._user_benefit_plan_access_model",
            side_effect=AssertionError("authorization models must stay lazy"),
        ):
            self.assertTrue(benefit_plan_access_control_enabled())

    def test_all_plan_permission_bypasses_assignments_only(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        user = SimpleNamespace(
            is_superuser=False,
            has_perms=Mock(side_effect=lambda perms: perms == ["160006"]),
        )
        self.assertTrue(has_all_benefit_plans_access(user))
        self.assertTrue(can_access_benefit_plan(user, "plan"))
        self.assertFalse(user.has_perms(["160003"]))

    def test_superuser_bypasses_assignment_restrictions(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        user = SimpleNamespace(is_superuser=True, has_perms=Mock(return_value=False))
        self.assertTrue(can_access_benefit_plan(user, "plan"))
        self.assertIsNone(get_accessible_benefit_plan_ids(user))

    def test_update_permission_does_not_grant_assignment_management(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        user = SimpleNamespace(
            is_superuser=False,
            has_perms=Mock(side_effect=lambda perms: perms == ["160003"]),
        )
        self.assertFalse(can_manage_benefit_plan_assignments(user, "plan"))

    def test_assignment_manager_is_limited_to_accessible_plans(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        user = SimpleNamespace(
            is_superuser=False,
            has_perms=Mock(side_effect=lambda perms: perms == ["160007"]),
        )
        with patch(
            "social_protection.benefit_plan_access.can_access_benefit_plan",
            return_value=False,
        ):
            self.assertFalse(can_manage_benefit_plan_assignments(user, "plan"))

    def test_all_plan_assignment_manager_can_manage_any_plan(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        user = SimpleNamespace(
            is_superuser=False,
            has_perms=Mock(return_value=True),
        )
        self.assertTrue(can_manage_benefit_plan_assignments(user, "plan"))


@override_settings(ROW_SECURITY=False)
class UserBenefitPlanAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_technical_user(username="plan-access-user")
        cls.audit_user = cls.user
        cls.upg = BenefitPlan(code="UPG000001", name="UPG")
        cls.upg.save(user=cls.audit_user)
        cls.rmep = BenefitPlan(code="RMEP00001", name="RMEP")
        cls.rmep.save(user=cls.audit_user)

    def setUp(self):
        self.original_enabled = SocialProtectionConfig.enable_benefit_plan_access_control
        self.original_all = SocialProtectionConfig.gql_benefit_plan_all_access_perms
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = ["160006"]

    def tearDown(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = self.original_enabled
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = self.original_all

    def assign(self, plan):
        assignment = UserBenefitPlanAccess(user=self.user, benefit_plan=plan)
        assignment.save(user=self.audit_user)
        return assignment

    def test_user_can_hold_upg_rmep_and_both_assignments(self):
        self.assign(self.upg)
        self.assertTrue(can_access_benefit_plan(self.user, self.upg.id))
        self.assertFalse(can_access_benefit_plan(self.user, self.rmep.id))

        self.assign(self.rmep)
        self.assertTrue(can_access_benefit_plan(self.user, self.rmep.id))
        self.assertCountEqual(
            list(get_accessible_benefit_plan_ids(self.user)),
            [self.upg.id, self.rmep.id],
        )

    def test_rmep_only_user_gets_rmep_not_upg(self):
        self.assign(self.rmep)
        self.assertTrue(can_access_benefit_plan(self.user, self.rmep.id))
        self.assertFalse(can_access_benefit_plan(self.user, self.upg.id))

    def test_no_assignment_means_no_access_even_without_row_security(self):
        self.assertFalse(can_access_benefit_plan(self.user, self.upg.id))
        self.assertEqual(list(get_accessible_benefit_plan_ids(self.user)), [])
        with self.assertRaises(PermissionDenied):
            require_benefit_plan_access(self.user, self.upg.id)

    def test_duplicate_active_assignment_is_rejected(self):
        self.assign(self.upg)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.assign(self.upg)

    def test_soft_deleted_assignment_can_be_recreated(self):
        assignment = self.assign(self.upg)
        assignment.delete(user=self.audit_user)
        recreated = self.assign(self.upg)
        self.assertNotEqual(assignment.id, recreated.id)
        self.assertTrue(can_access_benefit_plan(self.user, self.upg.id))

    def test_queryset_filter_uses_only_assigned_plans(self):
        self.assign(self.upg)
        scoped = filter_queryset_by_benefit_plan(
            BenefitPlan.objects.filter(is_deleted=False),
            self.user,
            "id",
        )
        self.assertEqual(list(scoped), [self.upg])

    def test_cloned_scope_marker_is_safe_for_supported_same_user_reuse(self):
        self.assign(self.upg)
        scoped = filter_queryset_by_benefit_plan(
            BenefitPlan.objects.filter(is_deleted=False),
            self.user,
            "id",
        )

        # Chaining clones the Query and retains the marker. Supported callers
        # keep the same user and field, so the existing UPG restriction remains.
        cloned = scoped.filter(name__icontains="UPG").filter(
            name__icontains="UPG",
        )
        reapplied = filter_queryset_by_benefit_plan(cloned, self.user, "id")

        self.assertIs(reapplied, cloned)
        self.assertEqual(list(reapplied), [self.upg])
