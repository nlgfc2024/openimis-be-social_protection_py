from django.contrib import admin

from social_protection.forms import BenefitPlanEnrollmentConfigurationForm
from social_protection.models import BenefitPlan


@admin.register(BenefitPlan)
class BenefitPlanAdmin(admin.ModelAdmin):
    form = BenefitPlanEnrollmentConfigurationForm
    list_display = (
        "code",
        "name",
        "type",
        "max_beneficiaries",
        "date_valid_from",
        "date_valid_to",
    )
    list_filter = ("type", "is_deleted")
    search_fields = ("code", "name")
    ordering = ("code",)
    fields = (
        "code",
        "name",
        "type",
        "beneficiary_data_schema",
        "max_beneficiaries",
        "enrollment_configuration",
    )
    readonly_fields = (
        "code",
        "name",
        "type",
        "beneficiary_data_schema",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_deleted=False)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.save(user=request.user)
