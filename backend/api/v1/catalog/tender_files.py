"""Raw ТЗ §6/§16, docs/adr/0011: tender files in the project's shared
storage, served by a permanent public link like every other upload here."""

import uuid
from pathlib import Path

from django.core.files.storage import default_storage
from django.db import transaction
from rest_framework.exceptions import ValidationError

from api.v1.catalog.tender_editing import _reject_completed
from api.v1.catalog.tender_history import record_event
from catalog.models import TenderChangeType, TenderFile, TenderStatus

MAX_TENDER_FILES = 20
TENDER_FILE_MAX_SIZE = 20 * 1024 * 1024  # 20 МБ — как документы заявки на доступ
TENDER_FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png"}


def _reject_unsuitable_file(tender, uploaded_file):
    """Rejections carry a readable detail — the frontend shows it as is."""
    if Path(uploaded_file.name).suffix.lower() not in TENDER_FILE_EXTENSIONS:
        raise ValidationError({"detail": "Допустимые форматы файла: PDF, DOC, DOCX, XLS, XLSX, JPG, PNG."})
    if uploaded_file.size > TENDER_FILE_MAX_SIZE:
        raise ValidationError({"detail": "Файл больше 20 МБ."})
    if tender.files.count() >= MAX_TENDER_FILES:
        raise ValidationError({"detail": f"У тендера может быть не больше {MAX_TENDER_FILES} файлов."})


def add_tender_file(*, tender, uploaded_file, actor=None):
    _reject_completed(tender)
    _reject_unsuitable_file(tender, uploaded_file)
    name = Path(uploaded_file.name).name
    path = default_storage.save(
        f"tender_files/{tender.id}/{uuid.uuid4().hex}{Path(name).suffix.lower()}", uploaded_file
    )
    tender_file = TenderFile.objects.create(
        tender=tender, name=name, storage_path=path, size=uploaded_file.size, uploaded_by=actor
    )
    if tender.status != TenderStatus.DRAFT:
        record_event(tender, TenderChangeType.FILE_ADDED, actor=actor, new_value=name)
    return tender_file


@transaction.atomic
def delete_tender_file(*, tender_file, actor=None):
    tender = tender_file.tender
    _reject_completed(tender)
    path = tender_file.storage_path
    tender_file.delete()
    transaction.on_commit(lambda: default_storage.delete(path))
    if tender.status != TenderStatus.DRAFT:
        record_event(tender, TenderChangeType.FILE_REMOVED, actor=actor, old_value=tender_file.name)
