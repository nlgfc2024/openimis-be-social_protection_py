from types import SimpleNamespace
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

import graphene
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from graphql_relay import to_global_id

from core.models import ClaimAdmin, MutationLog, Officer, User
from core.test_helpers import (
    create_test_interactive_user,
    create_test_language,
    create_test_technical_user,
)
from social_protection.apps import SocialProtectionConfig
from social_protection.benefit_plan_access import (
    can_access_benefit_plan,
    can_manage_benefit_plan_assignments,
)
from social_protection.gql_mutations import (
    AssignBenefitPlanUserMutation,
    RevokeBenefitPlanUserAccessMutation,
)
from social_protection.gql_queries import (
    BenefitPlanAssignmentUserGQLType,
    UserBenefitPlanAccessGQLType,
)
from social_protection.models import BenefitPlan, UserBenefitPlanAccess
from social_protection.schema import Mutation, Query


class BenefitPlanAccessNodeQuery(Query, graphene.ObjectType):
    node = graphene.relay.Node.Field()


class BenefitPlanUserAccessGraphQLUnitTest(SimpleTestCase):
    def setUp(self):
        self.original_enabled = SocialProtectionConfig.enable_benefit_plan_access_control
        self.original_all = SocialProtectionConfig.gql_benefit_plan_all_access_perms
        self.original_manage = SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = ["160006"]
        SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms = ["160007"]
        self.requester = SimpleNamespace(
            id=uuid4(),
            is_superuser=False,
            has_perms=lambda perms: False,
        )
        self.target_id = uuid4()
        self.plan_id = uuid4()

    def tearDown(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = self.original_enabled
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = self.original_all
        SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms = self.original_manage

    def grant(self, *permissions):
        self.requester.has_perms = lambda perms: all(
            permission in permissions for permission in perms
        )

    @patch("social_protection.schema.BenefitPlan.objects.filter")
    @patch("social_protection.gql_mutations.BenefitPlan.objects.filter")
    def test_flag_disabled_rejects_160007_for_assign_list_and_revoke(
        self,
        mutation_plan_filter,
        schema_plan_filter,
    ):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        self.grant("160007")
        mutation_plan_filter.return_value.exists.return_value = True
        schema_plan_filter.return_value.exists.return_value = True

        with self.assertRaises(PermissionDenied):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.requester,
                user_id=self.target_id,
                benefit_plan_id=self.plan_id,
            )
        with self.assertRaises(PermissionDenied):
            Query().resolve_benefit_plan_user_access(
                SimpleNamespace(context=SimpleNamespace(user=self.requester)),
                benefit_plan_id=self.plan_id,
            )
        assignment = SimpleNamespace(benefit_plan_id=self.plan_id)
        with patch.object(
            RevokeBenefitPlanUserAccessMutation,
            "_active_assignment",
            return_value=assignment,
        ), self.assertRaises(PermissionDenied):
            RevokeBenefitPlanUserAccessMutation._mutate(
                self.requester,
                id=uuid4(),
            )

    def test_flag_disabled_accepts_160006_with_160007(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        self.grant("160006", "160007")
        self.assertTrue(
            can_manage_benefit_plan_assignments(self.requester, self.plan_id),
        )

    def test_flag_disabled_preserves_superuser_management(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        self.requester.is_superuser = True
        self.assertTrue(
            can_manage_benefit_plan_assignments(self.requester, self.plan_id),
        )

    def test_assignment_user_type_exposes_only_required_fields(self):
        self.assertEqual(
            set(BenefitPlanAssignmentUserGQLType._meta.fields),
            {"id", "username", "first_name", "last_name"},
        )

    @patch(
        "social_protection.gql_queries."
        "can_manage_benefit_plan_assignments",
        return_value=False,
    )
    @patch("social_protection.gql_queries.UserBenefitPlanAccess.objects.filter")
    def test_direct_node_lookup_hides_inaccessible_assignment(
        self,
        assignment_filter,
        can_manage,
    ):
        assignment = SimpleNamespace(benefit_plan_id=self.plan_id)
        assignment_filter.return_value.first.return_value = assignment
        info = SimpleNamespace(context=SimpleNamespace(user=self.requester))
        self.grant("160007")

        self.assertIsNone(UserBenefitPlanAccessGQLType.get_node(
            info,
            str(uuid4()),
        ))
        can_manage.assert_called_once_with(
            self.requester,
            self.plan_id,
        )

    @patch("social_protection.gql_queries.UserBenefitPlanAccess.objects.filter")
    def test_direct_node_lookup_requires_general_management_permission(
        self,
        assignment_filter,
    ):
        info = SimpleNamespace(context=SimpleNamespace(user=self.requester))
        with self.assertRaises(PermissionDenied):
            UserBenefitPlanAccessGQLType.get_node(info, str(uuid4()))
        assignment_filter.assert_not_called()

    @patch(
        "social_protection.gql_queries."
        "can_manage_benefit_plan_assignments",
    )
    @patch("social_protection.gql_queries.UserBenefitPlanAccess.objects.filter")
    def test_direct_node_lookup_returns_none_for_missing_assignment(
        self,
        assignment_filter,
        can_manage,
    ):
        self.grant("160007")
        assignment_filter.return_value.first.return_value = None
        info = SimpleNamespace(context=SimpleNamespace(user=self.requester))
        self.assertIsNone(
            UserBenefitPlanAccessGQLType.get_node(info, str(uuid4())),
        )
        can_manage.assert_not_called()

    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.UserBenefitPlanAccess.objects.filter")
    @patch("social_protection.gql_mutations.User.objects.select_related")
    @patch("social_protection.gql_mutations.BenefitPlan.objects.filter")
    def test_authorised_assignment_validation(
        self,
        benefit_plan_filter,
        user_select_related,
        assignment_filter,
        require_management,
    ):
        self.grant("160007")
        benefit_plan_filter.return_value.exists.return_value = True
        user_select_related.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(
                _u=SimpleNamespace(validity_to=None),
                i_user=None,
                is_active=True,
            )
        )
        assignment_filter.return_value.exists.return_value = False

        AssignBenefitPlanUserMutation._validate_mutation(
            self.requester,
            user_id=self.target_id,
            benefit_plan_id=self.plan_id,
        )
        require_management.assert_called_once_with(self.requester, self.plan_id)

    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.UserBenefitPlanAccess.objects.filter")
    @patch("social_protection.gql_mutations.User.objects.select_related")
    @patch("social_protection.gql_mutations.BenefitPlan.objects.filter")
    def test_duplicate_assignment_validation(
        self,
        benefit_plan_filter,
        user_select_related,
        assignment_filter,
        _require_management,
    ):
        self.grant("160007")
        benefit_plan_filter.return_value.exists.return_value = True
        user_select_related.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(
                _u=SimpleNamespace(validity_to=None),
                i_user=None,
                is_active=True,
            )
        )
        assignment_filter.return_value.exists.return_value = True
        with self.assertRaises(ValidationError):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.requester,
                user_id=self.target_id,
                benefit_plan_id=self.plan_id,
            )

    @patch("social_protection.gql_mutations.UserBenefitPlanAccess")
    @patch("social_protection.gql_mutations.transaction.atomic", return_value=nullcontext())
    def test_assignment_is_audited(self, _atomic, assignment_model):
        assignment = assignment_model.return_value
        AssignBenefitPlanUserMutation._mutate(
            self.requester,
            user_id=self.target_id,
            benefit_plan_id=self.plan_id,
        )
        assignment_model.assert_called_once_with(
            user_id=self.target_id,
            benefit_plan_id=self.plan_id,
        )
        assignment.save.assert_called_once_with(user=self.requester)

    @patch("social_protection.gql_mutations.UserBenefitPlanAccess")
    @patch("social_protection.gql_mutations.transaction.atomic", return_value=nullcontext())
    def test_concurrent_duplicate_returns_assignment_exists_validation_error(
        self,
        _atomic,
        assignment_model,
    ):
        assignment_model.return_value.save.side_effect = IntegrityError
        with self.assertRaisesMessage(
            ValidationError,
            "mutation.benefit_plan_user_assignment_exists",
        ):
            AssignBenefitPlanUserMutation._mutate(
                self.requester,
                user_id=self.target_id,
                benefit_plan_id=self.plan_id,
            )

    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.UserBenefitPlanAccess.objects.filter")
    @patch("social_protection.gql_mutations.User.objects.select_related")
    @patch("social_protection.gql_mutations.BenefitPlan.objects.filter")
    def test_expired_inactive_or_soft_deleted_user_is_rejected(
        self,
        benefit_plan_filter,
        user_select_related,
        assignment_filter,
        _require_management,
    ):
        self.grant("160007")
        benefit_plan_filter.return_value.exists.return_value = True
        assignment_filter.return_value.exists.return_value = False
        rejected_users = (
            SimpleNamespace(
                _u=SimpleNamespace(validity_to=None),
                i_user=SimpleNamespace(
                    validity_to=timezone.now() - timedelta(days=1),
                ),
                is_active=True,
            ),
            SimpleNamespace(
                _u=SimpleNamespace(validity_to=None),
                i_user=SimpleNamespace(validity_to=None),
                is_active=False,
            ),
            SimpleNamespace(
                _u=SimpleNamespace(validity_to=None),
                i_user=SimpleNamespace(validity_to=None),
                is_active=False,
            ),
        )
        for target_user in rejected_users:
            user_select_related.return_value.filter.return_value.first.return_value = (
                target_user
            )
            with self.subTest(target_user=target_user), self.assertRaisesMessage(
                ValidationError,
                "mutation.user_not_found",
            ):
                AssignBenefitPlanUserMutation._validate_mutation(
                    self.requester,
                    user_id=self.target_id,
                    benefit_plan_id=self.plan_id,
                )

    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.UserBenefitPlanAccess.objects.filter")
    @patch("social_protection.gql_mutations.User.objects.select_related")
    @patch("social_protection.gql_mutations.BenefitPlan.objects.filter")
    def test_active_user_expiry_variants_are_accepted(
        self,
        benefit_plan_filter,
        user_select_related,
        assignment_filter,
        _require_management,
    ):
        self.grant("160007")
        benefit_plan_filter.return_value.exists.return_value = True
        assignment_filter.return_value.exists.return_value = False
        now = timezone.now()
        with patch("social_protection.gql_mutations.timezone.now", return_value=now):
            for validity_to in (None, now, now + timedelta(days=365)):
                target_user = SimpleNamespace(
                    _u=SimpleNamespace(validity_to=validity_to),
                    i_user=SimpleNamespace(validity_to=validity_to),
                    is_active=True,
                )
                user_select_related.return_value.filter.return_value.first.return_value = (
                    target_user
                )
                with self.subTest(validity_to=validity_to):
                    AssignBenefitPlanUserMutation._validate_mutation(
                        self.requester,
                        user_id=self.target_id,
                        benefit_plan_id=self.plan_id,
                    )

    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.RevokeBenefitPlanUserAccessMutation._active_assignment")
    def test_revocation_uses_soft_delete_with_audit_user(
        self,
        active_assignment,
        _require_management,
    ):
        self.grant("160007")
        assignment = active_assignment.return_value
        assignment.benefit_plan_id = self.plan_id
        assignment.user_id = self.target_id
        RevokeBenefitPlanUserAccessMutation._mutate(
            self.requester,
            id=uuid4(),
        )
        assignment.delete.assert_called_once_with(user=self.requester)

    @patch("social_protection.gql_mutations.has_all_benefit_plans_access", return_value=False)
    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.RevokeBenefitPlanUserAccessMutation._active_assignment")
    def test_plan_scoped_manager_cannot_revoke_own_access(
        self,
        active_assignment,
        _require_management,
        _has_all_access,
    ):
        self.grant("160007")
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        active_assignment.return_value = SimpleNamespace(
            benefit_plan_id=self.plan_id,
            user_id=self.requester.id,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "mutation.benefit_plan_user_assignment_self_revoke",
        ):
            RevokeBenefitPlanUserAccessMutation._mutate(
                self.requester,
                id=uuid4(),
            )

    @patch("social_protection.gql_mutations.has_all_benefit_plans_access", return_value=True)
    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.RevokeBenefitPlanUserAccessMutation._active_assignment")
    def test_global_manager_can_revoke_own_redundant_assignment(
        self,
        active_assignment,
        _require_management,
        _has_all_access,
    ):
        self.grant("160007")
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        assignment = Mock(
            benefit_plan_id=self.plan_id,
            user_id=self.requester.id,
        )
        active_assignment.return_value = assignment
        RevokeBenefitPlanUserAccessMutation._mutate(
            self.requester,
            id=uuid4(),
        )
        assignment.delete.assert_called_once_with(user=self.requester)

    @patch(
        "social_protection.gql_mutations."
        "RevokeBenefitPlanUserAccessMutation._active_assignment",
    )
    def test_revocation_without_manage_permission_does_not_probe_assignment(
        self,
        active_assignment,
    ):
        assignment_id = uuid4()
        with self.assertRaisesMessage(
            PermissionDenied,
            "Benefit Plan assignment-management permission is required.",
        ):
            RevokeBenefitPlanUserAccessMutation._validate_mutation(
                self.requester,
                id=assignment_id,
            )
        with self.assertRaisesMessage(
            PermissionDenied,
            "Benefit Plan assignment-management permission is required.",
        ):
            RevokeBenefitPlanUserAccessMutation._mutate(
                self.requester,
                id=assignment_id,
            )
        active_assignment.assert_not_called()

    @patch(
        "social_protection.gql_mutations."
        "RevokeBenefitPlanUserAccessMutation._active_assignment",
        return_value=None,
    )
    def test_authorised_revocation_of_missing_assignment_returns_not_found(
        self,
        active_assignment,
    ):
        self.grant("160007")
        assignment_id = uuid4()
        RevokeBenefitPlanUserAccessMutation._validate_mutation(
            self.requester,
            id=assignment_id,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "mutation.benefit_plan_user_assignment_not_found",
        ):
            RevokeBenefitPlanUserAccessMutation._mutate(
                self.requester,
                id=assignment_id,
            )
        active_assignment.assert_called_once_with(assignment_id)

    @patch("social_protection.schema.require_benefit_plan_assignment_management")
    @patch("social_protection.schema.UserBenefitPlanAccess.objects.filter")
    @patch("social_protection.schema.BenefitPlan.objects.filter")
    def test_resolver_scopes_to_requested_plan(
        self,
        benefit_plan_filter,
        access_filter,
        require_management,
    ):
        self.grant("160007")
        benefit_plan_filter.return_value.exists.return_value = True
        queryset = Mock()
        access_filter.return_value.select_related.return_value.order_by.return_value = queryset
        info = SimpleNamespace(context=SimpleNamespace(user=self.requester))
        result = Query().resolve_benefit_plan_user_access(
            info,
            benefit_plan_id=self.plan_id,
        )
        self.assertIs(result, queryset)
        require_management.assert_called_once_with(self.requester, self.plan_id)
        access_filter.assert_called_once_with(
            benefit_plan_id=self.plan_id,
            is_deleted=False,
        )
        access_filter.return_value.select_related.assert_called_once_with(
            "user",
            "user__i_user",
            "user__officer",
            "user__claim_admin",
            "user__t_user",
        )

    @patch("social_protection.schema.require_benefit_plan_assignment_management")
    @patch("social_protection.schema.BenefitPlan.objects.filter")
    def test_missing_plan_uses_not_found_error(
        self,
        benefit_plan_filter,
        _require_management,
    ):
        self.grant("160007")
        benefit_plan_filter.return_value.exists.return_value = False
        with self.assertRaisesMessage(
            ValidationError,
            "mutation.benefit_plan_not_found",
        ):
            Query().resolve_benefit_plan_user_access(
                SimpleNamespace(context=SimpleNamespace(user=self.requester)),
                benefit_plan_id=self.plan_id,
            )

    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.BenefitPlan.objects.filter")
    def test_assignment_to_missing_plan_uses_not_found_error(
        self,
        benefit_plan_filter,
        require_management,
    ):
        self.grant("160007")
        benefit_plan_filter.return_value.exists.return_value = False
        with self.assertRaisesMessage(
            ValidationError,
            "mutation.benefit_plan_not_found",
        ):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.requester,
                user_id=self.target_id,
                benefit_plan_id=self.plan_id,
            )
        require_management.assert_not_called()

    @patch("social_protection.gql_mutations.BenefitPlan.objects.filter")
    def test_assignment_without_manage_permission_does_not_probe_plan(
        self,
        benefit_plan_filter,
    ):
        for plan_exists in (False, True):
            benefit_plan_filter.reset_mock()
            benefit_plan_filter.return_value.exists.return_value = plan_exists
            with self.assertRaisesMessage(
                PermissionDenied,
                "Benefit Plan assignment-management permission is required.",
            ):
                AssignBenefitPlanUserMutation._validate_mutation(
                    self.requester,
                    user_id=self.target_id,
                    benefit_plan_id=self.plan_id,
                )
            benefit_plan_filter.assert_not_called()

    @patch("social_protection.schema.BenefitPlan.objects.filter")
    def test_assignment_list_without_manage_permission_does_not_probe_plan(
        self,
        benefit_plan_filter,
    ):
        for plan_exists in (False, True):
            benefit_plan_filter.reset_mock()
            benefit_plan_filter.return_value.exists.return_value = plan_exists
            with self.assertRaisesMessage(
                PermissionDenied,
                "Benefit Plan assignment-management permission is required.",
            ):
                Query().resolve_benefit_plan_user_access(
                    SimpleNamespace(
                        context=SimpleNamespace(user=self.requester),
                    ),
                    benefit_plan_id=self.plan_id,
                )
            benefit_plan_filter.assert_not_called()

    def test_assignment_user_identifier_is_plain_uuid(self):
        target_id = uuid4()
        resolved = BenefitPlanAssignmentUserGQLType.resolve_id(
            SimpleNamespace(id=target_id),
            None,
        )
        self.assertEqual(resolved, str(target_id))

    def test_assignment_user_names_prefer_interactive_user(self):
        user = SimpleNamespace(
            username="interactive-login",
            i_user=SimpleNamespace(
                other_names="Interactive First",
                last_name="Interactive Last",
            ),
            officer=SimpleNamespace(
                other_names="Officer First",
                last_name="Officer Last",
            ),
        )
        self.assertEqual(
            BenefitPlanAssignmentUserGQLType.resolve_first_name(user, None),
            "Interactive First",
        )
        self.assertEqual(
            BenefitPlanAssignmentUserGQLType.resolve_last_name(user, None),
            "Interactive Last",
        )

    def test_assignment_user_names_fall_back_to_officer(self):
        user = SimpleNamespace(
            username="officer-login",
            i_user=None,
            officer=SimpleNamespace(
                other_names="Officer First",
                last_name="Officer Last",
            ),
            claim_admin=None,
        )
        self.assertEqual(
            BenefitPlanAssignmentUserGQLType.resolve_first_name(user, None),
            "Officer First",
        )
        self.assertEqual(
            BenefitPlanAssignmentUserGQLType.resolve_last_name(user, None),
            "Officer Last",
        )

    def test_assignment_user_names_fall_back_to_claim_admin(self):
        user = SimpleNamespace(
            username="claim-admin-login",
            i_user=None,
            officer=None,
            claim_admin=SimpleNamespace(
                other_names="Claim First",
                last_name="Claim Last",
            ),
        )
        self.assertEqual(
            BenefitPlanAssignmentUserGQLType.resolve_first_name(user, None),
            "Claim First",
        )
        self.assertEqual(
            BenefitPlanAssignmentUserGQLType.resolve_last_name(user, None),
            "Claim Last",
        )

    def test_assignment_user_partial_and_technical_name_fallbacks(self):
        partial_user = SimpleNamespace(
            username="partial-login",
            i_user=SimpleNamespace(
                other_names="",
                last_name="Interactive Last",
            ),
            officer=SimpleNamespace(
                other_names="Officer First",
                last_name="Officer Last",
            ),
            claim_admin=None,
        )
        self.assertEqual(
            BenefitPlanAssignmentUserGQLType.resolve_first_name(
                partial_user,
                None,
            ),
            "Officer First",
        )
        self.assertEqual(
            BenefitPlanAssignmentUserGQLType.resolve_last_name(
                partial_user,
                None,
            ),
            "Interactive Last",
        )

        technical_user = SimpleNamespace(
            username="technical-login",
            i_user=None,
            officer=None,
            claim_admin=None,
        )
        self.assertEqual(
            BenefitPlanAssignmentUserGQLType.resolve_first_name(
                technical_user,
                None,
            ),
            "technical-login",
        )
        self.assertIsNone(
            BenefitPlanAssignmentUserGQLType.resolve_last_name(
                technical_user,
                None,
            ),
        )

    @patch("social_protection.gql_queries.filter_benefit_plan_assignments_for_management")
    def test_assignment_type_queryset_reuses_shared_management_filter(
        self,
        management_filter,
    ):
        queryset = Mock()
        active_queryset = queryset.filter.return_value
        management_filter.return_value = Mock()
        info = SimpleNamespace(context=SimpleNamespace(user=self.requester))
        result = UserBenefitPlanAccessGQLType.get_queryset(queryset, info)
        self.assertIs(result, management_filter.return_value)
        queryset.filter.assert_called_once_with(is_deleted=False)
        management_filter.assert_called_once_with(active_queryset, self.requester)


class BenefitPlanUserAccessGraphQLTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_test_language(code="en")
        cls.manager = create_test_technical_user(username="assignment-manager")
        cls.target = create_test_technical_user(username="assignment-target")
        cls.named_target = create_test_interactive_user(
            username="assignment-named-target",
            custom_props={
                "other_names": "Named First",
                "last_name": "Named Last",
                "email": "private@example.test",
                "phone": "0999999999",
            },
        )
        cls.superuser = create_test_technical_user(
            username="assignment-superuser",
            super_user=True,
        )
        officer = Officer.objects.create(
            code="ASSIGN-OFFICER",
            last_name="Officer",
            other_names="Assignment",
            audit_user_id=-1,
        )
        cls.officer_target = User.objects.create(
            username="assignment-officer",
            officer=officer,
        )
        claim_admin = ClaimAdmin.objects.create(
            code="ASSIGN-CLAIM-ADMIN",
            last_name="Claim Admin",
            other_names="Assignment",
            audit_user_id=-1,
        )
        cls.claim_admin_target = User.objects.create(
            username="assignment-claim-admin",
            claim_admin=claim_admin,
        )
        cls.upg = BenefitPlan(code="UPG000002", name="UPG assignment test")
        cls.upg.save(user=cls.manager)
        cls.rmep = BenefitPlan(code="RMEP00002", name="RMEP assignment test")
        cls.rmep.save(user=cls.manager)

    def setUp(self):
        self.original_enabled = SocialProtectionConfig.enable_benefit_plan_access_control
        self.original_all = SocialProtectionConfig.gql_benefit_plan_all_access_perms
        self.original_manage = SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = ["160006"]
        SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms = ["160007"]
        self.manager.has_perms = lambda perms: perms == ["160007"]
        self.assign(self.manager, self.upg, self.manager)
        self.schema = graphene.Schema(
            query=BenefitPlanAccessNodeQuery,
            mutation=Mutation,
        )

    def tearDown(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = self.original_enabled
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = self.original_all
        SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms = self.original_manage

    @staticmethod
    def assign(target, plan, audit_user):
        assignment = UserBenefitPlanAccess(user=target, benefit_plan=plan)
        assignment.save(user=audit_user)
        return assignment

    def validate_and_assign(self, requester, target, plan):
        data = {"user_id": target.id, "benefit_plan_id": plan.id}
        AssignBenefitPlanUserMutation._validate_mutation(requester, **data)
        AssignBenefitPlanUserMutation._mutate(requester, **data)

    @staticmethod
    def request(user):
        request = RequestFactory().post("/graphql")
        request.user = user
        return request

    def execute(self, document, user=None, variables=None):
        return self.schema.execute(
            document,
            context_value=self.request(user or self.manager),
            variable_values=variables,
        )

    def test_actual_graphql_assignment_succeeds(self):
        result = self.execute(
            """
            mutation AssignBenefitPlanUser($plan: UUID!, $user: UUID!) {
              assignBenefitPlanUser(input: {
                benefitPlanId: $plan
                userId: $user
                clientMutationId: "assignment-success"
              }) {
                internalId
                clientMutationId
              }
            }
            """,
            variables={
                "plan": str(self.upg.id),
                "user": str(self.target.id),
            },
        )
        self.assertIsNone(result.errors, result.errors)
        self.assertTrue(UserBenefitPlanAccess.objects.filter(
            user=self.target,
            benefit_plan=self.upg,
            is_deleted=False,
        ).exists())
        self.assertEqual(
            MutationLog.objects.get(
                client_mutation_id="assignment-success",
            ).status,
            MutationLog.SUCCESS,
        )

    def test_active_interactive_user_without_expiry_is_eligible(self):
        self.named_target.i_user.active = True
        self.named_target.i_user.validity_to = None
        self.named_target.i_user.save()
        AssignBenefitPlanUserMutation._validate_mutation(
            self.manager,
            user_id=self.named_target.id,
            benefit_plan_id=self.upg.id,
        )

    def test_active_interactive_user_with_future_expiry_is_eligible(self):
        self.named_target.i_user.active = True
        self.named_target.i_user.validity_to = timezone.now() + timedelta(days=1)
        self.named_target.i_user.save()
        AssignBenefitPlanUserMutation._validate_mutation(
            self.manager,
            user_id=self.named_target.id,
            benefit_plan_id=self.upg.id,
        )

    def test_expired_interactive_user_is_rejected_even_when_active(self):
        self.named_target.i_user.active = True
        self.named_target.i_user.validity_to = timezone.now() - timedelta(days=1)
        self.named_target.i_user.save()
        with self.assertRaisesMessage(ValidationError, "mutation.user_not_found"):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.manager,
                user_id=self.named_target.id,
                benefit_plan_id=self.upg.id,
            )

    def test_inactive_interactive_user_is_rejected(self):
        self.named_target.i_user.active = False
        self.named_target.i_user.validity_to = None
        self.named_target.i_user.save()
        with self.assertRaisesMessage(ValidationError, "mutation.user_not_found"):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.manager,
                user_id=self.named_target.id,
                benefit_plan_id=self.upg.id,
            )

    def test_interactive_user_expiry_boundary_is_eligible(self):
        boundary = timezone.now()
        self.named_target.i_user.active = True
        self.named_target.i_user.validity_to = boundary
        self.named_target.i_user.save()
        with patch("social_protection.gql_mutations.timezone.now", return_value=boundary):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.manager,
                user_id=self.named_target.id,
                benefit_plan_id=self.upg.id,
            )

    def test_active_non_interactive_user_types_are_eligible(self):
        for target_user in (
            self.target,
            self.officer_target,
            self.claim_admin_target,
        ):
            with self.subTest(username=target_user.username):
                AssignBenefitPlanUserMutation._validate_mutation(
                    self.manager,
                    user_id=target_user.id,
                    benefit_plan_id=self.upg.id,
                )

    def test_missing_user_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, "mutation.user_not_found"):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.manager,
                user_id=uuid4(),
                benefit_plan_id=self.upg.id,
            )

    def test_actual_graphql_failed_mutation_is_recorded(self):
        result = self.execute(
            """
            mutation AssignBenefitPlanUser($plan: UUID!, $user: UUID!) {
              assignBenefitPlanUser(input: {
                benefitPlanId: $plan
                userId: $user
                clientMutationId: "assignment-denied"
              }) {
                internalId
                clientMutationId
              }
            }
            """,
            variables={
                "plan": str(self.rmep.id),
                "user": str(self.target.id),
            },
        )
        self.assertIsNone(result.errors, result.errors)
        self.assertFalse(UserBenefitPlanAccess.objects.filter(
            user=self.target,
            benefit_plan=self.rmep,
            is_deleted=False,
        ).exists())
        mutation_log = MutationLog.objects.get(
            client_mutation_id="assignment-denied",
        )
        self.assertEqual(mutation_log.status, MutationLog.ERROR)

    def test_actual_graphql_rejects_invalid_uuid_input(self):
        result = self.execute(
            """
            mutation {
              assignBenefitPlanUser(input: {
                benefitPlanId: "not-a-uuid"
                userId: "also-not-a-uuid"
                clientMutationId: "assignment-invalid-uuid"
              }) {
                internalId
              }
            }
            """,
        )
        self.assertTrue(result.errors)
        self.assertFalse(MutationLog.objects.filter(
            client_mutation_id="assignment-invalid-uuid",
        ).exists())

    def test_restricted_user_fields_are_returned_without_private_data(self):
        self.assign(self.named_target, self.upg, self.manager)
        result = self.execute(
            """
            query BenefitPlanAssignments($plan: UUID!) {
              benefitPlanUserAccess(benefitPlanId: $plan, first: 100) {
                edges {
                  node {
                    user { id username firstName lastName }
                  }
                }
              }
            }
            """,
            variables={"plan": str(self.upg.id)},
        )
        self.assertIsNone(result.errors, result.errors)
        users = [
            edge["node"]["user"]
            for edge in result.data["benefitPlanUserAccess"]["edges"]
        ]
        named_user = next(
            user for user in users
            if user["username"] == self.named_target.username
        )
        self.assertEqual(named_user["firstName"], "Named First")
        self.assertEqual(named_user["lastName"], "Named Last")
        self.assertEqual(
            set(named_user),
            {"id", "username", "firstName", "lastName"},
        )

        forbidden = self.execute(
            """
            query BenefitPlanAssignments($plan: UUID!) {
              benefitPlanUserAccess(benefitPlanId: $plan, first: 1) {
                edges { node { user { email phone rights } } }
              }
            }
            """,
            variables={"plan": str(self.upg.id)},
        )
        self.assertTrue(forbidden.errors)

    def test_authorised_assignment_and_multiple_plans(self):
        self.validate_and_assign(self.manager, self.target, self.upg)
        self.manager.has_perms = lambda perms: perms in (["160007"], ["160006"])
        self.validate_and_assign(self.manager, self.target, self.rmep)
        self.assertEqual(
            UserBenefitPlanAccess.objects.filter(
                user=self.target,
                is_deleted=False,
            ).count(),
            2,
        )

    def test_authorised_revocation_and_reassignment(self):
        self.validate_and_assign(self.manager, self.target, self.upg)
        assignment = UserBenefitPlanAccess.objects.get(
            user=self.target,
            benefit_plan=self.upg,
            is_deleted=False,
        )
        RevokeBenefitPlanUserAccessMutation._validate_mutation(
            self.manager,
            id=assignment.id,
        )
        RevokeBenefitPlanUserAccessMutation._mutate(
            self.manager,
            id=assignment.id,
        )
        self.assertFalse(UserBenefitPlanAccess.objects.filter(
            id=assignment.id,
            is_deleted=False,
        ).exists())
        self.validate_and_assign(self.manager, self.target, self.upg)

    def test_unauthorised_revocation_hides_assignment_existence(self):
        existing_assignment = UserBenefitPlanAccess.objects.get(
            user=self.manager,
            benefit_plan=self.upg,
            is_deleted=False,
        )
        self.target.has_perms = lambda perms: False
        for assignment_id in (existing_assignment.id, uuid4()):
            with self.subTest(assignment_id=assignment_id), self.assertRaisesMessage(
                PermissionDenied,
                "Benefit Plan assignment-management permission is required.",
            ):
                RevokeBenefitPlanUserAccessMutation._validate_mutation(
                    self.target,
                    id=assignment_id,
                )
            with self.subTest(assignment_id=assignment_id), self.assertRaisesMessage(
                PermissionDenied,
                "Benefit Plan assignment-management permission is required.",
            ):
                RevokeBenefitPlanUserAccessMutation._mutate(
                    self.target,
                    id=assignment_id,
                )

    def test_duplicate_active_assignment_is_rejected(self):
        self.validate_and_assign(self.manager, self.target, self.upg)
        with self.assertRaises(ValidationError):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.manager,
                user_id=self.target.id,
                benefit_plan_id=self.upg.id,
            )

    def test_160003_alone_is_rejected(self):
        self.manager.has_perms = lambda perms: perms == ["160003"]
        with self.assertRaises(PermissionDenied):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.manager,
                user_id=self.target.id,
                benefit_plan_id=self.upg.id,
            )

    def test_160007_cannot_manage_outside_own_plan_scope(self):
        with self.assertRaises(PermissionDenied):
            self.validate_and_assign(self.manager, self.target, self.rmep)

    def test_flag_disabled_160007_without_160006_is_rejected(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        with self.assertRaises(PermissionDenied):
            self.validate_and_assign(self.manager, self.target, self.upg)

    def test_flag_disabled_160006_and_160007_can_manage_any_plan(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        self.manager.has_perms = lambda perms: perms in (["160006"], ["160007"])
        self.validate_and_assign(self.manager, self.target, self.rmep)
        self.assertTrue(UserBenefitPlanAccess.objects.filter(
            user=self.target,
            benefit_plan=self.rmep,
            is_deleted=False,
        ).exists())

    def test_flag_disabled_superuser_can_manage_any_plan(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        self.validate_and_assign(self.superuser, self.target, self.rmep)
        self.assertTrue(UserBenefitPlanAccess.objects.filter(
            user=self.target,
            benefit_plan=self.rmep,
            is_deleted=False,
        ).exists())

    def test_160006_and_160007_can_manage_any_plan(self):
        self.manager.has_perms = lambda perms: perms in (["160006"], ["160007"])
        self.validate_and_assign(self.manager, self.target, self.rmep)
        self.assertTrue(UserBenefitPlanAccess.objects.filter(
            user=self.target,
            benefit_plan=self.rmep,
            is_deleted=False,
        ).exists())

    def test_superuser_can_manage_any_plan(self):
        self.validate_and_assign(self.superuser, self.target, self.rmep)
        self.assertTrue(UserBenefitPlanAccess.objects.filter(
            user=self.target,
            benefit_plan=self.rmep,
            is_deleted=False,
        ).exists())

    def test_revocation_rejects_assignment_from_inaccessible_plan(self):
        assignment = self.assign(self.target, self.rmep, self.manager)
        with self.assertRaises(PermissionDenied):
            RevokeBenefitPlanUserAccessMutation._mutate(
                self.manager,
                id=assignment.id,
            )

    def test_deleted_plan_assignments_are_retained_but_fail_closed(self):
        plan = BenefitPlan.objects.get(id=self.upg.id)
        assignment = UserBenefitPlanAccess.objects.get(
            user=self.manager,
            benefit_plan=plan,
            is_deleted=False,
        )

        plan.delete(user=self.manager)

        self.assertTrue(UserBenefitPlanAccess.objects.filter(
            id=assignment.id,
            is_deleted=False,
        ).exists())
        self.assertFalse(can_access_benefit_plan(self.manager, plan.id))
        with self.assertRaisesMessage(
            ValidationError,
            "mutation.benefit_plan_not_found",
        ):
            Query().resolve_benefit_plan_user_access(
                SimpleNamespace(context=SimpleNamespace(user=self.manager)),
                benefit_plan_id=plan.id,
            )
        with self.assertRaises(PermissionDenied):
            RevokeBenefitPlanUserAccessMutation._mutate(
                self.manager,
                id=assignment.id,
            )

        plan.is_deleted = False
        plan.save(user=self.manager)

        self.assertTrue(can_access_benefit_plan(self.manager, plan.id))
        restored_assignments = Query().resolve_benefit_plan_user_access(
            SimpleNamespace(context=SimpleNamespace(user=self.manager)),
            benefit_plan_id=plan.id,
        )
        self.assertTrue(restored_assignments.filter(id=assignment.id).exists())

    def test_direct_node_lookup_hides_inaccessible_assignment(self):
        assignment = self.assign(self.target, self.rmep, self.manager)
        node_id = to_global_id(
            "UserBenefitPlanAccessGQLType",
            assignment.id,
        )
        result = self.execute(
            """
            query AssignmentNode($id: ID!) {
              node(id: $id) { id }
            }
            """,
            variables={"id": node_id},
        )
        self.assertIsNone(result.errors)
        self.assertIsNone(result.data["node"])

    def test_direct_node_lookup_allows_global_access_manager(self):
        assignment = self.assign(self.target, self.rmep, self.manager)
        self.manager.has_perms = lambda perms: perms in (
            ["160006"],
            ["160007"],
        )
        node_id = to_global_id(
            "UserBenefitPlanAccessGQLType",
            assignment.id,
        )
        result = self.execute(
            """
            query AssignmentNode($id: ID!) {
              node(id: $id) { id }
            }
            """,
            variables={"id": node_id},
        )
        self.assertIsNone(result.errors, result.errors)
        self.assertIsNotNone(result.data["node"])

    def test_resolver_returns_only_requested_authorised_plan(self):
        upg_target = self.assign(self.target, self.upg, self.manager)
        self.assign(self.target, self.rmep, self.manager)
        info = SimpleNamespace(context=SimpleNamespace(user=self.manager))
        result = list(Query().resolve_benefit_plan_user_access(
            info,
            benefit_plan_id=self.upg.id,
        ))
        self.assertEqual(result, [
            UserBenefitPlanAccess.objects.get(id=self.manager.benefit_plan_accesses.get(
                benefit_plan=self.upg,
                is_deleted=False,
            ).id),
            upg_target,
        ])
        with self.assertRaises(PermissionDenied):
            Query().resolve_benefit_plan_user_access(
                info,
                benefit_plan_id=self.rmep.id,
            )
