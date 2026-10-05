import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.core.exceptions import ValidationError
from django.db import DataError
from django.test import SimpleTestCase, TestCase

from core.test_helpers import LogInHelper
from individual.services import (
    IndividualService,
    _criterion_to_condition,
    build_group_enrollment_queryset,
    build_individual_enrollment_queryset,
    build_individual_enrollment_selection,
    build_group_enrollment_selection,
)
from social_protection.enrolment_ranking import (
    calculate_cap,
    build_order_by,
    load_ranking_spec,
    rank_and_cap_queryset,
    validate_ranking_spec,
)
from individual.models import Individual
from individual.schema import Query
from individual.custom_filters import GroupCustomFilterWizard, IndividualCustomFilterWizard
from individual.tests.test_helpers import create_group, create_individual
from social_protection.tests.test_helpers import create_benefit_plan
from social_protection.apps import DEFAULT_CONFIG, SocialProtectionConfig
from social_protection.models import Beneficiary, GroupBeneficiary
from social_protection.program_policies import (
    policy_criteria,
    resolve_program_policy,
    runtime_policy_selection,
    validate_program_specific_enrolment_criteria,
)


class ProgramPolicyResolutionTest(SimpleTestCase):
    def test_default_pwp_policy_restricts_candidates_to_pwp_source(self):
        criteria = policy_criteria(
            DEFAULT_CONFIG["program_specific_enrolment_criteria"]["PWP"]
        )

        self.assertIn({
            "field": "beneficiary_data_source",
            "filter": "exact",
            "type": "string",
            "value": "PWP",
        }, criteria)

    def test_disabled_program_specific_flag_always_uses_pwp(self):
        pwp = {"selection_criteria": {"fit_for_work": 1}}
        rmep = {"selection_criteria": {"business_experience": True}}
        with patch.object(SocialProtectionConfig, "use_program_specific_enrolment_criteria", False), \
             patch.object(SocialProtectionConfig, "program_specific_enrolment_criteria", {"PWP": pwp, "RMEP": rmep}):
            self.assertIs(resolve_program_policy(SimpleNamespace(code="RMEP")), pwp)

    def test_enabled_program_specific_flag_requires_a_code(self):
        with patch.object(SocialProtectionConfig, "use_program_specific_enrolment_criteria", True), \
             patch.object(SocialProtectionConfig, "program_specific_enrolment_criteria", {"PWP": {}}):
            with self.assertRaisesMessage(ValidationError, "Benefit Plan code is required"):
                resolve_program_policy(SimpleNamespace(code=""))

    def test_enabled_program_specific_flag_requires_matching_policy(self):
        with patch.object(SocialProtectionConfig, "use_program_specific_enrolment_criteria", True), \
             patch.object(SocialProtectionConfig, "program_specific_enrolment_criteria", {"PWP": {}}):
            with self.assertRaisesMessage(ValidationError, "No enrolment policy"):
                resolve_program_policy(SimpleNamespace(code="RMEP"))

    def test_enabled_program_specific_flag_uses_matching_code(self):
        rmep = {"selection_criteria": {"business_experience": True}}
        with patch.object(SocialProtectionConfig, "use_program_specific_enrolment_criteria", True), \
             patch.object(SocialProtectionConfig, "program_specific_enrolment_criteria", {"PWP": {}, "RMEP": rmep}):
            self.assertIs(resolve_program_policy(SimpleNamespace(code="RMEP")), rmep)


class _PolicyRows:
    """Small queryset stand-in for policy-selection tests."""

    def __init__(self, rows):
        self.rows = rows

    def values(self, *fields):
        return [{field: row[field] for field in fields} for row in self.rows]


