import json
from core.models import ModuleConfiguration
from django.test import TestCase
from social_protection.apps import SocialProtectionConfig


class ModuleConfigTest(TestCase):

    def test_config_reloading(self):
        # First set the social_protection config to be empty
        config = ModuleConfiguration.objects.filter(module='social_protection', layer='be').first()
        if config is None:
            config = ModuleConfiguration(module='social_protection', layer='be', config='{}')
        else:
            config.config = '{}'
        with self.captureOnCommitCallbacks(execute=True):
            config.save()

        self.assertTrue(SocialProtectionConfig.gql_check_benefit_plan_update)

        # Update config should trigger a reload
        updated_config = {
            "gql_check_benefit_plan_update": False,
        }
        # ModuleConfiguration._cfg is a cached property; use a fresh instance
        # when simulating the separate request that updates configuration.
        config = ModuleConfiguration.objects.get(pk=config.pk)
        config.config = json.dumps(updated_config)
        with self.captureOnCommitCallbacks(execute=True):
            config.save()

        self.assertFalse(SocialProtectionConfig.gql_check_benefit_plan_update)
