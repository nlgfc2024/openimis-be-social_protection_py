import json
import logging
import types
from io import BytesIO
from itertools import islice

from django.core.exceptions import ValidationError
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
    "account_number", "account_name", "bank", "kyc_status",
)
KYC_EXPORT_COLUMNS = (
    "district_name", "traditional_authority_name", "catchment_name",
    "group_village_head_name", "village_name", "phase_name", "form_number", "full_name",
    "national_id", "date_of_birth", "Gender", "account_number", "account_name",
    "bank",
)
ENROLMENT_EXPORT_BATCH_SIZE = 500


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
    def _enrolment_workbook(rows, columns=ENROLMENT_EXPORT_COLUMNS):
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell

        content = BytesIO()
        workbook = Workbook(write_only=True)
        worksheet = workbook.create_sheet("Enrolments")
        worksheet.append(columns)
        for row in rows:
            cells = []
            for column in columns:
                cell = WriteOnlyCell(worksheet, value=row.get(column, ""))
                # Account numbers are identifiers, not amounts. Formatting the
                # exported cells as text preserves leading zeroes and ensures
                # Excel does not turn newly entered values into numeric cells.
                if column == "account_number":
                    cell.number_format = "@"
                cells.append(cell)
            worksheet.append(cells)
        workbook.save(content)
        return content.getvalue()

    @classmethod
    def _create_enrolment_xlsx(
        cls, queryset, field_name, user, primary_recipient_only=False,
        phase_beneficiary_summary_only=False, benefit_plan_id=None,
    ):
        """Create the fixed enrolment workbook used by the social-protection UI."""
        from django.db.models import Count, Prefetch
        from household_validation.identity import get_household_form_number
        from individual.models import Group, GroupIndividual, Individual
        from location.models import MicroCatchmentGVH, MicroCatchmentTA
        from project_social_protection.models import (
            BeneficiaryProjectEnrollment, GroupBeneficiaryProjectEnrollment, Project,
        )
        from social_protection.models import BenefitPlan, BeneficiaryStatus, GroupBeneficiary

        phase_name = ""
        if benefit_plan_id:
            phase = BenefitPlan.objects.filter(
                id=benefit_plan_id,
                is_deleted=False,
            ).values("code", "name").first()
            if phase:
                phase_name = " ".join(
                    value for value in (phase["code"], phase["name"]) if value
                )

        is_group_export = (
            field_name == "group_beneficiary" or queryset.model is Group
        )
        relation = "group" if is_group_export else "individual"
        has_beneficiary_relation = any(
            field.name == relation for field in queryset.model._meta.get_fields()
        )
        enrollment_model = (
            GroupBeneficiaryProjectEnrollment if is_group_export
            else BeneficiaryProjectEnrollment
        )
        enrollment_beneficiary_field = "group_beneficiary" if is_group_export else "beneficiary"
        enrolment_prefetch = Prefetch(
            "project_enrollments",
            queryset=enrollment_model.objects.filter(is_deleted=False).select_related(
                "project__activity", "project__benefit_plan",
            ),
        )
        prefetches = [enrolment_prefetch] if has_beneficiary_relation else []
        if is_group_export:
            prefetches.append(Prefetch(
                f"{'group__' if has_beneficiary_relation else ''}groupindividuals",
                queryset=GroupIndividual.objects.select_related("individual").order_by(
                    "group_id", "individual_id", "-date_updated"
                ),
                to_attr="export_group_members",
            ))
        export_queryset = queryset
        enrolled_by_project = {
            row["project_id"]: row["count"]
            for row in enrollment_model.objects.filter(
                is_deleted=False,
                **{f"{enrollment_beneficiary_field}_id__in": export_queryset.order_by().values("pk")},
            ).values("project_id").annotate(count=Count("id"))
        } if has_beneficiary_relation else {}
        location_relation = (
            f"{relation}__location__parent__parent__parent"
            if has_beneficiary_relation else "location__parent__parent__parent"
        )
        queryset = queryset.select_related(location_relation)
        if prefetches:
            queryset = queryset.prefetch_related(*prefetches)

        def build_rows(records):
            record_locations = {}
            record_location_ids = {"D": set(), "W": set()}
            for record in records:
                identity = getattr(record, relation, record)
                location = identity.location
                current = location
                while current:
                    if current.type in record_location_ids:
                        record_location_ids[current.type].add(current.id)
                    current = getattr(current, "parent", None)
                record_locations[record.id] = location

            active_phase_group_ids = set()
            if primary_recipient_only and is_group_export:
                group_ids = [
                    getattr(record, relation, record).id
                    for record in records
                ]
                # KYC is a phase-level export. A household is eligible when it is an
                # ACTIVE household beneficiary of the selected phase; requiring a
                # separate project enrolment incorrectly excludes valid households
                # that have not been assigned to an individual project.
                active_phase_group_ids = set(
                    GroupBeneficiary.objects.filter(
                        is_deleted=False,
                        status=BeneficiaryStatus.ACTIVE,
                        group_id__in=group_ids,
                        **({"benefit_plan_id": benefit_plan_id} if benefit_plan_id else {}),
                    ).values_list("group_id", flat=True)
                )

            members_by_group = {}
            archived_members_by_group = {}
            if is_group_export:
                for record in records:
                    identity = getattr(record, relation, record)
                    for member in identity.export_group_members:
                        if not member.is_deleted:
                            members_by_group.setdefault(member.group_id, []).append(member)
                        else:
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
                identity = getattr(record, relation, record)
                group_json = identity.json_ext or {}
                member_ids = list((group_json.get("members") or {}).keys())
                legacy_member_ids_by_group[identity.id] = member_ids
                legacy_member_ids.update(member_ids)
            legacy_individuals = {
                str(individual_id): individual
                for individual_id, individual in Individual.objects.in_bulk(legacy_member_ids).items()
            }

            micro_catchments_by_location = {}
            micro_catchment_ids_by_location = {}
            for link in MicroCatchmentGVH.objects.filter(
                location_id__in=record_location_ids["W"],
                validity_to__isnull=True,
                micro_catchment__validity_to__isnull=True,
            ).select_related("micro_catchment"):
                micro_catchments_by_location.setdefault(link.location_id, []).append(link.micro_catchment.name)
                micro_catchment_ids_by_location.setdefault(link.location_id, set()).add(
                    link.micro_catchment_id,
                )
            for link in MicroCatchmentTA.objects.filter(
                location_id__in=record_location_ids["D"],
                validity_to__isnull=True,
                micro_catchment__validity_to__isnull=True,
            ).select_related("micro_catchment"):
                micro_catchments_by_location.setdefault(link.location_id, []).append(link.micro_catchment.name)
                micro_catchment_ids_by_location.setdefault(link.location_id, set()).add(
                    link.micro_catchment_id,
                )

            projects_by_micro_catchment = {}
            micro_catchment_ids = set().union(*micro_catchment_ids_by_location.values()) \
                if micro_catchment_ids_by_location else set()
            for project in Project.objects.filter(
                micro_catchment_id__in=micro_catchment_ids,
                is_deleted=False,
            ).select_related("activity", "benefit_plan"):
                projects_by_micro_catchment.setdefault(project.micro_catchment_id, []).append(project)

            for record in records:
                identity = getattr(record, relation, record)
                if primary_recipient_only and is_group_export:
                    if identity.id not in active_phase_group_ids:
                        continue
                    if (identity.json_ext or {}).get("validation_status") != "VERIFIED":
                        continue
                district, ta, gvh, village = cls._location_columns(record_locations[record.id])
                location_ids = {}
                current = record_locations[record.id]
                while current:
                    location_ids[current.type] = current.id
                    current = getattr(current, "parent", None)
                micro_catchments = sorted(set(
                    micro_catchments_by_location.get(location_ids.get("W"), [])
                    + micro_catchments_by_location.get(location_ids.get("D"), [])
                ))
                project_candidates = {}
                for micro_catchment_id in (
                    micro_catchment_ids_by_location.get(location_ids.get("W"), set())
                    | micro_catchment_ids_by_location.get(location_ids.get("D"), set())
                ):
                    project_candidates.update({
                        project.id: project
                        for project in projects_by_micro_catchment.get(micro_catchment_id, [])
                    })
                if is_group_export:
                    members = [
                        (member.individual, member.role)
                        for member in members_by_group.get(identity.id, [])
                        if (
                            not (primary_recipient_only or phase_beneficiary_summary_only)
                            or member.recipient_type == GroupIndividual.RecipientType.PRIMARY
                        )
                    ]
                    if not members:
                        members = [
                            (member.individual, member.role)
                            for member in archived_members_by_group.get(identity.id, {}).values()
                            if (
                                not (primary_recipient_only or phase_beneficiary_summary_only)
                                or member.recipient_type == GroupIndividual.RecipientType.PRIMARY
                            )
                        ]
                    if not members:
                        head_id = str((identity.json_ext or {}).get("head_id") or "")
                        legacy_members = [
                            (
                                legacy_individuals.get(member_id),
                                "HEAD" if str(member_id) == head_id else "",
                            )
                            for member_id in legacy_member_ids_by_group.get(identity.id, [])
                        ]
                        if primary_recipient_only or phase_beneficiary_summary_only:
                            head_member = next(
                                (member for member in legacy_members if member[1] == "HEAD"),
                                None,
                            )
                            members = [head_member] if head_member else legacy_members[:1]
                        else:
                            members = legacy_members
                else:
                    members = [(identity, "")]
                # Keep one row for an enrolled household without members, but otherwise
                # export every attached member with the same household/project details.
                members = members or [(None, "")]
                enrolments = (
                    list(record.project_enrollments.all())
                    if has_beneficiary_relation else []
                )
                if primary_recipient_only:
                    project_rows = [(None, None)]
                elif phase_beneficiary_summary_only:
                    # The Phase beneficiary list represents households, not
                    # members or project assignments. A household without a
                    # project enrolment still gets exactly one row.
                    project_rows = (
                        [(enrolments[0], enrolments[0].project)]
                        if enrolments else [(None, None)]
                    )
                else:
                    project_rows = (
                        [(enrolment, enrolment.project) for enrolment in enrolments]
                        or [(None, project) for project in project_candidates.values()]
                        or [(None, None)]
                    )

                for enrolment, project in project_rows:
                    project_json = getattr(project, "json_ext", {}) or {}
                    for person, relationship in members:
                        record_json = record.json_ext or {}
                        person_json = getattr(person, "json_ext", {}) or {}
                        group_json = getattr(identity, "json_ext", {}) or {}
                        yield {
                            "district_name": district,
                            "traditional_authority_name": ta,
                            "catchment_name": "; ".join(micro_catchments),
                            "group_village_head_name": gvh,
                            "village_name": village,
                            "phase_name": phase_name,
                            "Phase_Name": project.benefit_plan.name if project else "",
                            "project_code": (
                                project.code or project_json.get("project_code")
                                or project_json.get("code", "")
                            ) if project else "",
                            "project_name": project.name if project else "",
                            "target_HHs": project.target_beneficiaries if project else "",
                            "enrolled_HHs": enrolled_by_project.get(project.id, "") if project else "",
                            "enrolment_date": (
                                enrolment.date_created.date().isoformat()
                                if enrolment and enrolment.date_created else ""
                            ),
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
                            "Gender": person_json.get("gender", ""),
                            "relationship": relationship,
                            "enrolment_type": "Household" if is_group_export else "Individual",
                            "account_number": person_json.get("account_number", ""),
                            "account_name": person_json.get("account_name", ""),
                            "bank": person_json.get("bank", ""),
                            "kyc_status": person_json.get("validation_status", ""),
                        }

        def rows():
            records = queryset.iterator(chunk_size=ENROLMENT_EXPORT_BATCH_SIZE)
            while batch := list(islice(records, ENROLMENT_EXPORT_BATCH_SIZE)):
                yield from build_rows(batch)

        from core.models import ExportableQueryModel
        import uuid
        filename = f"{uuid.uuid4()}.xlsx"
        export = ExportableQueryModel(
            name=filename,
            model=queryset.model.__name__,
            content=ContentFile(cls._enrolment_workbook(
                rows(),
                KYC_EXPORT_COLUMNS if primary_recipient_only else ENROLMENT_EXPORT_COLUMNS,
            ), filename),
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
            requested_fields = kwargs.pop("fields", None)
            primary_recipient_only = kwargs.get("primary_recipient_only", False)
            if primary_recipient_only:
                # KYC always writes the fixed KYC workbook below. Its display
                # columns (for example catchment_name and full_name) are not
                # arbitrary queryset fields and must not be passed through the
                # generic model-column allow-list. The only internal query
                # field required for this safe fixed export is the record ID.
                if not isinstance(requested_fields, (list, tuple)) or not requested_fields:
                    raise ValidationError("KYC export requires a non-empty list of column names.")
                export_fields = ["id"]
            else:
                export_fields = cls.validate_export_columns(field_name, requested_fields)
            fields_mapping = json.loads(kwargs.pop('fields_columns'))
            file_format = kwargs.pop("file_format", "csv")

            source_field = getattr(cls, field_name)
            filter_kwargs = {k: v for k, v in kwargs.items() if k in source_field.filtering_args}

            qs = default_resolve(None, info, **kwargs)
            qs = qs.filter(**filter_kwargs)
            qs = cls.__append_custom_filters(custom_filters, qs, fields_mapping)
            if file_format == "xlsx":
                return cls._create_enrolment_xlsx(
                    qs,
                    field_name,
                    info.context.user,
                    primary_recipient_only=primary_recipient_only,
                    phase_beneficiary_summary_only=kwargs.get(
                        "phase_beneficiary_summary_only", False,
                    ),
                    # Graphene passes the Django filter argument in snake case;
                    # retain the camel-case fallback for older schemas.
                    benefit_plan_id=(
                        kwargs.get("benefit_plan__id")
                        or kwargs.get("benefitPlanId")
                    ),
                ).name
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
