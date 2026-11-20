import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, Tender, TenderAccess, TenderChange, TenderFile, TenderItem, TenderStatus
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierUser, SupplierUserRole


class TenderFilesAPITests(APITestCase):
    """Raw ТЗ §6 (documents, specifications), §16 (add and remove on an active tender), §17 and §53 (shown to
    the buyer and to the supplier), docs/adr/0011."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        media = override_settings(MEDIA_ROOT=self.media_root, MEDIA_URL="/media/")
        media.enable()
        self.addCleanup(media.disable)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)

        self.owner = User.objects.create(email="files-owner@example.com", full_name="Анна", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(email="files-str@example.com", full_name="Чужой", role=UserRole.VENUE_USER)
        venue = Venue.objects.create(display_name="Кафе файлов")
        VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.supplier = Supplier.objects.create(legal_name="Files LLC", display_name="Files Co")
        self.supplier_user = User.objects.create(email="files-sup@example.com", full_name="S", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Со спецификацией", access=TenderAccess.INVITE_ONLY, status=TenderStatus.ACTIVE
        )
        self.tender.invited_suppliers.set([self.supplier])
        cream = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        TenderItem.objects.create(tender=self.tender, master_product=cream).venues.set([venue])
        self.files_url = f"/api/v1/catalog/tenders/{self.tender.id}/files"

    def auth(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.owner))

    def upload(self, name="Спецификация.pdf", content=b"%PDF-1.4 spec", user=None, url=None):
        self.auth(user)
        upload = SimpleUploadedFile(name, content, content_type="application/octet-stream")
        return self.client.post(url or self.files_url, {"file": upload}, format="multipart")

    def stored_file(self, name="old.pdf"):
        return TenderFile.objects.create(
            tender=self.tender,
            name=name,
            storage_path=default_storage.save(f"tender_files/{name}", SimpleUploadedFile(name, b"x")),
            size=1,
        )

    def test_upload_requires_authentication(self):
        upload = SimpleUploadedFile("a.pdf", b"x")
        response = self.client.post(self.files_url, {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 401)

    def test_other_owner_gets_403(self):
        response = self.upload(user=self.stranger)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(TenderFile.objects.exists())

    def test_upload_to_active_tender(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["name"], "Спецификация.pdf")
        self.assertEqual(data["size"], len(b"%PDF-1.4 spec"))
        self.assertTrue(data["url"].endswith(".pdf"))
        self.assertIn(f"tender_files/{self.tender.id}/", data["url"])
        stored = TenderFile.objects.get(pk=data["id"])
        self.assertTrue(default_storage.exists(stored.storage_path))

        card = self.client.get(f"/api/v1/catalog/tenders/{self.tender.id}").json()
        self.assertEqual([file["id"] for file in card["files"]], [data["id"]])
        change = TenderChange.objects.get(tender=self.tender)
        self.assertEqual(
            (change.change_type, change.new_value, change.actor_id), ("file_added", "Спецификация.pdf", self.owner.id)
        )

    def test_upload_to_draft_is_not_in_history(self):
        Tender.objects.filter(pk=self.tender.pk).update(status=TenderStatus.DRAFT)
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(TenderChange.objects.exists())

    def test_size_limit_boundary(self):
        with patch("api.v1.catalog.tender_files.TENDER_FILE_MAX_SIZE", 10):
            self.assertEqual(self.upload(content=b"x" * 10).status_code, 201)
            response = self.upload(content=b"x" * 11)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("detail", response.json())
        self.assertEqual(TenderFile.objects.count(), 1)

    def test_unsupported_format_rejected(self):
        response = self.upload(name="setup.exe")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("detail", response.json())

    def test_missing_file_rejected(self):
        self.auth()
        response = self.client.post(self.files_url, {}, format="multipart")
        self.assertEqual(response.status_code, 400, response.content)

    def test_files_per_tender_boundary(self):
        self.stored_file("one.pdf")
        with patch("api.v1.catalog.tender_files.MAX_TENDER_FILES", 2):
            self.assertEqual(self.upload().status_code, 201)
            response = self.upload()
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("detail", response.json())
        self.assertEqual(TenderFile.objects.filter(tender=self.tender).count(), 2)

    def test_completed_tender_rejected(self):
        Tender.objects.filter(pk=self.tender.pk).update(accepts_offers_until=timezone.now() - timedelta(minutes=1))
        response = self.upload()
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(TenderFile.objects.exists())

    def test_delete_file(self):
        file_id = self.upload().json()["id"]
        path = TenderFile.objects.get(pk=file_id).storage_path
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f"{self.files_url}/{file_id}")
        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(TenderFile.objects.filter(pk=file_id).exists())
        self.assertFalse(default_storage.exists(path))
        removed = TenderChange.objects.get(change_type="file_removed")
        self.assertEqual(removed.old_value, "Спецификация.pdf")

    def test_delete_other_tenders_file_is_404(self):
        other = Tender.objects.create(created_by=self.owner, name="Другой", access=TenderAccess.OPEN)
        stored = self.stored_file()
        self.auth()
        response = self.client.delete(f"/api/v1/catalog/tenders/{other.id}/files/{stored.id}")
        self.assertEqual(response.status_code, 404, response.content)
        self.assertTrue(TenderFile.objects.filter(pk=stored.pk).exists())

    def test_delete_by_other_owner_gets_403(self):
        stored = self.stored_file()
        self.auth(self.stranger)
        response = self.client.delete(f"{self.files_url}/{stored.id}")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(TenderFile.objects.filter(pk=stored.pk).exists())

    def test_delete_on_completed_tender_rejected(self):
        stored = self.stored_file()
        Tender.objects.filter(pk=self.tender.pk).update(status=TenderStatus.COMPLETED)
        self.auth()
        response = self.client.delete(f"{self.files_url}/{stored.id}")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertTrue(TenderFile.objects.filter(pk=stored.pk).exists())

    def test_invited_supplier_sees_files(self):
        stored = self.stored_file("Спецификация.pdf")
        self.auth(self.supplier_user)
        response = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [(f["id"], f["name"]) for f in response.json()["files"]], [(str(stored.id), "Спецификация.pdf")]
        )

    def test_buyer_list_queries_do_not_grow_with_files(self):
        self.stored_file("a.pdf")

        def list_queries():
            self.auth()
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get("/api/v1/catalog/tenders", {"owner_id": str(self.owner.id)})
            self.assertEqual(response.status_code, 200, response.content)
            return len(queries)

        one = list_queries()
        for index in range(3):
            tender = Tender.objects.create(created_by=self.owner, name=f"Ещё {index}", access=TenderAccess.OPEN)
            TenderFile.objects.create(tender=tender, name="b.pdf", storage_path=f"tender_files/{index}.pdf", size=1)
        self.assertEqual(list_queries(), one)
