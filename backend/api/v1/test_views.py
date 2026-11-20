"""
Comprehensive tests for api/v1/views.py.

Covers all view functions and private helpers that previously had 0 test coverage.
"""

import json
import shutil
import tempfile
import uuid
from datetime import timedelta

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from api.v1.validators import mask_inn as _mask_inn
from api.v1.validators import validate_inn as _validate_inn
from core.models import EntityStatus
from suppliers.models import (
    BotUserBinding,
    CabinetSession,
    SourceChannel,
    Supplier,
    SupplierPriceUpload,
    SupplierProduct,
    SupplierProductPrice,
    UploadStatus,
)

# ---------------------------------------------------------------------------
# HealthView
# ---------------------------------------------------------------------------


class HealthViewTest(TestCase):
    def test_health_returns_ok(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


# ---------------------------------------------------------------------------
# price_list_crud
# ---------------------------------------------------------------------------


class PriceListCrudTest(TestCase):
    def test_price_list_returns_200(self):
        response = self.client.get("/prices/")
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# supplier_products_page
# ---------------------------------------------------------------------------


class SupplierProductsPageTest(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Test Supplier",
            legal_name="Test Supplier LLC",
            status=EntityStatus.ACTIVE,
        )

    def test_existing_supplier_returns_200(self):
        response = self.client.get(f"/supplier/{self.supplier.pk}/")
        self.assertEqual(response.status_code, 200)

    def test_unknown_supplier_returns_404(self):
        response = self.client.get(f"/supplier/{uuid.uuid4()}/")
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# cabinet_view
# ---------------------------------------------------------------------------


class CabinetViewTest(TestCase):
    def setUp(self):
        cache.clear()
        self.supplier = Supplier.objects.create(
            display_name="Cabinet Supplier",
            legal_name="Cabinet Supplier LLC",
            status=EntityStatus.ACTIVE,
        )
        self.binding = BotUserBinding.objects.create(
            supplier=self.supplier,
            channel=SourceChannel.TELEGRAM,
            chat_id="12345",
            status=EntityStatus.ACTIVE,
        )

    def test_no_token_no_session_shows_error(self):
        response = self.client.get("/cabinet/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Откройте кабинет")

    def test_valid_token_marks_used_and_sets_session(self):
        session = CabinetSession.objects.create(
            token="test-token-123",
            binding=self.binding,
            expires_at=timezone.now() + timedelta(hours=1),
            used=False,
        )
        response = self.client.get("/cabinet/?token=test-token-123")
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertTrue(session.used)
        self.assertEqual(self.client.session.get("cabinet_supplier_id"), str(self.supplier.pk))

    def test_expired_token_shows_error(self):
        CabinetSession.objects.create(
            token="expired-token",
            binding=self.binding,
            expires_at=timezone.now() - timedelta(hours=1),
            used=False,
        )
        response = self.client.get("/cabinet/?token=expired-token")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "истекла")

    def test_already_used_token_shows_error(self):
        CabinetSession.objects.create(
            token="used-token",
            binding=self.binding,
            expires_at=timezone.now() + timedelta(hours=1),
            used=True,
        )
        response = self.client.get("/cabinet/?token=used-token")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "истекла")

    def test_session_present_allows_access_without_token(self):
        """After using a valid token once, subsequent F5 refreshes must work."""
        CabinetSession.objects.create(
            token="first-access-token",
            binding=self.binding,
            expires_at=timezone.now() + timedelta(hours=1),
            used=False,
        )
        # First visit uses the token and sets the session.
        self.client.get("/cabinet/?token=first-access-token")
        # Second visit without a token should render the cabinet, not the error.
        response = self.client.get("/cabinet/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Откройте кабинет через Telegram-бот.")


# ---------------------------------------------------------------------------
# cabinet_by_supplier_view
# ---------------------------------------------------------------------------


class CabinetBySupplierViewTest(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Supplier For Cabinet",
            legal_name="Supplier For Cabinet LLC",
            status=EntityStatus.ACTIVE,
        )

    def test_no_session_shows_error(self):
        response = self.client.get(f"/cabinet/supplier/{self.supplier.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Откройте кабинет")

    def test_session_with_supplier_id_allows_access(self):
        session = self.client.session
        session["cabinet_supplier_ids"] = [str(self.supplier.pk)]
        session.save()
        response = self.client.get(f"/cabinet/supplier/{self.supplier.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Откройте кабинет через Telegram-бот.")


