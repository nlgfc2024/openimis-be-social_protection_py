import pandas as pd
from django.test import SimpleTestCase, TestCase
from types import SimpleNamespace

from core.test_helpers import LogInHelper
from social_protection.models import Beneficiary, GroupBeneficiary
from social_protection.services import BeneficiaryImportService, BeneficiaryService, GroupBeneficiaryService
from social_protection.tests.test_helpers import create_benefit_plan, create_group, create_individual


class BeneficiaryDataSourceTest(TestCase):
    """A benefit plan only takes beneficiaries from its own data source."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.default_plan = create_benefit_plan(cls.user.username, {"code": "DSUBR", "type": "INDIVIDUAL"})
        cls.pwp_plan = create_benefit_plan(
            cls.user.username, {"code": "DSPWP", "type": "INDIVIDUAL", "json_ext": {"data_source": "pwp"}},
        )
        cls.pwp_group_plan = create_benefit_plan(
            cls.user.username, {"code": "DSGRP", "type": "GROUP", "json_ext": {"data_source": "pwp"}},
        )

    def _individual(self, source_type=None):
        json_ext = {"source_type": source_type} if source_type else {}
        return create_individual(self.user.username, {"json_ext": json_ext})

    def _add(self, plan, individual):
        return BeneficiaryService(self.user).create({
            "individual_id": individual.id, "benefit_plan_id": plan.id, "status": "POTENTIAL",
        })

    def test_plan_accepts_people_from_its_source(self):
        result = self._add(self.pwp_plan, self._individual("pwp"))

        self.assertTrue(result.get("success"), result)

    def test_plan_rejects_people_from_another_source(self):
        result = self._add(self.pwp_plan, self._individual())

        self.assertFalse(result.get("success"))
        self.assertIn("only takes data from 'pwp'", result["detail"])
        self.assertFalse(Beneficiary.objects.filter(benefit_plan=self.pwp_plan).exists())

    def test_default_plan_accepts_untagged_and_rejects_other_sources(self):
        self.assertTrue(self._add(self.default_plan, self._individual()).get("success"))
        self.assertFalse(self._add(self.default_plan, self._individual("pwp")).get("success"))

    def test_update_cannot_move_a_beneficiary_to_another_sources_plan(self):
        result = self._add(self.default_plan, self._individual())
        beneficiary_id = result["data"]["id"]

        moved = BeneficiaryService(self.user).update({"id": beneficiary_id, "benefit_plan_id": self.pwp_plan.id})

        self.assertFalse(moved.get("success"))

    def test_group_plan_checks_the_household_source_by_its_code(self):
        service = GroupBeneficiaryService(self.user)
        # No json_ext tag, as for a household without a head; the code carries the source
        pwp_group = create_group(self.user.username, {"code": "pwp:94104570", "json_ext": {}})
        msr_group = create_group(self.user.username, {"code": "94104571", "json_ext": {"source_type": "pwp"}})

        accepted = service.create({"group_id": pwp_group.id, "benefit_plan_id": self.pwp_group_plan.id, "status": "POTENTIAL"})
        rejected = service.create({"group_id": msr_group.id, "benefit_plan_id": self.pwp_group_plan.id, "status": "POTENTIAL"})

        self.assertTrue(accepted.get("success"), accepted)
        self.assertFalse(rejected.get("success"))
        self.assertEqual(GroupBeneficiary.objects.filter(benefit_plan=self.pwp_group_plan).count(), 1)


class BeneficiaryImportDataSourceTest(SimpleTestCase):
    """Beneficiary file imports tag the people they create with the plan's data source."""

    apply = staticmethod(BeneficiaryImportService._apply_benefit_plan_data_source)

    def test_default_plan_tags_rows_and_keeps_household_codes(self):
        df = pd.DataFrame({"first_name": ["A"], "group_code": ["30874398"]})

        result = self.apply(df, SimpleNamespace(json_ext={}))

        self.assertEqual(result["source_type"].tolist(), ["ubr"])
        self.assertEqual(result["group_code"].tolist(), ["30874398"])

    def test_other_source_plan_tags_rows_and_prefixes_household_codes(self):
        df = pd.DataFrame({"first_name": ["A", "B"], "household_id": ["30874398", None]})

        result = self.apply(df, SimpleNamespace(json_ext={"data_source": "pwp"}), "household_id")

        self.assertEqual(result["source_type"].tolist(), ["pwp", "pwp"])
        self.assertEqual(result["household_id"].tolist()[0], "pwp:30874398")
        self.assertTrue(pd.isna(result["household_id"].tolist()[1]))

    def test_overrides_a_source_type_column_in_the_file(self):
        df = pd.DataFrame({"first_name": ["A"], "source_type": ["ubr"]})

        result = self.apply(df, SimpleNamespace(json_ext={"data_source": "pwp"}))

        self.assertEqual(result["source_type"].tolist(), ["pwp"])
