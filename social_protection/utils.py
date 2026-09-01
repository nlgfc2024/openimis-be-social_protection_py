from typing import Iterable

import pandas as pd
from django.db import transaction
from django.db.models import Q

from individual.models import IndividualDataSource


BULK_ENROLLMENT_BATCH_SIZE = 1000


def bulk_create_in_batches(manager, objects, batch_size=BULK_ENROLLMENT_BATCH_SIZE):
    """Persist an iterable without retaining the complete enrollment in memory."""
    with transaction.atomic():
        batch = []
        for obj in objects:
            batch.append(obj)
            if len(batch) == batch_size:
                manager.bulk_create(batch, batch_size=batch_size)
                batch = []
        if batch:
            manager.bulk_create(batch, batch_size=batch_size)


def load_dataframe(
    individual_sources: Iterable[IndividualDataSource]
) -> pd.DataFrame:
    data_from_source = []
    for individual_source in individual_sources:
        json_ext = individual_source.json_ext
        individual_source.json_ext["id"] = individual_source.id
        data_from_source.append(json_ext)
    recreated_df = pd.DataFrame(data_from_source)
    return recreated_df


def fetch_summary_of_broken_items(upload_id):
    return list(IndividualDataSource.objects.filter(
        Q(is_deleted=False)
        & Q(upload_id=upload_id)
        & ~Q(validations__validation_errors=[])
    ).values_list('uuid', flat=True))


def fetch_summary_of_valid_items(upload_id):
    return list(IndividualDataSource.objects.filter(
        Q(is_deleted=False)
        & Q(upload_id=upload_id)
        & Q(validations__validation_errors=[])
    ).values_list('uuid', flat=True))


def calculate_percentage_of_invalid_items(upload_id):
    number_of_valid_items = len(fetch_summary_of_valid_items(upload_id))
    number_of_invalid_items = len(fetch_summary_of_broken_items(upload_id))
    total_items = number_of_invalid_items + number_of_valid_items

    if total_items == 0:
        percentage_of_invalid_items = 0
    else:
        percentage_of_invalid_items = (
            number_of_invalid_items / total_items
        ) * 100

    percentage_of_invalid_items = round(percentage_of_invalid_items, 2)
    return percentage_of_invalid_items