class ProgramConfigurationEndToEndTest(SimpleTestCase):
    configuration = {
        "use_program_specific_enrolment_criteria": True,
        "program_specific_enrolment_criteria": {
            "RMEP": {
                "sorting_strategy": [
                    {"key": "business_capital", "weight": {"< 1000": 0.5, "1000-5000": 1, "5001-10000": 2, "10001-20000": 3, "> 20000": 5}},
                    {"key": "type_of_business", "weight": {"Retail": 1, "Service": 2, "Manufacturing": 3, "Agriculture": 4}},
                    {"key": "business_period", "weight": {"< 1 year": 0.5, "1-3 years": 1, "4-6 years": 2, "7-10 years": 3, "> 10 years": 5}},
                    {"key": "business_skills", "weight": {"Formal training": 3, "Apprenticeship": 2, "Learned from family/community": 1, "Self-taught": 1, "Previous work/business experience": 2}},
                    {"key": "prior_knowledge", "weight": {"Technical/Production": 3, "Business management": 2, "Financial literacy": 2, "Marketing/sales": 1}},
                ],
                "selection_criteria": {
                    "data_source": "PWP",
                    "validation_status": "VERIFIED",
                    "primary_worker": True,
                    "business_experience": True,
                    "selection_strategy": {"female": 60, "youth": 40},
                },
            },
            "UPG": {
                "sorting_strategy": [
                    {"key": "savings_experience", "weight": {"No": 0, "Yes": 1}},
                    {"key": "sanitary_facilities", "weight": {"Toilet": 1, "Mponda gear": 1, "Kitchen": 1, "Rubbish pit": 1, "Bathroom": 1}},
                ],
                "selection_criteria": {
                    "data_source": "SCTP",
                    "validation_status": "VERIFIED",
                    "willingness_to_participate": True,
                    "Age_limit": {"min_age": 18, "max_age": 60},
                },
            },
        },
    }

    def _policy(self, code):
        return self.configuration["program_specific_enrolment_criteria"][code]

    def test_full_given_configuration_is_supported(self):
        validate_program_specific_enrolment_criteria(
            self.configuration["program_specific_enrolment_criteria"]
        )

    def test_rmep_eligibility_fields_are_translated_to_filters(self):
        criteria = policy_criteria(self._policy("RMEP"))
        self.assertEqual(
            [(item["field"], item["value"]) for item in criteria],
            [
                ("beneficiary_data_source", "PWP"),
                ("validation_status", "VERIFIED"),
                ("is_primary_worker", True),
                ("business_experience", "Yes"),
            ],
        )

    def test_upg_eligibility_fields_are_translated_to_filters(self):
        criteria = policy_criteria(self._policy("UPG"))
        self.assertEqual(
            [(item["field"], item["filter"], item["value"]) for item in criteria],
            [
                ("beneficiary_data_source", "exact", "SCTP"),
                ("validation_status", "exact", "VERIFIED"),
                ("willingness_to_participate", "exact", True),
                ("age", "gte", 18),
                ("age", "lte", 60),
            ],
        )

    def test_pwp_boolean_fit_for_work_is_translated_to_integer(self):
        criteria = policy_criteria({
            "selection_criteria": {"fit_for_work": True},
        })

        self.assertEqual(criteria, [{
            "field": "fit_for_work",
            "filter": "exact",
            "type": "integer",
            "value": 1,
        }])

    def test_rmep_selection_is_weighted_then_60_40_prioritised(self):
        rows = _PolicyRows([
            {"id": "female_high", "json_ext": {"gender": "famale", "age": 40, "business_capital": 21000, "type_of_business": "Agriculture", "business_period": 11, "business_skills": "Formal training", "prior_knowledge": "Technical/Production"}},
            {"id": "female_mid", "json_ext": {"gender": "famale", "age": 40, "business_capital": 5001, "type_of_business": "Service", "business_period": 4, "business_skills": "Apprenticeship", "prior_knowledge": "Financial literacy"}},
            {"id": "female_low", "json_ext": {"gender": "famale", "age": 40, "business_capital": 1000, "type_of_business": "Retail", "business_period": 1, "business_skills": "Previous work/business experience", "prior_knowledge": "Marketing/sales"}},
            {"id": "youth_high", "json_ext": {"gender": "male", "age": 25, "business_capital": 10001, "type_of_business": "Manufacturing", "business_period": 7, "business_skills": "Formal training", "prior_knowledge": "Technical/Production"}},
            {"id": "youth_low", "json_ext": {"gender": "male", "age": 21, "business_capital": 999, "type_of_business": "Retail", "business_period": 0, "business_skills": "Self-taught", "prior_knowledge": "Marketing/sales"}},
            {"id": "other", "json_ext": {"gender": "male", "age": 44, "business_capital": 21000, "type_of_business": "Service", "business_period": 11, "business_skills": "Formal training", "prior_knowledge": "Technical/Production"}},
        ])
        with patch.object(SocialProtectionConfig, "use_program_specific_enrolment_criteria", True), \
             patch.object(SocialProtectionConfig, "program_specific_enrolment_criteria", self.configuration["program_specific_enrolment_criteria"]):
            selected = runtime_policy_selection(rows, SimpleNamespace(code="RMEP"), "ACTIVE", 5)
        self.assertEqual(
            selected,
            ["female_high", "female_mid", "female_low", "youth_high", "youth_low"],
        )

    def test_upg_sorting_applies_without_a_quota_strategy(self):
        rows = _PolicyRows([
            {"id": "lowest", "json_ext": {"savings_experience": "No", "sanitary_facilities": []}},
            {"id": "middle", "json_ext": {"savings_experience": "Yes", "sanitary_facilities": ["Toilet"]}},
            {"id": "highest", "json_ext": {"savings_experience": "Yes", "sanitary_facilities": ["Toilet", "Kitchen", "Bathroom"]}},
        ])
        with patch.object(SocialProtectionConfig, "use_program_specific_enrolment_criteria", True), \
             patch.object(SocialProtectionConfig, "program_specific_enrolment_criteria", self.configuration["program_specific_enrolment_criteria"]):
            selected = runtime_policy_selection(rows, SimpleNamespace(code="UPG"), "ACTIVE", 3)
        self.assertEqual(selected, ["highest", "middle", "lowest"])


