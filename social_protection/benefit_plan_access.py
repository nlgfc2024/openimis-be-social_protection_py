"""Shared Benefit Plan assignment-based access-control helpers."""

from django.core.exceptions import PermissionDenied

from social_protection.apps import SocialProtectionConfig
from social_protection.models import UserBenefitPlanAccess


def _is_superuser(user):
    return bool(user and getattr(user, "is_superuser", False))


def has_all_benefit_plans_access(user):
    """Return whether the user bypasses Benefit Plan assignments."""
    if _is_superuser(user):
        return True
    return bool(
        user
        and user.has_perms(
            SocialProtectionConfig.gql_benefit_plan_all_access_perms
        )
    )


def get_accessible_benefit_plan_ids(user):
    """Return assigned plan IDs, or ``None`` when plan access is unrestricted.

    An empty queryset means no access. ``None`` is deliberately reserved for the
    feature-disabled, all-plan-permission, and superuser cases.
    """
    if not SocialProtectionConfig.enable_benefit_plan_access_control:
        return None
    if has_all_benefit_plans_access(user):
        return None
    if not user or getattr(user, "is_anonymous", False):
        return UserBenefitPlanAccess.objects.none().values_list(
            "benefit_plan_id", flat=True
        )
    return UserBenefitPlanAccess.objects.filter(
        user=user,
        is_deleted=False,
        benefit_plan__is_deleted=False,
    ).values_list("benefit_plan_id", flat=True)


def can_access_benefit_plan(user, benefit_plan_id):
    if not SocialProtectionConfig.enable_benefit_plan_access_control:
        return True
    if has_all_benefit_plans_access(user):
        return True
    if not user or getattr(user, "is_anonymous", False):
        return False
    return UserBenefitPlanAccess.objects.filter(
        user=user,
        benefit_plan_id=benefit_plan_id,
        is_deleted=False,
        benefit_plan__is_deleted=False,
    ).exists()


def require_benefit_plan_access(user, benefit_plan_id):
    if not can_access_benefit_plan(user, benefit_plan_id):
        raise PermissionDenied("Benefit Plan access is required.")


def filter_queryset_by_benefit_plan(queryset, user, field_name="benefit_plan_id"):
    """Apply assignment scope to a queryset with a configurable plan field."""
    accessible_ids = get_accessible_benefit_plan_ids(user)
    if accessible_ids is None:
        return queryset
    lookup = f"{field_name}__in"
    return queryset.filter(**{lookup: accessible_ids})


def can_manage_benefit_plan_assignments(user, benefit_plan_id):
    """Policy foundation for Ticket 02 assignment mutations."""
    if _is_superuser(user):
        return True
    if not user or not user.has_perms(
        SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms
    ):
        return False
    if not SocialProtectionConfig.enable_benefit_plan_access_control:
        return has_all_benefit_plans_access(user)
    return can_access_benefit_plan(user, benefit_plan_id)


def require_benefit_plan_assignment_management(user, benefit_plan_id):
    if not can_manage_benefit_plan_assignments(user, benefit_plan_id):
        raise PermissionDenied(
            "Benefit Plan assignment-management permission and plan access are required."
        )