# ---------------------------------------------------------------------------
# cabinet_update_proxy
# ---------------------------------------------------------------------------


class CabinetUpdateProxyTest(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Old Name",
            legal_name="Old Legal Name",
            status=EntityStatus.ACTIVE,
        )

    def _url(self, supplier_id=None):
        sid = supplier_id or self.supplier.pk
        return f"/cabinet/supplier/{sid}/update/"

    def test_valid_json_updates_display_name_and_returns_200(self):
        response = self.client.post(
            self._url(),
            data=json.dumps({"display_name": "New Display Name"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["display_name"], "New Display Name")
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.display_name, "New Display Name")

    def test_metadata_is_merged_not_replaced(self):
        self.supplier.metadata = {"existing_key": "existing_value"}
        self.supplier.save()
        response = self.client.post(
            self._url(),
            data=json.dumps({"metadata": {"new_key": "new_value"}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.metadata["existing_key"], "existing_value")
        self.assertEqual(self.supplier.metadata["new_key"], "new_value")

    def test_invalid_json_returns_400(self):
        response = self.client.post(
            self._url(),
            data="not-valid-json{{{",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_supplier_returns_404(self):
        response = self.client.post(
            self._url(supplier_id=uuid.uuid4()),
            data=json.dumps({"display_name": "Name"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_min_order_amount_returns_400(self):
        response = self.client.post(
            self._url(),
            data=json.dumps({"min_order_amount": "not-a-number"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# supplier_product_image_upload
# ---------------------------------------------------------------------------


class SupplierProductImageUploadTest(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Image Upload Supplier",
            legal_name="LLC",
            status=EntityStatus.ACTIVE,
        )
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _url(self, supplier_id=None):
        sid = supplier_id or self.supplier.pk
        return f"/supplier/{sid}/api/images/"

    def test_no_file_returns_400(self):
        with override_settings(MEDIA_ROOT=self.tmpdir, MEDIA_URL="/media/"):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 400)
        self.assertIn("Файл не передан", response.json()["detail"])

    def test_disallowed_content_type_returns_400(self):
        with override_settings(MEDIA_ROOT=self.tmpdir, MEDIA_URL="/media/"):
            txt_file = SimpleUploadedFile("file.txt", b"plain text content", content_type="text/plain")
            response = self.client.post(self._url(), {"image": txt_file})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Допустимые форматы", response.json()["detail"])

    def test_oversized_file_returns_400(self):
        # 5 MB + 1 byte
        large_data = b"\xff\xd8\xff" + b"X" * (5 * 1024 * 1024 + 1)
        with override_settings(MEDIA_ROOT=self.tmpdir, MEDIA_URL="/media/"):
            big_file = SimpleUploadedFile("big.jpg", large_data, content_type="image/jpeg")
            response = self.client.post(self._url(), {"image": big_file})
        self.assertEqual(response.status_code, 400)
        self.assertIn("5 МБ", response.json()["detail"])

    def test_valid_jpeg_returns_200_with_image_url(self):
        # Minimal JPEG bytes (SOI + EOI markers)
        jpeg_data = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        with override_settings(MEDIA_ROOT=self.tmpdir, MEDIA_URL="/media/"):
            image_file = SimpleUploadedFile("photo.jpg", jpeg_data, content_type="image/jpeg")
            response = self.client.post(self._url(), {"image": image_file})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("image_url", data)
        self.assertTrue(data["image_url"].startswith("/media/"))
        self.assertTrue(data["image_url"].endswith(".jpg"))

    def test_valid_png_returns_200(self):
        # Minimal 1×1 PNG
        png_data = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
            b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
            b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with override_settings(MEDIA_ROOT=self.tmpdir, MEDIA_URL="/media/"):
            image_file = SimpleUploadedFile("photo.png", png_data, content_type="image/png")
            response = self.client.post(self._url(), {"image": image_file})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["image_url"].endswith(".png"))

    def test_unknown_supplier_returns_404(self):
        with override_settings(MEDIA_ROOT=self.tmpdir, MEDIA_URL="/media/"):
            jpeg_data = b"\xff\xd8\xff\xd9"
            image_file = SimpleUploadedFile("photo.jpg", jpeg_data, content_type="image/jpeg")
            response = self.client.post(self._url(supplier_id=uuid.uuid4()), {"image": image_file})
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# supplier_product_update_proxy
# ---------------------------------------------------------------------------


class SupplierProductUpdateProxyTest(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Update Supplier",
            legal_name="LLC",
            status=EntityStatus.ACTIVE,
        )
        self.product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Test Product",
            normalized_name="Test Product Normalized",
            status=EntityStatus.ACTIVE,
        )

    def _url(self, supplier_id=None, product_id=None):
        sid = supplier_id or self.supplier.pk
        pid = product_id or self.product.pk
        return f"/supplier/{sid}/api/products/{pid}/"

    def test_patch_comment_returns_200_and_updates_product(self):
        response = self.client.patch(
            self._url(),
            data=json.dumps({"comment": "Updated via test"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["comment"], "Updated via test")

    def test_patch_unknown_supplier_returns_404(self):
        response = self.client.patch(
            self._url(supplier_id=uuid.uuid4()),
            data=json.dumps({"comment": "x"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_patch_unknown_product_returns_404(self):
        response = self.client.patch(
            self._url(product_id=uuid.uuid4()),
            data=json.dumps({"comment": "x"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_patch_invalid_json_returns_400(self):
        response = self.client.patch(
            self._url(),
            data="{{not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_patch_empty_body_returns_400(self):
        """The serializer requires at least one field."""
        response = self.client.patch(
            self._url(),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# supplier_product_archive_proxy
# ---------------------------------------------------------------------------


class SupplierProductArchiveProxyTest(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Archive Supplier",
            legal_name="LLC",
            status=EntityStatus.ACTIVE,
        )
        self.product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Product To Archive",
            normalized_name="Product To Archive Normalized",
            status=EntityStatus.ACTIVE,
        )

    def _url(self, supplier_id=None, product_id=None):
        sid = supplier_id or self.supplier.pk
        pid = product_id or self.product.pk
        return f"/supplier/{sid}/api/products/{pid}/archive/"

    def test_archive_product_returns_200_with_status(self):
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "archived"})

    def test_archive_changes_product_status_to_archived(self):
        self.client.post(self._url())
        self.product.refresh_from_db()
        self.assertEqual(self.product.status, EntityStatus.ARCHIVED)

    def test_archive_unknown_product_returns_404(self):
        response = self.client.post(self._url(product_id=uuid.uuid4()))
        self.assertEqual(response.status_code, 404)

    def test_archive_unknown_supplier_returns_404(self):
        response = self.client.post(self._url(supplier_id=uuid.uuid4()))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# supplier_upload_text_proxy
# ---------------------------------------------------------------------------


class SupplierUploadTextProxyTest(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Text Upload Supplier",
            legal_name="LLC",
            status=EntityStatus.ACTIVE,
        )

    def _url(self, supplier_id=None):
        sid = supplier_id or self.supplier.pk
        return f"/supplier/{sid}/api/uploads/text/"

    def test_valid_text_upload_returns_201_with_upload_id(self):
        response = self.client.post(
            self._url(),
            data=json.dumps({"text": "Болт М8 150 руб шт", "source_channel": "web"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("upload_id", data)
        # Verify a SupplierPriceUpload was actually created.
        self.assertTrue(SupplierPriceUpload.objects.filter(pk=data["upload_id"]).exists())

    def test_default_source_channel_is_web(self):
        """source_channel defaults to 'web' even if not provided."""
        response = self.client.post(
            self._url(),
            data=json.dumps({"text": "Гайка М10 80 руб шт"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        upload_id = response.json()["upload_id"]
        upload = SupplierPriceUpload.objects.get(pk=upload_id)
        self.assertEqual(upload.source_channel, SourceChannel.WEB)

    def test_missing_text_returns_400(self):
        response = self.client.post(
            self._url(),
            data=json.dumps({"source_channel": "web"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_supplier_returns_404(self):
        response = self.client.post(
            self._url(supplier_id=uuid.uuid4()),
            data=json.dumps({"text": "Some text"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_json_body_returns_400(self):
        response = self.client.post(
            self._url(),
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# supplier_upload_apply_proxy
# ---------------------------------------------------------------------------


class SupplierUploadApplyProxyTest(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Apply Supplier",
            legal_name="LLC",
            status=EntityStatus.ACTIVE,
        )
        self.upload = SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.WEB,
            status=UploadStatus.PROCESSING,
        )

    def _url(self, supplier_id=None, upload_id=None):
        sid = supplier_id or self.supplier.pk
        uid = upload_id or self.upload.pk
        return f"/supplier/{sid}/api/uploads/{uid}/apply/"

    def _valid_payload(self, **overrides):
        payload = {
            "schema_version": "supplier_price_ai.v1",
            "processing_version": "test-v1",
            "products": [],
        }
        payload.update(overrides)
        return payload

    def test_valid_empty_products_returns_200(self):
        response = self.client.post(
            self._url(),
            data=json.dumps(self._valid_payload()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("upload_id", data)
        self.assertEqual(data["items_total"], 0)

    def test_upload_status_becomes_applied_after_empty_products(self):
        self.client.post(
            self._url(),
            data=json.dumps(self._valid_payload()),
            content_type="application/json",
        )
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.APPLIED)

    def test_wrong_schema_version_returns_400(self):
        payload = self._valid_payload()
        payload["schema_version"] = "unsupported.schema"
        response = self.client.post(
            self._url(),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_supplier_returns_404(self):
        response = self.client.post(
            self._url(supplier_id=uuid.uuid4()),
            data=json.dumps(self._valid_payload()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_json_body_returns_400(self):
        response = self.client.post(
            self._url(),
            data="not-json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# supplier_product_price_history
# ---------------------------------------------------------------------------


class SupplierProductPriceHistoryTest(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Price History Supplier",
            legal_name="LLC",
            status=EntityStatus.ACTIVE,
        )
        self.product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Price History Product",
            normalized_name="Price History Product Normalized",
            status=EntityStatus.ACTIVE,
        )

    def _url(self, supplier_id=None, product_id=None):
        sid = supplier_id or self.supplier.pk
        pid = product_id or self.product.pk
        return f"/supplier/{sid}/api/products/{pid}/prices/"

    def test_get_empty_history_returns_200(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("product_name", data)
        self.assertIn("history", data)
        self.assertEqual(data["history"], [])

    def test_product_name_falls_back_to_supplier_name(self):
        product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Raw Name Only",
            normalized_name="",
            status=EntityStatus.ACTIVE,
        )
        response = self.client.get(self._url(product_id=product.pk))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # normalized_name is empty, so supplier_name is used
        self.assertEqual(data["product_name"], "Raw Name Only")

    def test_price_records_appear_in_history(self):
        SupplierProductPrice.objects.create(
            supplier_product=self.product,
            price="150.00",
            valid_from=timezone.now(),
            status="active",
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["history"][0]["price"], "150.00")

    def test_unknown_product_returns_404(self):
        response = self.client.get(self._url(product_id=uuid.uuid4()))
        self.assertEqual(response.status_code, 404)

    def test_unknown_supplier_returns_404(self):
        response = self.client.get(self._url(supplier_id=uuid.uuid4()))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# suppliers_list_view
# ---------------------------------------------------------------------------


class SuppliersListViewTest(TestCase):
    def setUp(self):
        Supplier.objects.create(
            display_name="Active Company",
            legal_name="Active LLC",
            inn="7707083893",
            status=EntityStatus.ACTIVE,
        )
        Supplier.objects.create(
            display_name="Inactive Company",
            legal_name="Inactive LLC",
            status=EntityStatus.INACTIVE,
        )

    def test_returns_200(self):
        response = self.client.get("/suppliers/")
        self.assertEqual(response.status_code, 200)

    def test_only_active_suppliers_appear(self):
        response = self.client.get("/suppliers/")
        self.assertContains(response, "Active Company")
        self.assertNotContains(response, "Inactive Company")


# ---------------------------------------------------------------------------
# supplier_add_view
# ---------------------------------------------------------------------------


class SupplierAddViewTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_get_returns_200(self):
        response = self.client.get("/suppliers/add/")
        self.assertEqual(response.status_code, 200)

    def test_get_with_added_param_sets_success_context(self):
        response = self.client.get("/suppliers/add/?added=1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("success"))

    def test_post_valid_data_creates_supplier_and_redirects(self):
        response = self.client.post(
            "/suppliers/add/",
            {
                "company_name": "New Company LLC",
                "full_name": "Иван Иванов",
                "email": "ivan@newcompany.com",
                "inn": "7707083893",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("added=1", response["Location"])
        # A new inactive supplier must have been created.
        self.assertTrue(Supplier.objects.filter(inn="7707083893").exists())

    def test_post_with_existing_active_supplier_shows_existing_supplier(self):
        Supplier.objects.create(
            display_name="Already On Platform",
            legal_name="Already LLC",
            inn="7707083893",
            status=EntityStatus.ACTIVE,
        )
        response = self.client.post(
            "/suppliers/add/",
            {
                "company_name": "New Company",
                "full_name": "Иван Иванов",
                "email": "ivan@example.com",
                "inn": "7707083893",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("existing_supplier", response.context)
        self.assertIsNotNone(response.context["existing_supplier"])

    def test_post_missing_company_name_returns_errors(self):
        response = self.client.post(
            "/suppliers/add/",
            {
                "company_name": "",
                "full_name": "Иван Иванов",
                "email": "ivan@example.com",
                "inn": "7707083893",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("errors", response.context)
        self.assertIn("company_name", response.context["errors"])

    def test_post_missing_full_name_returns_errors(self):
        response = self.client.post(
            "/suppliers/add/",
            {
                "company_name": "Company",
                "full_name": "",
                "email": "ivan@example.com",
                "inn": "7707083893",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("full_name", response.context["errors"])

    def test_post_invalid_inn_returns_inn_error(self):
        response = self.client.post(
            "/suppliers/add/",
            {
                "company_name": "Company",
                "full_name": "Ivan",
                "email": "ivan@example.com",
                "inn": "abc",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("inn", response.context["errors"])

    def test_post_non_digit_inn_returns_inn_error(self):
        response = self.client.post(
            "/suppliers/add/",
            {
                "company_name": "Company",
                "full_name": "Ivan",
                "email": "ivan@example.com",
                "inn": "abcdefghij",  # 10 chars, non-digit
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("inn", response.context["errors"])


# ---------------------------------------------------------------------------
# _mask_inn (pure function, no DB)
# ---------------------------------------------------------------------------


class MaskInnTest(TestCase):
    def test_ten_digit_inn_is_masked(self):
        self.assertEqual(_mask_inn("7707083893"), "7707****93")

    def test_twelve_digit_inn_is_masked(self):
        # len=12: first 4 + 6 stars + last 2
        result = _mask_inn("500100732259")
        self.assertEqual(result, "5001******59")

    def test_inn_shorter_than_six_returned_as_is(self):
        self.assertEqual(_mask_inn("12345"), "12345")

    def test_none_returns_empty_string(self):
        self.assertEqual(_mask_inn(None), "")

    def test_empty_string_returns_empty_string(self):
        self.assertEqual(_mask_inn(""), "")

    def test_exactly_six_chars_no_stars(self):
        # len=6: 4 + 0 stars + 2 → same as input
        self.assertEqual(_mask_inn("123456"), "123456")


# ---------------------------------------------------------------------------
# _validate_inn (pure function, no DB)
# ---------------------------------------------------------------------------


class ValidateInnTest(TestCase):
    def test_valid_10_digit_inn_returns_none(self):
        # Сбербанк ИНН — passes checksum
        self.assertIsNone(_validate_inn("7707083893"))

    def test_invalid_checksum_10_digit_returns_error(self):
        result = _validate_inn("1234567890")
        self.assertIsNotNone(result)
        self.assertIn("контрольной суммы", result)

    def test_non_digit_inn_returns_error(self):
        result = _validate_inn("abc")
        self.assertIsNotNone(result)
        self.assertIn("цифры", result)

    def test_inn_with_spaces_but_non_digit_returns_error(self):
        result = _validate_inn("7707 abc 93")
        self.assertIsNotNone(result)
        self.assertIn("цифры", result)

    def test_wrong_length_inn_returns_error(self):
        result = _validate_inn("12345")
        self.assertIsNotNone(result)
        self.assertIn("10 цифр", result)

    def test_nine_digit_inn_returns_length_error(self):
        result = _validate_inn("123456789")
        self.assertIsNotNone(result)
        self.assertIn("10 цифр", result)

    def test_eleven_digit_inn_returns_length_error(self):
        result = _validate_inn("12345678901")
        self.assertIsNotNone(result)
        self.assertIn("10 цифр", result)

    def test_valid_12_digit_inn_returns_none(self):
        # Valid 12-digit individual entrepreneur INN
        self.assertIsNone(_validate_inn("500100732259"))

    def test_invalid_12_digit_inn_returns_checksum_error(self):
        result = _validate_inn("123456789012")
        self.assertIsNotNone(result)
        self.assertIn("контрольной суммы", result)

    def test_empty_string_returns_error(self):
        # "".isdigit() is False, so the function returns the "digits only" error.
        result = _validate_inn("")
        self.assertIsNotNone(result)
        self.assertIn("цифры", result)
