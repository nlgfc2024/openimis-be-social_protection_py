import json
import logging
import types
from io import BytesIO

import pandas as pd
from django.core.files.base import ContentFile

from core.custom_filters import CustomFilterWizardStorage
from core.models import ExportableQueryModel
from core.gql.export_mixin import ExportableQueryMixin

logger = logging.getLogger(__file__)

ENROLMENT_EXPORT_COLUMNS = (
    "district_name", "traditional_authority_name", "catchment_name",
    "group_village_head_name", "village_name", "Phase_Name", "project_code",
    "project_name", "target_HHs", "enrolled_HHs", "enrolment_date", "form_number",
    "full_name", "national_id", "date_of_birth", "relationship", "enrolment_type",
)


class ExportableSocialProtectionQueryMixin(ExportableQueryMixin):

    @staticmethod
    def _location_columns(location):
        """Return Malawi location names regardless of the depth of the stored location."""
        locations = {}
        current = location
        while current:
            locations[current.type] = current.name
            current = getattr(current, "parent", None)
        return (
            locations.get("R", ""), locations.get("D", ""),
            locations.get("W", ""), locations.get("V", ""),
        )

    @staticmethod
    def _enrolment_workbook(rows):
        content = BytesIO()
        with pd.ExcelWriter(content, engine="openpyxl") as writer:
            pd.DataFrame(rows, columns=ENROLMENT_EXPORT_COLUMNS).to_excel(
                writer, sheet_name="Enrolments", index=False,
            )
        return content.getvalue()

    @classmethod
    def _create_enrolment_xlsx(cls, queryset, field_name, user):
        """Create the fixed enrolment workbook used by the social-protection UI."""
        from django.db.models import Count, Prefetch
        from household_validation.identity import get_household_form_number
        from individual.models import GroupIndividual, Individual
        from location.models import MicroCatchmentGVH, MicroCatchmentTA
        from project_social_protection.models import (
            BeneficiaryProjectEnrollment, GroupBeneficiaryProjectEnrollment,
        )

        is_group_export = field_name == "group_beneficiary"
        relation = "group" if is_group_export else "individual"
        enrollment_model = (
            GroupBeneficiaryProjectEnrollment if is_group_export
            else BeneficiaryProjectEnrollment
        )
        enrolment_prefetch = Prefetch(
            "project_enrollments",
            queryset=enrollment_model.objects.filter(is_deleted=False).select_related(
                "project__activity", "project__benefit_plan",
            ),
        )
        queryset = queryset.select_related(
            f"{relation}__location__parent__parent__parent"
        ).prefetch_related(enrolment_prefetch)
        records = list(queryset)

        record_locations = {}
        record_location_ids = {"D": set(), "W": set()}
        group_ids = set()
        for record in records:
            location = getattr(record, relation).location
            if is_group_export:
                group_ids.add(record.group_id)
            current = location
            while current:
                if current.type in record_location_ids:
                    record_location_ids[current.type].add(current.id)
                current = getattr(current, "parent", None)
            record_locations[record.id] = location

        members_by_group = {}
        archived_members_by_group = {}
        if group_ids:
            for member in GroupIndividual.objects.filter(
                group_id__in=group_ids,
                is_deleted=False,
            ).select_related("individual"):
                members_by_group.setdefault(member.group_id, []).append(member)
            # Legacy imports can mark links deleted after the household was
            # enrolled. They still hold the member role needed for reporting.
            for member in GroupIndividual.objects.filter(
                group_id__in=group_ids,
                is_deleted=True,
            ).select_related("individual").order_by("group_id", "individual_id", "-date_updated"):
                archived_members_by_group.setdefault(member.group_id, {}) \
                    .setdefault(member.individual_id, member)

        # Older household imports may have lost their active GroupIndividual
        # links while retaining member IDs in Group.json_ext. Keep those reports
        # useful by resolving the saved IDs back to Individual records.
        legacy_member_ids_by_group = {}
        legacy_member_ids = set()
        for record in records:
            if not is_group_export:
                continue
            group_json = record.group.json_ext or {}
            member_ids = list((group_json.get("members") or {}).keys())
            legacy_member_ids_by_group[record.group_id] = member_ids
            legacy_member_ids.update(member_ids)
        legacy_individuals = {
            str(individual_id): individual
            for individual_id, individual in Individual.objects.in_bulk(legacy_member_ids).items()
        }

        micro_catchments_by_location = {}
        for link in MicroCatchmentGVH.objects.filter(
            location_id__in=record_location_ids["W"],
            validity_to__isnull=True,
            micro_catchment__validity_to__isnull=True,
        ).select_related("micro_catchment"):
            micro_catchments_by_location.setdefault(link.location_id, []).append(link.micro_catchment.name)
        for link in MicroCatchmentTA.objects.filter(
            location_id__in=record_location_ids["D"],
            validity_to__isnull=True,
            micro_catchment__validity_to__isnull=True,
        ).select_related("micro_catchment"):
            micro_catchments_by_location.setdefault(link.location_id, []).append(link.micro_catchment.name)

        enrolled_by_project = {
            row["project_id"]: row["count"]
            for row in enrollment_model.objects.filter(is_deleted=False)
            .values("project_id").annotate(count=Count("id"))
        }

        rows = []
        for record in records:
            district, ta, gvh, village = cls._location_columns(record_locations[record.id])
            location_ids = {}
            current = record_locations[record.id]
            while current:
                location_ids[current.type] = current.id
                current = getattr(current, "parent", None)
            micro_catchments = (
                micro_catchments_by_location.get(location_ids.get("W"), [])
                or micro_catchments_by_location.get(location_ids.get("D"), [])
            )
            identity = getattr(record, relation)
            if is_group_export:
                members = [
                    (member.individual, member.role)
                    for member in members_by_group.get(identity.id, [])
                ]
                if not members:
                    members = [
                        (member.individual, member.role)
                        for member in archived_members_by_group.get(identity.id, {}).values()
                    ]
                if not members:
                    head_id = str((identity.json_ext or {}).get("head_id") or "")
                    members = [
                        (
                            legacy_individuals.get(member_id),
                            "HEAD" if str(member_id) == head_id else "",
                        )
                        for member_id in legacy_member_ids_by_group.get(identity.id, [])
                    ]
            else:
                members = [(identity, "")]
            # Keep one row for an enrolled household without members, but otherwise
            # export every attached member with the same household/project details.
            members = members or [(None, "")]
            enrolments = list(record.project_enrollments.all()) or [None]

            for enrolment in enrolments:
                project = enrolment.project if enrolment else None
                project_json = getattr(project, "json_ext", {}) or {}
                for person, relationship in members:
                    record_json = record.json_ext or {}
                    person_json = getattr(person, "json_ext", {}) or {}
                    group_json = getattr(identity, "json_ext", {}) or {}
                    rows.append({
                        "district_name": district,
                        "traditional_authority_name": ta,
                        "catchment_name": "; ".join(micro_catchments),
                        "group_village_head_name": gvh,
                        "village_name": village,
                        "Phase_Name": project.benefit_plan.name if project else "",
                        "project_code": (
                            project.code or project_json.get("project_code")
                            or project_json.get("code", "")
                        ) if project else "",
                        "project_name": project.name if project else "",
                        "target_HHs": project.target_beneficiaries if project else "",
                        "enrolled_HHs": enrolled_by_project.get(project.id, "") if project else "",
                        "enrolment_date": enrolment.date_created.date().isoformat() if enrolment and enrolment.date_created else "",
                        "form_number": (
                            get_household_form_number(identity, person) or identity.code
                            if is_group_export else record_json.get(
                                "form_number", person_json.get("form_number", "")
                            )
                        ),
                        "full_name": f"{person.first_name} {person.last_name}".strip() if person else "",
                        "national_id": person_json.get(
                            "national_id", record_json.get("national_id", group_json.get("national_id", ""))
                        ),
                        "date_of_birth": person.dob.isoformat() if person and person.dob else "",
                        "relationship": relationship,
                        "enrolment_type": "Household" if is_group_export else "Individual",
                    })

        from core.models import ExportableQueryModel
        import uuid
        filename = f"{uuid.uuid4()}.xlsx"
        export = ExportableQueryModel(
            name=filename,
            model=queryset.model.__name__,
            content=ContentFile(cls._enrolment_workbook(rows), filename),
            user=user,
            sql_query=str(queryset.query),
            file_format="xlsx",
        )
        export.save()
        return export

    @classmethod
    def create_export_function(cls, field_name):
        new_function_name = f"resolve_{field_name}_export"
        default_resolve = getattr(cls, F"resolve_{field_name}", None)

        if not default_resolve:
            raise AttributeError(
                f"Query {cls} doesn't provide resolve function for {field_name}. "
                f"CSV export cannot be created")

        def exporter(cls, self, info, **kwargs):
            custom_filters = kwargs.pop("customFilters", None)
            export_fields = [cls._adjust_notation(f) for f in kwargs.pop('fields')]
            fields_mapping = json.loads(kwargs.pop('fields_columns'))
            file_format = kwargs.pop("file_format", "csv")

            source_field = getattr(cls, field_name)
            filter_kwargs = {k: v for k, v in kwargs.items() if k in source_field.filtering_args}

            qs = default_resolve(None, info, **kwargs)
            qs = qs.filter(**filter_kwargs)
            qs = cls.__append_custom_filters(custom_filters, qs, fields_mapping)
            if file_format == "xlsx":
                return cls._create_enrolment_xlsx(qs, field_name, info.context.user).name
            export_file = ExportableQueryModel\
                .create_csv_export(qs, export_fields, info.context.user, column_names=fields_mapping,
                                   patches=cls.get_patches_for_field(field_name))

            return export_file.name

        setattr(cls, new_function_name, types.MethodType(exporter, cls))

    @classmethod
    def __append_custom_filters(cls, custom_filters, queryset, fields_mapping):
        if custom_filters:
            module_name = cls.get_module_name()
            object_type = cls.get_object_type()
            related_field = cls.get_related_field()
            if "group__id" in fields_mapping:
                queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                    "individual",
                    "GroupIndividual",
                    custom_filters,
                    queryset,
                    relation="group"
                )
            else:
                queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                    module_name,
                    object_type,
                    custom_filters,
                    queryset,
                    relation=related_field
                )
        return queryset
