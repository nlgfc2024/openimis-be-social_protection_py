from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import graphene
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from graphql_relay import to_global_id
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import User
from core.test_helpers import create_test_technical_user
from individual.models import Group, Individual, IndividualDataSourceUpload
from social_protection.apps import SocialProtectionConfig
from social_protection.benefit_plan_access import (
    require_benefit_plan_upload_access,
)
from social_protection.gql_mutations import (
    CloseBenefitPlanMutation,
    CreateBeneficiaryMutation,
    CreateGroupBeneficiaryMutation,
    DeleteBeneficiaryMutation,
    DeleteBenefitPlanMutation,
    DeleteGroupBeneficiaryMutation,
    UndoDeleteBenefitPlanMutation,
    UpdateBeneficiaryMutation,
    UpdateBenefitPlanMutation,
    UpdateGroupBeneficiaryMutation,
)
from social_protection.gql_queries import (
    BeneficiaryGQLType,
    BenefitPlanDataUploadQGLType,
    BenefitPlanGQLType,
    BenefitPlanHistoryGQLType,
    GroupBeneficiaryGQLType,
)
from social_protection.models import (
    Beneficiary,
    BenefitPlan,
    BenefitPlanDataUploadRecords,
    GroupBeneficiary,
    UserBenefitPlanAccess,
)
from social_protection.schema import Query
from social_protection.services import BeneficiaryImportService
from social_protection import views


