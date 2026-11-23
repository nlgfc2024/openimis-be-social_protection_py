"""Shared Benefit Plan assignment-based access-control helpers."""

from django.core.exceptions import PermissionDenied, ValidationError

from social_protection.apps import SocialProtectionConfig


_BENEFIT_PLAN_SCOPE_MARKER = "_benefit_plan_access_scope_applied"


def benefit_plan_access_control_enabled():
    """Return the shared feature state without loading authorization models."""
    return bool(SocialProtectionConfig.enable_benefit_plan_access_control)


def _user_benefit_plan_access_model():
    from social_protection.models import UserBenefitPlanAccess

    return UserBenefitPlanAccess


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


def has_benefit_plan_assignment_management_permission(user):
    """Return whether ``user`` has the general assignment-management right."""
    if _is_superuser(user):
        return True
    return bool(
        user
        and user.has_perms(
            SocialProtectionConfig.gql_benefit_plan_assignment_manage_perms
        )
    )


def require_benefit_plan_assignment_management_permission(user):
    """Enforce the general right before looking up a requested resource."""
    if not has_benefit_plan_assignment_management_permission(user):
        raise PermissionDenied(
            "Benefit Plan assignment-management permission is required."
        )


def get_accessible_benefit_plan_ids(user):
    """Return assigned plan IDs, or ``None`` when plan access is unrestricted.

    An empty queryset means no access. ``None`` is deliberately reserved for the
    feature-disabled, all-plan-permission, and superuser cases.
    """
    if not benefit_plan_access_control_enabled():
        return None
    if has_all_benefit_plans_access(user):
        return None
    UserBenefitPlanAccess = _user_benefit_plan_access_model()
    if not user or getattr(user, "is_anonymous", False):
        return UserBenefitPlanAccess.objects.none().values_list(
            "benefit_plan_id", flat=True
        )
    return UserBenefitPlanAccess.objects.filter(
        user=user,
        is_deleted=False,
        benefit_plan__is_deleted=False,
    ).values_list("benefit_plan_id", flat=True)


def can_access_benefit_plan(user, benefit_plan_id, include_deleted=False):
    """Return access only while both assignment and Benefit Plan are active.

    Assignments are deliberately retained when a Benefit Plan is soft-deleted,
    but cannot grant scope until an authorised restore reactivates that plan.
    """
    if not benefit_plan_access_control_enabled():
        return True
    if has_all_benefit_plans_access(user):
        return True
    if not user or getattr(user, "is_anonymous", False):
        return False
    UserBenefitPlanAccess = _user_benefit_plan_access_model()
    filters = {
        "user": user,
        "benefit_plan_id": benefit_plan_id,
        "is_deleted": False,
    }
    if not include_deleted:
        filters["benefit_plan__is_deleted"] = False
    return UserBenefitPlanAccess.objects.filter(
        **filters,
    ).exists()


def require_benefit_plan_access(
    user,
    benefit_plan_id,
    include_deleted=False,
):
    if not can_access_benefit_plan(
        user,
        benefit_plan_id,
        include_deleted=include_deleted,
    ):
        raise PermissionDenied("Benefit Plan access is required.")


def require_benefit_plan_upload_access(
    user,
    upload_id,
    benefit_plan_id=None,
):
    """Protect an upload and bind it to the requested Benefit Plan.

    Upload ownership was not enforced before the feature flag existed, so the
    disabled path deliberately remains a no-op for backwards compatibility.
    When enabled, every plan associated with the upload must be accessible and
    an explicitly supplied plan must own the upload.
    """
    if not benefit_plan_access_control_enabled():
        return
    from individual.models import IndividualDataSourceUpload
    from social_protection.models import BenefitPlanDataUploadRecords

    if not upload_id:
        raise ValidationError("Upload ID is required.")
    if not IndividualDataSourceUpload.objects.filter(pk=upload_id).exists():
        raise ValidationError("Upload does not exist.")

    plan_ids = list(
        BenefitPlanDataUploadRecords.objects.filter(
            data_upload_id=upload_id,
            is_deleted=False,
        ).values_list("benefit_plan_id", flat=True).distinct()
    )
    if has_all_benefit_plans_access(user):
        return
    requested_plan_mismatch = benefit_plan_id is not None and str(
        benefit_plan_id
    ) not in {str(plan_id) for plan_id in plan_ids}
    if not plan_ids or requested_plan_mismatch:
        raise PermissionDenied("Benefit Plan upload access is required.")
    for plan_id in plan_ids:
        require_benefit_plan_access(user, plan_id)


def filter_queryset_by_benefit_plan(queryset, user, field_name="benefit_plan_id"):
    """Apply assignment scope to a queryset with a configurable plan field."""
    accessible_ids = get_accessible_benefit_plan_ids(user)
    if accessible_ids is None:
        return queryset
    if getattr(queryset.query, _BENEFIT_PLAN_SCOPE_MARKER, False):
        return queryset
    lookup = f"{field_name}__in"
    scoped_queryset = queryset.filter(**{lookup: accessible_ids})
    # This marker means that this Query has already been scoped. Django's
    # Query.clone() preserves custom attributes, so subsequent filters and
    # Graphene's connection pass retain it. A marked queryset must therefore
    # never be reused for a different user or Benefit Plan field without
    # re-evaluating authorisation: the early return above would otherwise skip
    # filtering required for that new user or field.
    setattr(scoped_queryset.query, _BENEFIT_PLAN_SCOPE_MARKER, True)
    return scoped_queryset


def can_manage_benefit_plan_assignments(user, benefit_plan_id):
    """Return whether ``user`` may manage assignments for one Benefit Plan.

    When access control is disabled, Benefit Plan data is deliberately
    unrestricted, but assignment administration remains a global privileged
    operation.  Permission 160006 supplies that explicit global scope and
    160007 supplies the assignment-management capability; both are therefore
    required.  Superusers retain their existing bypass in every mode.
    """
    if _is_superuser(user):
        return True
    if not has_benefit_plan_assignment_management_permission(user):
        return False
    if not benefit_plan_access_control_enabled():
        return has_all_benefit_plans_access(user)
    return can_access_benefit_plan(user, benefit_plan_id)


def filter_benefit_plan_assignments_for_management(queryset, user):
    """Restrict assignments to Benefit Plans ``user`` may manage."""
    if not has_benefit_plan_assignment_management_permission(user):
        return queryset.none()
    if _is_superuser(user):
        return queryset
    if not benefit_plan_access_control_enabled():
        if has_all_benefit_plans_access(user):
            return queryset
        return queryset.none()

    accessible_ids = get_accessible_benefit_plan_ids(user)
    if accessible_ids is None:
        return queryset
    return queryset.filter(benefit_plan_id__in=accessible_ids)


def require_benefit_plan_assignment_management(user, benefit_plan_id):
    if not can_manage_benefit_plan_assignments(user, benefit_plan_id):
        raise PermissionDenied(
            "Benefit Plan assignment-management permission and plan access are required."
        )
