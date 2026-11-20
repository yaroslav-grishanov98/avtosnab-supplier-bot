import re
import uuid
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils.text import get_valid_filename


def persist_supplier_price_source_file(*, source_file, supplier_id, content_sha256, source_file_name):
    extension = Path(source_file_name or source_file.name or "upload").suffix.lower()
    base_name = Path(source_file_name or source_file.name or "upload").stem
    safe_base_name = get_valid_filename(base_name)[:80] or "upload"
    safe_base_name = re.sub(r"_+", "_", safe_base_name).strip("._") or "upload"
    relative_path = (
        f"supplier_price_uploads/{supplier_id}/{content_sha256[:2]}/{content_sha256[:16]}-{safe_base_name}{extension}"
    )

    if default_storage.exists(relative_path):
        source_file.seek(0)
        return relative_path

    source_file.seek(0)
    saved_path = default_storage.save(relative_path, ContentFile(source_file.read()))
    source_file.seek(0)
    return saved_path


def persist_supplier_logo(*, logo_file, supplier_id):
    extension = Path(logo_file.name or "logo").suffix.lower() or ".jpg"
    relative_path = f"supplier_logos/{supplier_id}/{uuid.uuid4().hex}{extension}"
    saved_path = default_storage.save(relative_path, ContentFile(logo_file.read()))
    return default_storage.url(saved_path)


def persist_supplier_product_image(*, image_file, supplier_product_id):
    extension = Path(image_file.name or "image").suffix.lower() or ".jpg"
    relative_path = f"supplier_product_images/{supplier_product_id}/{uuid.uuid4().hex}{extension}"
    saved_path = default_storage.save(relative_path, ContentFile(image_file.read()))
    return default_storage.url(saved_path)


def persist_supplier_join_request_document(*, document_file, supplier_id):
    extension = Path(document_file.name or "document").suffix.lower() or ".bin"
    relative_path = f"supplier_join_request_documents/{supplier_id}/{uuid.uuid4().hex}{extension}"
    saved_path = default_storage.save(relative_path, ContentFile(document_file.read()))
    return default_storage.url(saved_path)
