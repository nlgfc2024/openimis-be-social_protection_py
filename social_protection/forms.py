import json

from django import forms

from social_protection.models import BenefitPlan
from social_protection.phase_defaults import (
    advanced_criteria_validation_errors,
    enrolment_ranking_validation_errors,
)


ENROLLMENT_CONFIGURATION_KEYS = {"advanced_criteria", "enrolment_ranking"}


def _json_object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
    return value if isinstance(value, dict) else {}


class PrettyJSONField(forms.JSONField):
    def prepare_value(self, value):
        if isinstance(value, (dict, list)):
            return json.dumps(value, indent=2, ensure_ascii=False)
        return super().prepare_value(value)


class BenefitPlanEnrollmentConfigurationForm(forms.ModelForm):
    enrollment_configuration = PrettyJSONField(
        required=False,
        label="Enrollment configuration JSON",
        help_text=(
            "Paste an object containing advanced_criteria and/or "
            "enrolment_ranking. Other Benefit Plan json_ext values are preserved."
        ),
        widget=forms.Textarea(attrs={"rows": 32, "cols": 120}),
    )

    class Meta:
        model = BenefitPlan
        fields = ("max_beneficiaries",)
        labels = {"max_beneficiaries": "Maximum households"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        json_ext = _json_object(getattr(self.instance, "json_ext", None))
        self.fields["enrollment_configuration"].initial = {
            key: json_ext[key]
            for key in ENROLLMENT_CONFIGURATION_KEYS
            if key in json_ext
        }

    def clean_enrollment_configuration(self):
        configuration = self.cleaned_data.get("enrollment_configuration") or {}
        if not isinstance(configuration, dict):
            raise forms.ValidationError("Enrollment configuration must be a JSON object.")

        unexpected = set(configuration) - ENROLLMENT_CONFIGURATION_KEYS
        if unexpected:
            raise forms.ValidationError(
                "Unsupported enrollment configuration keys: "
                + ", ".join(sorted(unexpected))
            )

        schema = self.instance.beneficiary_data_schema or {}
        benefit_plan_type = self.instance.type
        errors = []
        if "advanced_criteria" in configuration:
            errors.extend(advanced_criteria_validation_errors(
                configuration["advanced_criteria"],
                schema,
                benefit_plan_type,
            ))
        if "enrolment_ranking" in configuration:
            errors.extend(enrolment_ranking_validation_errors(
                configuration["enrolment_ranking"],
                schema,
                benefit_plan_type,
            ))
        if errors:
            raise forms.ValidationError(errors)
        return configuration

    def save(self, commit=True):
        instance = super().save(commit=False)
        json_ext = _json_object(instance.json_ext)
        for key in ENROLLMENT_CONFIGURATION_KEYS:
            json_ext.pop(key, None)
        json_ext.update(self.cleaned_data.get("enrollment_configuration") or {})
        instance.json_ext = json_ext
        if commit:
            instance.save()
            self.save_m2m()
        return instance