class EnrollmentCriterionNormalizationTest(SimpleTestCase):
    def test_custom_filter_string_cast_decodes_json_quoting(self):
        wizard = IndividualCustomFilterWizard()
        cast_value = wizard._IndividualCustomFilterWizard__cast_value

        self.assertEqual(cast_value('"Karonga"', 'string'), "Karonga")
        self.assertEqual(
            cast_value('"Nkhata \\"Bay\\""', 'string'),
            'Nkhata "Bay"',
        )

    def test_custom_filter_string_cast_preserves_legacy_unquoted_values(self):
        wizard = IndividualCustomFilterWizard()
        cast_value = wizard._IndividualCustomFilterWizard__cast_value

        self.assertEqual(cast_value("Karonga", "string"), "Karonga")

    def test_individual_custom_filter_preserves_equals_in_string_value(self):
        query = Mock()
        query.filter.return_value = query

        result = IndividualCustomFilterWizard().apply_filter_to_queryset(
            ['district__exact__string="a=b"'],
            query,
        )

        query.filter.assert_called_once_with(json_ext__district__exact="a=b")
        self.assertIs(result, query)

    def test_group_custom_filter_preserves_equals_in_string_value(self):
        query = Mock()
        query.filter.return_value = query

        result = GroupCustomFilterWizard().apply_filter_to_queryset(
            ['district__exact__string="a=b"'],
            query,
        )

        query.filter.assert_called_once_with(json_ext__district__exact="a=b")
        self.assertIs(result, query)

    def test_canonical_string_value_is_quoted_for_custom_filter_casting(self):
        condition = _criterion_to_condition({
            "field": "validation_status",
            "filter": "exact",
            "type": "string",
            "value": "VERIFIED",
        })
        self.assertEqual(
            condition,
            'validation_status__exact__string="VERIFIED"',
        )

    def test_existing_condition_is_preserved(self):
        condition = "score__gte__integer=10"
        self.assertEqual(
            _criterion_to_condition({"custom_filter_condition": condition}),
            condition,
        )

    def test_ranking_validation_rejects_unknown_field_and_bad_limit(self):
        with self.assertRaisesMessage(ValidationError, "Unsupported ranking field"):
            validate_ranking_spec(
                {"order_by": ["does_not_exist"]}, Individual
            )
        with self.assertRaisesMessage(ValidationError, "between 1 and 100"):
            validate_ranking_spec(
                {"order_by": ["id"], "limit": {"percentage": 101}},
                Individual,
            )

    def test_cap_uses_ceiling_and_smaller_remaining_capacity(self):
        self.assertEqual(calculate_cap(7, 20, None), (2, True))
        self.assertEqual(calculate_cap(100, 20, 15, 3), (12, True))
        self.assertEqual(calculate_cap(100, 20, 10, 12), (0, True))
        self.assertEqual(calculate_cap(7, None, None), (7, False))

    def test_tie_breaker_is_always_normalised_to_last_position(self):
        order_items, _, _ = validate_ranking_spec(
            {"order_by": ["id", "last_name"], "tie_breaker": "id"},
            Individual,
        )
        self.assertEqual([item["field"] for item in order_items], ["last_name", "id"])

    def test_string_json_ext_snapshot_uses_status_then_wildcard(self):
        plan = Mock(json_ext='{"enrolment_ranking":{"*":{"order_by":["id"]},"ACTIVE":{"order_by":["-id"]}}}')
        self.assertEqual(load_ranking_spec(plan, "ACTIVE")["order_by"], ["-id"])
        self.assertEqual(load_ranking_spec(plan, "POTENTIAL")["order_by"], ["id"])

    def test_string_and_object_ordering_compile_as_annotated_distinct_sql(self):
        order_items, _, _ = validate_ranking_spec({
            "order_by": [
                "-dob",
                {
                    "field": "json_ext__score",
                    "cast": "int",
                    "direction": "asc",
                    "nulls": "last",
                },
            ]
        }, Individual)
        query = build_order_by(Individual.objects.all().distinct(), order_items)
        sql = str(query.query)

        self.assertIn("DISTINCT", sql)
        self.assertIn("_enrolment_rank_", sql)
        self.assertIn("NULLS LAST", sql)
        self.assertIn("integer", sql)

    @patch(
        "social_protection.program_policies.runtime_policy_selection",
        return_value=None,
    )
    @patch("social_protection.enrolment_ranking.build_order_by")
    def test_cast_data_error_names_the_configured_field(
        self,
        build_ranking,
        policy_selection,
    ):
        queryset = MagicMock()
        queryset.count.return_value = 1
        queryset.model = Individual
        ranked = MagicMock()
        build_ranking.return_value = ranked
        ranked.values_list.return_value.__getitem__.return_value.__iter__.side_effect = (
            DataError('invalid input syntax for type integer: "Poorer"')
        )
        benefit_plan = Mock(
            json_ext={
                "enrolment_ranking": {
                    "*": {
                        "order_by": [{
                            "field": "json_ext__household_wealth_quintile",
                            "cast": "int",
                        }]
                    }
                }
            },
            max_beneficiaries=None,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "json_ext__household_wealth_quintile (int)",
        ):
            rank_and_cap_queryset(queryset, benefit_plan, "ACTIVE", 0)

    @patch("individual.services.build_individual_enrollment_selection")
    def test_confirmation_signal_result_contains_authoritative_queryset(
        self,
        build_selection,
    ):
        authoritative_queryset = Mock()
        not_assigned_queryset = Mock()
        build_selection.return_value = {
            "individual_query_with_filters": authoritative_queryset,
            "individuals_not_assigned_to_selected_programme": not_assigned_queryset,
        }

        service = IndividualService(Mock())
        result = IndividualService.select_individuals_to_benefit_plan.__wrapped__(
            service,
            [],
            "plan-id",
            "ACTIVE",
            service.user,
        )

        self.assertIs(
            result["individual_query_with_filters"],
            authoritative_queryset,
        )
        self.assertIs(
            result["individuals_not_assigned_to_selected_programme"],
            not_assigned_queryset,
        )


