from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import graphene
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase
from graphql_relay import to_global_id

from core.models import MutationLog
from core.test_helpers import (
    create_test_interactive_user,
    create_test_technical_user,
)
from social_protection.apps import SocialProtectionConfig
from social_protection.benefit_plan_access import (
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

    def test_flag_disabled_rejects_160007_for_assign_list_and_revoke(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        self.grant("160007")

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
            RevokeBenefitPlanUserAccessMutation._validate_mutation(
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
        "require_benefit_plan_assignment_management",
        side_effect=PermissionDenied,
    )
    @patch("social_protection.gql_queries.UserBenefitPlanAccess.objects.filter")
    def test_direct_node_lookup_enforces_management_policy(
        self,
        assignment_filter,
        require_management,
    ):
        assignment = SimpleNamespace(benefit_plan_id=self.plan_id)
        assignment_filter.return_value.select_related.return_value.first.return_value = (
            assignment
        )
        info = SimpleNamespace(context=SimpleNamespace(user=self.requester))

        with self.assertRaises(PermissionDenied):
            UserBenefitPlanAccessGQLType.get_node(
                info,
                str(uuid4()),
            )
        require_management.assert_called_once_with(
            self.requester,
            self.plan_id,
        )

    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.UserBenefitPlanAccess.objects.filter")
    @patch("social_protection.gql_mutations.User.objects.filter")
    @patch("social_protection.gql_mutations.BenefitPlan.objects.filter")
    def test_authorised_assignment_validation(
        self,
        benefit_plan_filter,
        user_filter,
        assignment_filter,
        require_management,
    ):
        benefit_plan_filter.return_value.exists.return_value = True
        user_filter.return_value.exists.return_value = True
        assignment_filter.return_value.exists.return_value = False

        AssignBenefitPlanUserMutation._validate_mutation(
            self.requester,
            user_id=self.target_id,
            benefit_plan_id=self.plan_id,
        )
        require_management.assert_called_once_with(self.requester, self.plan_id)

    @patch("social_protection.gql_mutations.require_benefit_plan_assignment_management")
    @patch("social_protection.gql_mutations.UserBenefitPlanAccess.objects.filter")
    @patch("social_protection.gql_mutations.User.objects.filter")
    @patch("social_protection.gql_mutations.BenefitPlan.objects.filter")
    def test_duplicate_assignment_validation(
        self,
        benefit_plan_filter,
        user_filter,
        assignment_filter,
        _require_management,
    ):
        benefit_plan_filter.return_value.exists.return_value = True
        user_filter.return_value.exists.return_value = True
        assignment_filter.return_value.exists.return_value = True
        with self.assertRaises(ValidationError):
            AssignBenefitPlanUserMutation._validate_mutation(
                self.requester,
                user_id=self.target_id,
                benefit_plan_id=self.plan_id,
            )

    @patch("social_protection.gql_mutations.UserBenefitPlanAccess")
    def test_assignment_is_audited(self, assignment_model):
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

    @patch("social_protection.gql_mutations.RevokeBenefitPlanUserAccessMutation._active_assignment")
    def test_revocation_uses_soft_delete_with_audit_user(self, active_assignment):
        assignment = active_assignment.return_value
        RevokeBenefitPlanUserAccessMutation._mutate(
            self.requester,
            id=uuid4(),
        )
        assignment.delete.assert_called_once_with(user=self.requester)

    @patch("social_protection.schema.require_benefit_plan_assignment_management")
    @patch("social_protection.schema.UserBenefitPlanAccess.objects.filter")
    @patch("social_protection.schema.BenefitPlan.objects.filter")
    def test_resolver_scopes_to_requested_plan(
        self,
        benefit_plan_filter,
        access_filter,
        require_management,
    ):
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


class BenefitPlanUserAccessGraphQLTest(TestCase):
    @classmethod
    def setUpTestData(cls):
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
            RevokeBenefitPlanUserAccessMutation._validate_mutation(
                self.manager,
                id=assignment.id,
            )

    def test_direct_node_lookup_rejects_inaccessible_assignment(self):
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
        self.assertTrue(result.errors)
        self.assertIsNone(result.data["node"])

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