@override_settings(ROW_SECURITY=False)
class Track03BenefitPlanAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_technical_user(username="track03-user")
        cls.audit_user = create_test_technical_user(
            username="track03-auditor",
        )
        cls.upg = BenefitPlan(
            code="T03UPG001",
            name="Track 03 UPG",
            type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE,
            beneficiary_data_schema={"properties": {"upg": {}}},
        )
        cls.upg.save(user=cls.audit_user)
        cls.rmep = BenefitPlan(
            code="T03RMEP1",
            name="Track 03 RMEP",
            type=BenefitPlan.BenefitPlanType.GROUP_TYPE,
            beneficiary_data_schema={"properties": {"rmep": {}}},
        )
        cls.rmep.save(user=cls.audit_user)

        cls.individual = Individual(
            first_name="Track",
            last_name="Three",
            dob=date(1990, 1, 1),
        )
        cls.individual.save(user=cls.audit_user)
        cls.group = Group(code="T03GROUP")
        cls.group.save(user=cls.audit_user)
        cls.upg_beneficiary = Beneficiary(
            individual=cls.individual,
            benefit_plan=cls.upg,
            status="POTENTIAL",
        )
        cls.upg_beneficiary.save(user=cls.audit_user)
        cls.rmep_group_beneficiary = GroupBeneficiary(
            group=cls.group,
            benefit_plan=cls.rmep,
            status="POTENTIAL",
        )
        cls.rmep_group_beneficiary.save(user=cls.audit_user)

        cls.upload = IndividualDataSourceUpload(
            source_name="track03.csv",
            source_type="text/csv",
            status=IndividualDataSourceUpload.Status.PENDING,
            error={},
            json_ext={},
        )
        cls.upload.save(user=cls.audit_user)
        cls.upload_record = BenefitPlanDataUploadRecords(
            data_upload=cls.upload,
            benefit_plan=cls.rmep,
            workflow="track03",
        )
        cls.upload_record.save(user=cls.audit_user)

    def setUp(self):
        self.original_enabled = (
            SocialProtectionConfig.enable_benefit_plan_access_control
        )
        self.original_all = (
            SocialProtectionConfig.gql_benefit_plan_all_access_perms
        )
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = ["160006"]
        UserBenefitPlanAccess.objects.filter(user=self.user).delete()
        self.assign(self.upg)

    def tearDown(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = (
            self.original_enabled
        )
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = (
            self.original_all
        )

    def assign(self, plan):
        assignment = UserBenefitPlanAccess(
            user=self.user,
            benefit_plan=plan,
        )
        assignment.save(user=self.audit_user)
        return assignment

    def info(self):
        return SimpleNamespace(
            context=SimpleNamespace(user=self.user, headers={}),
        )

    def permission_patch(self):
        return patch.object(
            User,
            "has_perms",
            side_effect=lambda permissions: "160006" not in permissions,
        )

    def test_model_querysets_keep_row_scope_and_add_plan_scope(self):
        self.assertQuerySetEqual(
            Beneficiary.get_queryset(None, self.user),
            [self.upg_beneficiary],
        )
        self.assertFalse(
            GroupBeneficiary.get_queryset(None, self.user).exists(),
        )

        self.assign(self.rmep)
        self.assertQuerySetEqual(
            GroupBeneficiary.get_queryset(None, self.user),
            [self.rmep_group_beneficiary],
        )

    def test_feature_disabled_preserves_existing_model_querysets(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        self.assertTrue(
            Beneficiary.get_queryset(None, self.user).filter(
                id=self.upg_beneficiary.id,
            ).exists(),
        )
        self.assertTrue(
            GroupBeneficiary.get_queryset(None, self.user).filter(
                id=self.rmep_group_beneficiary.id,
            ).exists(),
        )

    def test_query_resolvers_scope_lists_history_uploads_and_schema_fields(
        self,
    ):
        info = self.info()
        query = Query()
        with self.permission_patch(), patch.object(
            Query,
            "_check_permissions",
        ), patch(
            "social_protection.schema.gql_optimizer.query",
            side_effect=lambda queryset, _info: queryset,
        ):
            plans = query.resolve_benefit_plan(info)
            histories = query.resolve_benefit_plan_history(info)
            uploads = query.resolve_beneficiary_data_upload_history(info)
            schema_plans = query.resolve_benefit_plan_schema_field(info)

        self.assertEqual(
            set(plans.values_list("id", flat=True)),
            {self.upg.id},
        )
        self.assertEqual(
            set(histories.values_list("id", flat=True)),
            {self.upg.id},
        )
        self.assertFalse(uploads.exists())
        self.assertEqual(
            set(schema_plans.values_list("id", flat=True)),
            {self.upg.id},
        )

    def test_nodes_and_nested_foreign_keys_hide_unassigned_plans(self):
        info = self.info()
        rmep_history = self.rmep.history.first()
        with self.permission_patch():
            self.assertIsNone(BenefitPlanGQLType.get_node(
                info,
                self.rmep.id,
            ))
            self.assertIsNone(GroupBeneficiaryGQLType.get_node(
                info,
                self.rmep_group_beneficiary.id,
            ))
            self.assertIsNone(BenefitPlanDataUploadQGLType.get_node(
                info,
                self.upload_record.id,
            ))
            self.assertIsNone(BenefitPlanHistoryGQLType.get_node(
                info,
                rmep_history.pk,
            ))
            self.assertIsNone(
                GroupBeneficiaryGQLType.resolve_benefit_plan(
                    self.rmep_group_beneficiary,
                    info,
                )
            )
            self.assertEqual(
                BeneficiaryGQLType.resolve_benefit_plan(
                    self.upg_beneficiary,
                    info,
                ),
                self.upg,
            )

    def test_nested_querysets_still_require_existing_action_permissions(self):
        info = self.info()
        with patch.object(User, "has_perms", return_value=False):
            self.assertFalse(BenefitPlanGQLType.get_queryset(
                BenefitPlan.objects.all(),
                info,
            ).exists())
            self.assertFalse(BeneficiaryGQLType.get_queryset(
                Beneficiary.objects.all(),
                info,
            ).exists())
            self.assertFalse(GroupBeneficiaryGQLType.get_queryset(
                GroupBeneficiary.objects.all(),
                info,
            ).exists())
            self.assertFalse(BenefitPlanDataUploadQGLType.get_queryset(
                BenefitPlanDataUploadRecords.objects.all(),
                info,
            ).exists())
            self.assertFalse(BenefitPlanHistoryGQLType.get_queryset(
                BenefitPlan.history.all(),
                info,
            ).exists())

    def test_graphql_count_and_node_do_not_reveal_unassigned_plan(self):
        schema = graphene.Schema(query=Track03NodeQuery)
        rmep_node_id = to_global_id("BenefitPlanGQLType", self.rmep.id)
        query = """
            query Track03($node: ID!) {
              benefitPlan(isDeleted: false) {
                totalCount
                edges { node { code } }
              }
              node(id: $node) { id }
            }
        """
        with self.permission_patch():
            result = schema.execute(
                query,
                variable_values={"node": rmep_node_id},
                context_value=SimpleNamespace(user=self.user, headers={}),
            )
        self.assertFalse(result.errors, result.errors)
        self.assertEqual(result.data["benefitPlan"]["totalCount"], 1)
        self.assertEqual(
            result.data["benefitPlan"]["edges"][0]["node"]["code"],
            self.upg.code,
        )
        self.assertIsNone(result.data["node"])

    def test_all_plan_permission_and_superuser_bypass_assignment_scope(self):
        with patch.object(User, "has_perms", return_value=True):
            all_plan_ids = set(BenefitPlanGQLType.get_queryset(
                BenefitPlan.objects.filter(is_deleted=False),
                self.info(),
            ).values_list("id", flat=True))
            self.assertTrue(
                {self.upg.id, self.rmep.id}.issubset(all_plan_ids),
            )
        self.user.t_user.is_superuser = True
        self.user.t_user.save()
        superuser_plan_ids = set(BenefitPlanGQLType.get_queryset(
            BenefitPlan.objects.filter(is_deleted=False),
            self.info(),
        ).values_list("id", flat=True))
        self.assertTrue(
            {self.upg.id, self.rmep.id}.issubset(superuser_plan_ids),
        )

    def test_beneficiary_mutations_validate_origin_and_destination(self):
        with self.permission_patch():
            with self.assertRaises(PermissionDenied):
                CreateGroupBeneficiaryMutation._validate_mutation(
                    self.user,
                    group_id=self.group.id,
                    benefit_plan_id=self.rmep.id,
                    status="POTENTIAL",
                )
            with self.assertRaises(PermissionDenied):
                UpdateBeneficiaryMutation._validate_mutation(
                    self.user,
                    id=self.upg_beneficiary.id,
                    benefit_plan_id=self.rmep.id,
                    status="POTENTIAL",
                )
            with self.assertRaises(PermissionDenied):
                DeleteGroupBeneficiaryMutation._validate_mutation(
                    self.user,
                    ids=[self.rmep_group_beneficiary.id],
                )

            CreateBeneficiaryMutation._validate_mutation(
                self.user,
                individual_id=self.individual.id,
                benefit_plan_id=self.upg.id,
                status="POTENTIAL",
            )
            DeleteBeneficiaryMutation._validate_mutation(
                self.user,
                ids=[self.upg_beneficiary.id],
            )

            self.assign(self.rmep)
            CreateGroupBeneficiaryMutation._validate_mutation(
                self.user,
                group_id=self.group.id,
                benefit_plan_id=self.rmep.id,
                status="POTENTIAL",
            )
            UpdateBeneficiaryMutation._validate_mutation(
                self.user,
                id=self.upg_beneficiary.id,
                benefit_plan_id=self.rmep.id,
                status="POTENTIAL",
            )
            UpdateGroupBeneficiaryMutation._validate_mutation(
                self.user,
                id=self.rmep_group_beneficiary.id,
                benefit_plan_id=self.rmep.id,
                status="POTENTIAL",
            )
            DeleteGroupBeneficiaryMutation._validate_mutation(
                self.user,
                ids=[self.rmep_group_beneficiary.id],
            )

    def test_benefit_plan_mutations_require_plan_access(self):
        with self.permission_patch():
            validators = (
                (UpdateBenefitPlanMutation, {"id": self.rmep.id}),
                (DeleteBenefitPlanMutation, {"ids": [self.rmep.id]}),
                (UndoDeleteBenefitPlanMutation, {"ids": [self.rmep.id]}),
                (CloseBenefitPlanMutation, {"ids": [self.rmep.id]}),
            )
            for mutation, data in validators:
                with self.subTest(mutation=mutation.__name__):
                    with self.assertRaises(PermissionDenied):
                        mutation._validate_mutation(self.user, **data)

    def test_restore_accepts_retained_assignment_for_deleted_plan(self):
        self.upg.is_deleted = True
        self.upg.save(user=self.audit_user)
        with self.permission_patch():
            UndoDeleteBenefitPlanMutation._validate_mutation(
                self.user,
                ids=[self.upg.id],
            )

    def test_upload_binding_rejects_cross_plan_upload(self):
        with self.assertRaises(PermissionDenied):
            require_benefit_plan_upload_access(
                self.user,
                self.upload.id,
                self.upg.id,
            )

    def test_direct_import_service_methods_require_plan_access(self):
        service = BeneficiaryImportService(self.user)
        import_file = SimpleUploadedFile("track03.csv", b"first_name\n")
        workflow = SimpleNamespace(name="track03")
        calls = (
            lambda: service.import_beneficiaries(
                import_file,
                self.rmep,
                workflow,
                None,
            ),
            lambda: service.validate_import_beneficiaries(
                self.upload.id,
                [],
                self.rmep,
            ),
            lambda: service.create_task_with_importing_valid_items(
                self.upload.id,
                self.rmep,
            ),
            lambda: service.create_task_with_update_valid_items(
                self.upload.id,
                self.rmep,
            ),
            lambda: service.synchronize_data_for_reporting(
                self.upload.id,
                self.rmep,
            ),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(PermissionDenied):
                    call()

    def test_all_seven_rest_views_return_forbidden_for_unassigned_plan(self):
        factory = APIRequestFactory()
        import_file = SimpleUploadedFile("track03.csv", b"first_name\n")
        workflow = SimpleNamespace(name="track03")
        cases = (
            (
                views.download_template_benefit_plan_file,
                factory.get("/template", {"benefit_plan_uuid": self.rmep.id}),
                None,
            ),
            (
                views.import_beneficiaries,
                factory.post("/import", {}, format="multipart"),
                patch(
                    "social_protection.views."
                    "_resolve_import_beneficiaries_args",
                    return_value=(import_file, workflow, self.rmep, None),
                ),
            ),
            (
                views.validate_import_beneficiaries,
                factory.post("/validate", {}, format="json"),
                patch(
                    "social_protection.views."
                    "_resolve_validate_import_beneficiaries_args",
                    return_value=(self.upload.id, [], self.rmep),
                ),
            ),
            (
                views.create_task_with_importing_valid_items,
                factory.post("/task", {}, format="json"),
                patch(
                    "social_protection.views."
                    "_resolve_create_task_with_importing_valid_items",
                    return_value=(self.upload.id, self.rmep),
                ),
            ),
            (
                views.download_invalid_items,
                factory.get("/invalid", {"upload_id": self.upload.id}),
                None,
            ),
            (
                views.download_beneficiary_upload,
                factory.get(
                    "/upload",
                    {
                        "benefit_plan_id": self.rmep.id,
                        "filename": "track03.csv",
                    },
                ),
                None,
            ),
            (
                views.synchronize_data_for_reporting,
                factory.post("/sync", {}, format="json"),
                patch(
                    "social_protection.views."
                    "_resolve_synchronize_data_for_reporting",
                    return_value=(self.upload.id, self.rmep),
                ),
            ),
        )

        with self.permission_patch():
            for view, request, resolver_patch in cases:
                with self.subTest(view=view.__name__), ExitStack() as stack:
                    if resolver_patch is not None:
                        stack.enter_context(resolver_patch)
                    force_authenticate(request, user=self.user)
                    response = view(request)
                    self.assertEqual(response.status_code, 403)


class Track03NodeQuery(Query, graphene.ObjectType):
    node = graphene.relay.Node.Field()
