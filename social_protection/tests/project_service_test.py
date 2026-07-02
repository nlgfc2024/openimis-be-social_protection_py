from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from core.test_helpers import LogInHelper
from location.test_helpers import create_test_village
from social_protection.models import Project, ProjectSector, ProjectStatus
from social_protection.services import ProjectService


@override_settings(ROW_SECURITY=False)
class ProjectServiceTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = ProjectService(cls.user)

    def setUp(self):
        self.village = create_test_village({"code": "SPPROJ"})
        self.micro_catchment = self.village.parent
        self.district = self.micro_catchment.parent
        self.sector = ProjectSector.objects.create(name="Roads")

    def _payload(self, **overrides):
        payload = {
            "district": self.district,
            "micro_catchment": self.micro_catchment,
            "sector": self.sector,
            "known_place": "Bridge",
            "target_households": 120,
            "working_days": 10,
        }
        payload.update(overrides)
        return payload

    def test_create_sets_preparation_status_and_generated_name(self):
        result = self.service.create(self._payload(status=ProjectStatus.IN_PROGRESS))

        self.assertTrue(result["success"], result.get("detail"))
        project = Project.objects.get(uuid=result["data"]["uuid"])
        self.assertEqual(project.status, ProjectStatus.PREPARATION)
        self.assertEqual(project.name, f"{self.micro_catchment.name}-Roads - Bridge")

    def test_create_rejects_target_households_below_one(self):
        with self.assertRaises(ValidationError):
            self.service.create(self._payload(target_households=0))

    def test_create_rejects_target_households_above_two_hundred(self):
        with self.assertRaises(ValidationError):
            self.service.create(self._payload(target_households=201))

    def test_create_rejects_invalid_micro_catchment_district_combination(self):
        other_village = create_test_village({"code": "SPOTHER"})
        other_district = other_village.parent.parent

        with self.assertRaises(ValidationError):
            self.service.create(self._payload(district=other_district))

    def test_status_transition_is_forward_only_one_step(self):
        result = self.service.create(self._payload())
        project = Project.objects.get(uuid=result["data"]["uuid"])

        with self.assertRaises(ValidationError):
            self.service.mark_project_in_progress(project)

        self.service.mark_project_initiated(project)
        project.refresh_from_db()
        self.assertEqual(project.status, ProjectStatus.INITIATED)

        with self.assertRaises(ValidationError):
            self.service.mark_project_initiated(project)

        self.service.mark_project_in_progress(project)
        project.refresh_from_db()
        self.assertEqual(project.status, ProjectStatus.IN_PROGRESS)

        with self.assertRaises(ValidationError):
            self.service.mark_project_initiated(project)
