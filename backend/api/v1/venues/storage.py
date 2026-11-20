import uuid
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

# Mirrors api.v1.suppliers.storage's file-persistence helpers, subset needed
# for the venue cabinet mirror (docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md).


def persist_venue_logo(*, logo_file, venue_id):
    extension = Path(logo_file.name or "logo").suffix.lower() or ".jpg"
    relative_path = f"venue_logos/{venue_id}/{uuid.uuid4().hex}{extension}"
    saved_path = default_storage.save(relative_path, ContentFile(logo_file.read()))
    return default_storage.url(saved_path)


def persist_venue_join_request_document(*, document_file, venue_id):
    extension = Path(document_file.name or "document").suffix.lower() or ".bin"
    relative_path = f"venue_join_request_documents/{venue_id}/{uuid.uuid4().hex}{extension}"
    saved_path = default_storage.save(relative_path, ContentFile(document_file.read()))
    return default_storage.url(saved_path)
