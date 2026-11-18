import json
import logging
import types
from io import BytesIO
from itertools import islice

from django.apps import apps
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
    "bank", "kyc_status",
)
ENROLMENT_EXPORT_BATCH_SIZE = 500


def _first_json_ext_value(json_ext, *keys):
    if isinstance(json_ext, str):
        try:
            json_ext = json.loads(json_ext)
        except (TypeError, ValueError):
            return ""
    if not isinstance(json_ext, dict):
        return ""
    for key in keys:
        value = json_ext.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


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
        from individual.models import GroupIndividual, Individual
        from location.models import MicroCatchmentGVH, MicroCatchmentTA
        from project_social_protection.models import (
            BeneficiaryProjectEnrollment, GroupBeneficiaryProjectEnrollment,
        )
        from social_protection.models import BenefitPlan, BeneficiaryStatus

        if primary_recipient_only and field_name != "group_beneficiary":
            raise ValidationError("KYC export is only available for household beneficiaries.")
        if (primary_recipient_only or phase_beneficiary_summary_only) and not benefit_plan_id:
            raise ValidationError("Phase exports require a benefit plan.")

        phase_name = ""
        if benefit_plan_id:
            phase = BenefitPlan.objects.filter(
                id=benefit_plan_id,
                is_deleted=False,
            ).values("code", "name", "type").first()
            if (primary_recipient_only or phase_beneficiary_summary_only) and not phase:
                raise ValidationError("The selected benefit plan does not exist.")
            if (
                (primary_recipient_only or phase_beneficiary_summary_only)
                and phase["type"] != BenefitPlan.BenefitPlanType.GROUP_TYPE
            ):
                raise ValidationError("Phase exports require a household benefit plan.")
            if phase:
                phase_name = " ".join(
                    value for value in (phase["code"], phase["name"]) if value
                )

        is_group_export = field_name == "group_beneficiary"
        relation = "group" if is_group_export else "individual"
        enrollment_model = (
            GroupBeneficiaryProjectEnrollment if is_group_export
            else BeneficiaryProjectEnrollment
        )
        enrollment_beneficiary_field = "group_beneficiary" if is_group_export else "beneficiary"
        enrolment_prefetch = Prefetch(
            "project_enrollments",
            queryset=enrollment_model.objects.filter(is_deleted=False).select_related(
                "project__activity", "project__benefit_plan",
            ).order_by("date_created", "id"),
        )
        prefetches = [] if primary_recipient_only else [enrolment_prefetch]
        if is_group_export and not primary_recipient_only:
            prefetches.append(Prefetch(
                "group__groupindividuals",
                queryset=GroupIndividual.objects.select_related("individual").order_by(
                    "group_id", "individual_id", "-date_updated"
                ),
                to_attr="export_group_members",
            ))
        enrolled_by_project = {
            row["project_id"]: row["count"]
            for row in enrollment_model.objects.filter(
                is_deleted=False,
                **{f"{enrollment_beneficiary_field}_id__in": queryset.order_by().values("pk")},
            ).values("project_id").annotate(count=Count("id"))
        } if not primary_recipient_only else {}
        queryset = queryset.select_related(f"{relation}__location__parent__parent__parent")
        queryset = queryset.prefetch_related(*prefetches)
        emitted_household_ids = set()

        def build_rows(records):
            record_locations = {}
            record_location_ids = {"D": set(), "W": set()}
            for record in records:
                identity = getattr(record, relation)
                location = identity.location
                current = location
                while current:
                    if current.type in record_location_ids:
                        record_location_ids[current.type].add(current.id)
                    current = getattr(current, "parent", None)
                record_locations[record.id] = location

            kyc_members_by_group = {}
            individual_status_field = "validation_status"
            verified_individual_status = "VERIFIED"
            if primary_recipient_only:
                if not apps.is_installed("enrolment"):
                    raise ValidationError("KYC export requires the Enrolment module.")
                from enrolment.apps import EnrolmentConfig
                from individual.primary_recipients import authoritative_primary_worker_memberships

                individual_status_field = EnrolmentConfig.registry_individual_verification_field
                verified_individual_status = EnrolmentConfig.registry_verified_individual_value
                active_group_ids = {
                    record.group_id for record in records
                    if (
                        not record.is_deleted
                        and record.status == BeneficiaryStatus.ACTIVE
                        and str(record.benefit_plan_id) == str(benefit_plan_id)
                    )
                }
                memberships = authoritative_primary_worker_memberships().filter(
                    group_id__in=active_group_ids,
                ).select_related("individual").order_by("group_id", "id")
                for membership in memberships:
                    kyc_members_by_group.setdefault(membership.group_id, []).append(membership)

            members_by_group = {}
            archived_members_by_group = {}
            if is_group_export and not primary_recipient_only:
                for record in records:
                    identity = getattr(record, relation)
                    for member in identity.export_group_members:
                        if not member.is_deleted:
                            members_by_group.setdefault(member.group_id, []).append(member)
                        elif not (primary_recipient_only or phase_beneficiary_summary_only):
                            archived_members_by_group.setdefault(member.group_id, {}) \
                                .setdefault(member.individual_id, member)

            # Older household imports may have lost their active GroupIndividual
            # links while retaining member IDs in Group.json_ext. Keep those reports
            # useful by resolving the saved IDs back to Individual records.
            legacy_member_ids_by_group = {}
            legacy_individuals = {}
            if is_group_export and not (primary_recipient_only or phase_beneficiary_summary_only):
                legacy_member_ids = set()
                for record in records:
                    identity = getattr(record, relation)
                    group_json = identity.json_ext or {}
                    member_ids = list((group_json.get("members") or {}).keys())
                    legacy_member_ids_by_group[identity.id] = member_ids
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

            for record in records:
                identity = getattr(record, relation)
                if primary_recipient_only:
                    if (
                        record.is_deleted
                        or record.status != BeneficiaryStatus.ACTIVE
                        or str(record.benefit_plan_id) != str(benefit_plan_id)
                    ):
                        continue
                    primary_memberships = kyc_members_by_group.get(identity.id, [])
                    if (
                        len(primary_memberships) != 1
                        or _first_json_ext_value(
                            primary_memberships[0].individual.json_ext,
                            individual_status_field,
                        ) != str(verified_individual_status)
                    ):
                        logger.warning(
                            "Skipping group beneficiary %s from KYC export: "
                            "no single verified primary recipient was found",
                            record.id,
                        )
                        continue
                if phase_beneficiary_summary_only or primary_recipient_only:
                    if identity.id in emitted_household_ids:
                        logger.warning(
                            "Skipping duplicate group beneficiary %s from Phase export",
                            record.id,
                        )
                        continue
                    emitted_household_ids.add(identity.id)
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
                if primary_recipient_only:
                    membership = kyc_members_by_group[identity.id][0]
                    members = [(membership.individual, membership.role)]
                elif phase_beneficiary_summary_only:
                    primary_memberships = [
                        member
                        for member in members_by_group.get(identity.id, [])
                        if member.recipient_type == GroupIndividual.RecipientType.PRIMARY
                    ]
                    if len(primary_memberships) == 1:
                        member = primary_memberships[0]
                        members = [(member.individual, member.role)]
                    else:
                        if len(primary_memberships) > 1:
                            logger.warning(
                                "Phase summary export found multiple active primary recipients "
                                "for group %s; exporting the household row without a recipient",
                                identity.id,
                            )
                        members = [(None, "")]
                elif is_group_export:
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
                        legacy_members = [
                            (
                                legacy_individuals.get(member_id),
                                "HEAD" if str(member_id) == head_id else "",
                            )
                            for member_id in legacy_member_ids_by_group.get(identity.id, [])
                        ]
                        members = legacy_members
                else:
                    members = [(identity, "")]
                members = members or [(None, "")]
                enrolments = (
                    [] if primary_recipient_only
                    else list(record.project_enrollments.all())
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
                            "national_id": (
                                _first_json_ext_value(
                                    person_json, "national_id", "nationalId", "nid",
                                )
                                if primary_recipient_only or phase_beneficiary_summary_only
                                else _first_json_ext_value(
                                    person_json, "national_id", "nationalId", "nid",
                                ) or _first_json_ext_value(
                                    record_json, "national_id", "nationalId", "nid",
                                ) or _first_json_ext_value(
                                    group_json, "national_id", "nationalId", "nid",
                                )
                            ),
                            "date_of_birth": person.dob.isoformat() if person and person.dob else "",
                            "Gender": _first_json_ext_value(person_json, "gender", "sex"),
                            "relationship": relationship,
                            "enrolment_type": "Household" if is_group_export else "Individual",
                            "account_number": _first_json_ext_value(
                                person_json,
                                "account_number", "accountNumber", "payment_account_number",
                                "paymentAccountNumber", "bank_account_number", "bankAccountNumber",
                                "account_no", "accountNo",
                            ),
                            "account_name": _first_json_ext_value(
                                person_json,
                                "account_name", "accountName", "payment_account_name",
                                "paymentAccountName", "bank_account_name", "bankAccountName",
                                "account_holder_name", "accountHolderName",
                            ),
                            "bank": _first_json_ext_value(
                                person_json, "bank", "bank_name", "bankName", "provider",
                                "payment_provider", "paymentProvider",
                            ),
                            "kyc_status": _first_json_ext_value(
                                person_json, individual_status_field,
                            ),
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
            phase_beneficiary_summary_only = kwargs.get(
                "phase_beneficiary_summary_only", False,
            )
            file_format = kwargs.pop("file_format", "csv")
            benefit_plan_id = kwargs.get("benefit_plan__id")
            phase_export = primary_recipient_only or phase_beneficiary_summary_only

            if primary_recipient_only:
                if field_name != "group_beneficiary":
                    raise ValidationError("KYC export is only available for household beneficiaries.")
                if file_format != "xlsx":
                    raise ValidationError("KYC export only supports the XLSX format.")
                if not benefit_plan_id:
                    raise ValidationError("KYC export requires a benefit plan.")
                if not apps.is_installed("enrolment"):
                    raise ValidationError("KYC export requires the Enrolment module.")
            if phase_beneficiary_summary_only and (
                field_name != "group_beneficiary" or file_format != "xlsx"
            ):
                raise ValidationError("Phase summary export only supports household XLSX exports.")
            if phase_beneficiary_summary_only and not benefit_plan_id:
                raise ValidationError("Phase summary export requires a benefit plan.")
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
                    phase_beneficiary_summary_only=phase_beneficiary_summary_only,
                    benefit_plan_id=benefit_plan_id if phase_export else kwargs.get("benefit_plan__id"),
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