class EnrollmentPreviewResolverTest(SimpleTestCase):
    def setUp(self):
        self.info = Mock()
        self.info.context.user = Mock()

    @patch("individual.schema.gql_optimizer.query")
    @patch("individual.schema.build_individual_enrollment_selection")
    @patch.object(Query, "_check_permissions")
    def test_individual_preview_is_derived_server_side(
        self, check_permissions, build_selection, optimize
    ):
        selected = Mock()
        build_selection.return_value = {
            "individuals_not_assigned_to_selected_programme": selected,
        }
        optimize.return_value = selected

        result = Query().resolve_individual(
            self.info,
            customFilters=['score__gte__integer=10'],
            benefitPlanToEnroll="plan-id",
            enrollmentPreviewStatus="ACTIVE",
        )

        build_selection.assert_called_once_with(
            ['score__gte__integer=10'],
            "plan-id",
            "ACTIVE",
            self.info.context.user,
            materialize_selected_ids=False,
        )
        optimize.assert_called_once_with(selected, self.info)
        self.assertIs(result, selected)

    @patch("individual.schema.gql_optimizer.query")
    @patch("individual.schema.build_group_enrollment_selection")
    @patch.object(Query, "_check_permissions")
    def test_group_preview_is_derived_server_side(
        self, check_permissions, build_selection, optimize
    ):
        selected = Mock()
        build_selection.return_value = {
            "groups_not_assigned_to_selected_programme": selected,
        }
        optimize.return_value = selected

        result = Query().resolve_group(
            self.info,
            customFilters=['score__gte__integer=10'],
            benefitPlanToEnroll="plan-id",
            enrollmentPreviewStatus="POTENTIAL",
        )

        build_selection.assert_called_once_with(
            ['score__gte__integer=10'],
            "plan-id",
            "POTENTIAL",
            self.info.context.user,
            materialize_selected_ids=False,
        )
        optimize.assert_called_once_with(selected, self.info)
        self.assertIs(result, selected)


class AuthoritativeEnrollmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        self.original_mandatory_criteria = copy.deepcopy(
            SocialProtectionConfig.mandatory_enrollment_criteria
        )
        SocialProtectionConfig.mandatory_enrollment_criteria = {
            "INDIVIDUAL": {},
            "GROUP": {},
        }

    def tearDown(self):
        SocialProtectionConfig.mandatory_enrollment_criteria = (
            self.original_mandatory_criteria
        )

    def _benefit_plan(self, benefit_plan_type):
        return create_benefit_plan(self.user.username, payload_override={
            "type": benefit_plan_type,
            "beneficiary_data_schema": {
                "properties": {
                    "validation_status": {"type": "string"},
                    "district": {"type": "string"},
                }
            },
            "json_ext": {
                "advanced_criteria": {
                    "ACTIVE": [{
                        "field": "validation_status",
                        "filter": "exact",
                        "type": "string",
                        "value": "VERIFIED",
                    }]
                }
            },
        })

    def test_individual_confirmation_cannot_omit_phase_criteria(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        eligible = create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "PENDING",
                "district": "Lilongwe",
            }
        })

        result = build_individual_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
        )

        selected_ids = set(result.values_list("id", flat=True))
        self.assertEqual(selected_ids, {eligible.id})

    def test_operator_filter_can_only_narrow_phase_results(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        selected = create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Blantyre",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "PENDING",
                "district": "Lilongwe",
            }
        })

        result = build_individual_enrollment_queryset(
            custom_filters=['district__exact__string="Lilongwe"'],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
        )

        selected_ids = set(result.values_list("id", flat=True))
        self.assertEqual(selected_ids, {selected.id})

    def test_rejects_operator_filter_outside_phase_schema(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")

        with self.assertRaisesMessage(ValidationError, "is not allowed"):
            build_individual_enrollment_queryset(
                custom_filters=['unconfigured__exact__string="value"'],
                benefit_plan_id=str(benefit_plan.id),
                status="ACTIVE",
            )

    def test_group_confirmation_uses_group_json_ext(self):
        benefit_plan = self._benefit_plan("GROUP")
        eligible = create_group(self.user.username, {
            "json_ext": {"validation_status": "VERIFIED"}
        })
        create_group(self.user.username, {
            "json_ext": {"validation_status": "PENDING"}
        })

        result = build_group_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
        )

        selected_ids = set(result.values_list("id", flat=True))
        self.assertEqual(selected_ids, {eligible.id})

    def test_group_immutable_rule_cannot_be_removed_from_phase_or_payload(self):
        SocialProtectionConfig.mandatory_enrollment_criteria = {
            "INDIVIDUAL": {},
            "GROUP": {
                "POTENTIAL": [{
                    "field": "validation_status",
                    "filter": "exact",
                    "type": "string",
                    "value": "VERIFIED",
                }]
            },
        }
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "GROUP",
            "beneficiary_data_schema": {
                "properties": {"validation_status": {"type": "string"}}
            },
            "json_ext": {"advanced_criteria": {"POTENTIAL": []}},
        })
        eligible = create_group(self.user.username, {
            "json_ext": {"validation_status": "VERIFIED"}
        })
        create_group(self.user.username, {
            "json_ext": {"validation_status": "NOT_VERIFIED"}
        })

        result = build_group_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="POTENTIAL",
        )

        self.assertEqual(set(result.values_list("id", flat=True)), {eligible.id})

        attempted_replacement = build_group_enrollment_queryset(
            custom_filters=[
                'validation_status__exact__string="NOT_VERIFIED"'
            ],
            benefit_plan_id=str(benefit_plan.id),
            status="POTENTIAL",
        )
        self.assertFalse(attempted_replacement.exists())

    def test_rejects_benefit_plan_type_mismatch(self):
        group_plan = self._benefit_plan("GROUP")

        with self.assertRaisesMessage(ValidationError, "cannot be used"):
            build_individual_enrollment_queryset(
                custom_filters=[],
                benefit_plan_id=str(group_plan.id),
                status="ACTIVE",
            )

    def test_numeric_json_ranking_and_percentage_cap_are_applied_together(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "max_beneficiaries": 10,
            "beneficiary_data_schema": {"properties": {}},
            "json_ext": {
                "enrolment_ranking": {
                    "*": {
                        "order_by": [{
                            "field": "json_ext__score",
                            "cast": "int",
                            "direction": "asc",
                            "nulls": "last",
                        }],
                        "tie_breaker": "id",
                        "limit": {
                            "percentage": 50,
                            "respect_max_beneficiaries": True,
                        },
                    }
                }
            },
        })
        ranked = create_individual(self.user.username, {"json_ext": {"score": "9"}})
        second = create_individual(self.user.username, {"json_ext": {"score": "10"}})
        create_individual(self.user.username, {"json_ext": {"score": "100"}})

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["pool_size"], 3)
        self.assertEqual(selection["will_enrol"], 2)
        self.assertEqual(selection["cap_applied"], 2)
        self.assertEqual(
            list(selection["individuals_not_assigned_to_selected_programme"].values_list("id", flat=True)),
            [ranked.id, second.id],
        )
        self.assertFalse(selection["individuals_not_assigned_to_selected_programme"].query.is_sliced)
        self.assertEqual(selection["selected_ids"], [ranked.id, second.id])

    def test_preview_caps_with_filterable_subquery_without_materialising_ids(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "max_beneficiaries": 10,
            "beneficiary_data_schema": {"properties": {}},
            "json_ext": {
                "enrolment_ranking": {
                    "*": {
                        "order_by": ["id"],
                        "limit": {"percentage": 50},
                    }
                }
            },
        })
        first = create_individual(self.user.username)
        second = create_individual(self.user.username)
        create_individual(self.user.username)

        selection = build_individual_enrollment_selection(
            [],
            str(benefit_plan.id),
            "ACTIVE",
            self.user,
            materialize_selected_ids=False,
        )
        preview = selection["individuals_not_assigned_to_selected_programme"]

        self.assertFalse(preview.query.is_sliced)
        self.assertIsNone(selection["selected_ids"])
        self.assertEqual(list(preview.values_list("id", flat=True)), [first.id, second.id])
        self.assertEqual(preview.filter(id=first.id).count(), 1)
        self.assertEqual(
            list(preview.order_by("-id").values_list("id", flat=True)),
            [second.id, first.id],
        )

    def test_remaining_status_capacity_limits_cumulative_enrollment(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "max_beneficiaries": 2,
            "json_ext": {
                "enrolment_ranking": {
                    "ACTIVE": {
                        "order_by": ["id"],
                        "limit": {"percentage": 100},
                    }
                }
            },
        })
        existing = create_individual(self.user.username)
        create_individual(self.user.username)
        create_individual(self.user.username)
        beneficiary = Beneficiary(
            individual=existing,
            benefit_plan=benefit_plan,
            status="ACTIVE",
            json_ext={},
        )
        beneficiary.save(username=self.user.username)

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["pool_size"], 2)
        self.assertEqual(selection["cap_applied"], 1)
        self.assertEqual(selection["will_enrol"], 1)

    def test_group_ranking_cap_and_nulls_last(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "GROUP",
            "json_ext": {
                "enrolment_ranking": {
                    "*": {
                        "order_by": [{
                            "field": "json_ext__score",
                            "cast": "int",
                            "direction": "asc",
                            "nulls": "last",
                        }],
                        "limit": {"percentage": 50},
                    }
                }
            },
        })
        first = create_group(self.user.username, {"json_ext": {"score": "9"}})
        second = create_group(self.user.username, {"json_ext": {"score": "10"}})
        create_group(self.user.username, {"json_ext": {}})

        selection = build_group_enrollment_selection(
            [], str(benefit_plan.id), "POTENTIAL", self.user
        )

        self.assertEqual(selection["will_enrol"], 2)
        self.assertEqual(
            list(selection["groups_not_assigned_to_selected_programme"].values_list("id", flat=True))[:2],
            [first.id, second.id],
        )

    def test_group_selection_only_excludes_active_assignment_for_requested_status(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "GROUP",
        })
        active = create_group(self.user.username)
        other_status = create_group(self.user.username)
        deleted = create_group(self.user.username)

        for group, assignment_status, is_deleted in (
            (active, "ACTIVE", False),
            (other_status, "POTENTIAL", False),
            (deleted, "ACTIVE", True),
        ):
            assignment = GroupBeneficiary(
                group=group,
                benefit_plan=benefit_plan,
                status=assignment_status,
                is_deleted=is_deleted,
                json_ext={},
            )
            assignment.save(username=self.user.username)

        selection = build_group_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["pool_size"], 2)
        self.assertEqual(
            list(selection["groups_not_assigned_to_selected_programme"]
                 .order_by("id").values_list("id", flat=True)),
            sorted([other_status.id, deleted.id]),
        )

    def test_individual_selection_only_excludes_active_assignment_for_requested_status(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        active = create_individual(self.user.username)
        other_status = create_individual(self.user.username)

        for individual, assignment_status in (
            (active, "ACTIVE"),
            (other_status, "POTENTIAL"),
        ):
            assignment = Beneficiary(
                individual=individual,
                benefit_plan=benefit_plan,
                status=assignment_status,
                json_ext={},
            )
            assignment.save(username=self.user.username)

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["pool_size"], 1)
        self.assertEqual(
            list(selection["individuals_not_assigned_to_selected_programme"]),
            [other_status],
        )

    def test_missing_ranking_preserves_uncapped_behavior(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        create_individual(self.user.username, {"json_ext": {"validation_status": "VERIFIED"}})

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertIsNone(selection["ranking"])
        self.assertIsNone(selection["cap_applied"])
        self.assertEqual(selection["pool_size"], selection["will_enrol"])

    def test_rejects_unknown_status(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")

        with self.assertRaisesMessage(ValidationError, "Unsupported"):
            build_individual_enrollment_queryset(
                custom_filters=[],
                benefit_plan_id=str(benefit_plan.id),
                status="UNKNOWN",
            )
