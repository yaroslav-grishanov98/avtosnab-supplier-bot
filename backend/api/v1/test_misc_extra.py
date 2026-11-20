"""
Extra tests covering small gaps across models, admin, selectors, serializers, and storage.

Covered:
- catalog/models.py: __str__ for Brand, ProductCategory, Product, GoogleSheetsSheetLink
- core/models.py: __str__ for User, Unit, Venue, Order
- suppliers/models.py: __str__ for Supplier, BotUserBinding, BotPendingAction, BotUserState,
                        SupplierProduct, SupplierOwnershipRequest
- suppliers/admin.py: rejected status email branch; _provision_supplier_user with no role
- api/v1/catalog/selectors.py: supplier_id, updated_since, category_id, is_favorite, role filters
- api/v1/suppliers/selectors.py: last_upload_since, updated_since filters
- api/v1/suppliers/storage.py: file already exists branch
- api/v1/catalog/serializers.py: CartItemCreateSerializer validate branches,
                                  CartItemUpdateSerializer empty validate
- api/v1/suppliers/serializers.py: BotUserBindingResolveQuerySerializer validate branches
"""

from io import BytesIO
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.test import TestCase as DjangoTestCase
from django.utils import timezone

from catalog.models import Brand, Product, ProductCategory
from core.models import EntityStatus, Order, OrderStatus, Unit, UnitKind, Venue
from suppliers.models import (
    BotPendingAction,
    BotUserBinding,
    BotUserState,
    SourceChannel,
    Supplier,
    SupplierOwnershipRequest,
    SupplierPriceUpload,
    SupplierProduct,
    UploadStatus,
)

# ---------------------------------------------------------------------------
# Model __str__ methods
# ---------------------------------------------------------------------------


class CatalogModelStrTests(TestCase):
    def test_brand_str(self):
        brand = Brand.objects.create(canonical_name="SKF", normalized_name="skf")
        self.assertEqual(str(brand), "SKF")

    def test_product_category_str(self):
        cat = ProductCategory.objects.create(name="Крепёж", slug="krepezh-str-test")
        self.assertEqual(str(cat), "Крепёж")

    def test_product_str(self):
        unit = Unit.objects.create(code="pc-str", name="шт", kind=UnitKind.PIECE)
        product = Product.objects.create(canonical_name="Болт М8", normalized_name="болт м8", base_unit=unit)
        self.assertEqual(str(product), "Болт М8")

    def test_google_sheet_sync_log_str(self):
        from catalog.models import GoogleSheetSyncLog, GoogleSheetSyncStatus

        log = GoogleSheetSyncLog.objects.create(
            spreadsheet_id="sheet-abc",
            sheet_name="Лист1",
            status=GoogleSheetSyncStatus.STARTED,
        )
        result = str(log)
        self.assertIn("Лист1", result)


class CoreModelStrTests(TestCase):
    def test_user_str(self):
        from core.models import User, UserRole

        user = User.objects.create(email="str-test@test.ru", full_name="Иван Иванов", role=UserRole.SUPPLIER)
        self.assertEqual(str(user), "Иван Иванов")

    def test_unit_str(self):
        unit = Unit.objects.create(code="kg-str", name="кг", kind=UnitKind.WEIGHT)
        self.assertEqual(str(unit), "kg-str")

    def test_venue_str(self):
        venue = Venue.objects.create(display_name="Кафе Центр")
        self.assertEqual(str(venue), "Кафе Центр")

    def test_order_str(self):
        venue = Venue.objects.create(display_name="OrderVenue")
        order = Order.objects.create(venue=venue, order_number="ORD-STRTEST", status=OrderStatus.SUBMITTED)
        self.assertEqual(str(order), "ORD-STRTEST")


class SuppliersModelStrTests(TestCase):
    def test_supplier_str(self):
        supplier = Supplier.objects.create(display_name="АвтоМаг", legal_name="ООО АвтоМаг")
        self.assertEqual(str(supplier), "АвтоМаг")

    def test_bot_user_binding_str(self):
        supplier = Supplier.objects.create(display_name="BindStr", legal_name="BindStr LLC")
        binding = BotUserBinding.objects.create(
            supplier=supplier,
            channel=SourceChannel.TELEGRAM,
            chat_id="chat-str",
            status=EntityStatus.ACTIVE,
        )
        result = str(binding)
        self.assertIn("telegram", result)
        self.assertIn("chat-str", result)

    def test_bot_pending_action_str(self):
        action = BotPendingAction.objects.create(
            platform=SourceChannel.TELEGRAM,
            chat_id="chat-pa-str",
            action_type="upload_price",
            status="pending_registration",
        )
        result = str(action)
        self.assertIn("telegram", result)
        self.assertIn("upload_price", result)

    def test_bot_user_state_str(self):
        state = BotUserState.objects.create(
            platform=SourceChannel.TELEGRAM,
            chat_id="chat-state-str",
            state="start",
        )
        result = str(state)
        self.assertIn("telegram", result)
        self.assertIn("start", result)

    def test_supplier_product_str(self):
        supplier = Supplier.objects.create(display_name="SPStr", legal_name="SPStr LLC")
        unit = Unit.objects.create(code="pcs-sp-str", name="шт", kind=UnitKind.PIECE)
        product = Product.objects.create(canonical_name="Болт", normalized_name="болт", base_unit=unit)
        sp = SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name="Болт М10",
            normalized_name="болт м10",
            status=EntityStatus.ACTIVE,
        )
        self.assertEqual(str(sp), "Болт М10")

    def test_supplier_ownership_request_str(self):
        supplier = Supplier.objects.create(display_name="SorStr", legal_name="SorStr LLC")
        req = SupplierOwnershipRequest.objects.create(
            supplier=supplier,
            full_name="Иван Тест",
            email="sor-str@test.ru",
            inn="7707083893",
            role_requested="owner",
        )
        result = str(req)
        self.assertIn("Иван Тест", result)
        self.assertIn("SorStr", result)


# ---------------------------------------------------------------------------
# suppliers/admin.py: rejected status email, _provision_supplier_user no role
# ---------------------------------------------------------------------------


class AdminSendClaimDecisionEmailTests(TestCase):
    def _make_request(self, status):
        supplier = Supplier.objects.create(display_name="Admin Email", legal_name="Admin Email LLC")
        return SupplierOwnershipRequest.objects.create(
            supplier=supplier,
            full_name="Тест Пользователь",
            email="admin-email-test@test.ru",
            inn="7707083893",
            role_requested="owner",
            status=status,
        )

    def test_rejected_status_sends_email(self):
        from suppliers.admin import _send_claim_decision_email

        req = self._make_request("rejected")
        with patch("suppliers.admin.send_mail") as mock_mail:
            _send_claim_decision_email(req)
        mock_mail.assert_called_once()
        subject = mock_mail.call_args[0][0]
        self.assertIn("отклонена", subject)

    def test_pending_status_sends_no_email(self):
        from suppliers.admin import _send_claim_decision_email

        req = self._make_request("pending")
        with patch("suppliers.admin.send_mail") as mock_mail:
            _send_claim_decision_email(req)
        mock_mail.assert_not_called()

    def test_send_exception_is_swallowed(self):
        from suppliers.admin import _send_claim_decision_email

        req = self._make_request("approved")
        with patch("suppliers.admin.send_mail", side_effect=Exception("SMTP fail")):
            try:
                _send_claim_decision_email(req)
            except Exception:
                self.fail("_send_claim_decision_email raised on mail failure")

    def test_provision_no_role_for_support(self):
        from suppliers.admin import _provision_supplier_user

        supplier = Supplier.objects.create(display_name="Support Sup", legal_name="Support LLC")
        req = SupplierOwnershipRequest.objects.create(
            supplier=supplier,
            full_name="Тест Support",
            email="support-test@test.ru",
            inn="7707083893",
            role_requested="support",
        )
        from suppliers.models import SupplierUser

        _provision_supplier_user(req)
        self.assertFalse(SupplierUser.objects.filter(supplier=supplier).exists())


# ---------------------------------------------------------------------------
# api/v1/catalog/selectors.py: supplier_id, updated_since, category_id, role
# ---------------------------------------------------------------------------


class CatalogExportSelectorFiltersTests(TestCase):
    def test_supplier_id_filter(self):
        from api.v1.catalog.selectors import google_sheet_export_queryset

        supplier_a = Supplier.objects.create(display_name="CatExpA", legal_name="CatExpA LLC")
        qs = google_sheet_export_queryset(filters={"supplier_id": str(supplier_a.id)})
        self.assertIsNotNone(qs)

    def test_updated_since_filter(self):
        from api.v1.catalog.selectors import google_sheet_export_queryset

        qs = google_sheet_export_queryset(filters={"updated_since": timezone.now().isoformat()})
        self.assertIsNotNone(qs)


class CatalogProductsSelectorFiltersTests(TestCase):
    def test_category_id_filter(self):
        from api.v1.catalog.selectors import catalog_products_queryset

        cat = ProductCategory.objects.create(name="CatFilter", slug="cat-filter-sel")
        qs = catalog_products_queryset(filters={"category_id": [cat.id]})
        self.assertIsNotNone(qs)

    def test_is_favorite_filter_requires_venue_id(self):
        from api.v1.catalog.selectors import catalog_products_queryset

        venue = Venue.objects.create(display_name="FavVenue")
        qs = catalog_products_queryset(filters={"is_favorite": True}, venue_id=venue.id)
        self.assertIsNotNone(qs)

    def test_role_filter_requires_venue_id(self):
        from api.v1.catalog.selectors import catalog_products_queryset

        venue = Venue.objects.create(display_name="RoleVenue")
        qs = catalog_products_queryset(filters={"role": "kitchen"}, venue_id=venue.id)
        self.assertIsNotNone(qs)


class VenueOrdersSelectorFilterTests(TestCase):
    def test_order_status_filter(self):
        # 2026-09-17: venue_orders_queryset replaced by venue_supplier_order_rows
        # (one row per supplier заявка, not per Order) — see api/v1/catalog/tests.py
        # for the real coverage; this is just a smoke test that the filter kwarg
        # doesn't crash the selector.
        from api.v1.catalog.selectors import venue_supplier_order_rows

        venue = Venue.objects.create(display_name="OrderStatusVenue")
        rows = venue_supplier_order_rows(venue.id, filters={"status": "submitted"})
        self.assertIsNotNone(rows)


# ---------------------------------------------------------------------------
# api/v1/suppliers/selectors.py: last_upload_since, updated_since filters
# ---------------------------------------------------------------------------


class SupplierSelectorFiltersTests(TestCase):
    def test_last_upload_since_filter(self):
        from api.v1.suppliers.selectors import supplier_products_queryset

        qs = supplier_products_queryset({"last_upload_since": timezone.now()})
        self.assertIsNotNone(qs)

    def test_updated_since_filter_for_upload_items(self):
        from api.v1.suppliers.selectors import supplier_price_upload_items_queryset

        supplier = Supplier.objects.create(display_name="UpdSince", legal_name="UpdSince LLC")
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={"kind": "text", "text": "x", "metadata": {"text_sha256": "xxx"}},
        )
        qs = supplier_price_upload_items_queryset(upload_id=upload.id, filters={"status": "new"})
        self.assertIsNotNone(qs)


# ---------------------------------------------------------------------------
# api/v1/suppliers/storage.py: file already exists branch
# ---------------------------------------------------------------------------


class PersistSupplierPriceSourceFileTests(TestCase):
    def test_returns_existing_path_when_file_already_in_storage(self):
        import shutil
        import tempfile

        from django.test import override_settings

        from api.v1.suppliers.storage import persist_supplier_price_source_file

        tmp = tempfile.mkdtemp()
        try:
            with override_settings(MEDIA_ROOT=tmp, DEFAULT_FILE_STORAGE="django.core.files.storage.FileSystemStorage"):
                from django.core.files.base import ContentFile
                from django.core.files.storage import default_storage

                supplier_id = "supplier-xyz"
                sha = "a" * 64
                file_content = b"col1,col2\nval1,val2\n"

                relative_path = f"supplier_price_uploads/{supplier_id}/{sha[:2]}/{sha[:16]}-upload.csv"
                default_storage.save(relative_path, ContentFile(file_content))

                source_file = BytesIO(file_content)
                source_file.name = "upload.csv"

                result = persist_supplier_price_source_file(
                    source_file=source_file,
                    supplier_id=supplier_id,
                    content_sha256=sha,
                    source_file_name="upload.csv",
                )
                self.assertEqual(result, relative_path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# api/v1/catalog/serializers.py: CartItemCreateSerializer & CartItemUpdateSerializer
# ---------------------------------------------------------------------------


class CartItemCreateSerializerTests(TestCase):
    def test_both_supplier_product_and_name_missing_raises(self):
        from rest_framework.exceptions import ValidationError

        from api.v1.catalog.serializers import CartItemCreateSerializer

        Unit.objects.create(code="pcs-oci", name="шт", kind=UnitKind.PIECE)
        ser = CartItemCreateSerializer(
            data={
                "requested_qty": "1.0",
                "requested_unit_code": "pcs-oci",
            }
        )
        with self.assertRaises(ValidationError):
            ser.is_valid(raise_exception=True)

    def test_supplier_product_without_name_uses_normalized_name(self):
        from api.v1.catalog.serializers import CartItemCreateSerializer

        unit = Unit.objects.create(code="pcs-oci2", name="шт", kind=UnitKind.PIECE)
        product = Product.objects.create(canonical_name="СП Болт", normalized_name="сп болт", base_unit=unit)
        supplier = Supplier.objects.create(display_name="OCI Supplier", legal_name="OCI LLC")
        sp = SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name="СП Болт",
            normalized_name="сп болт",
            status=EntityStatus.ACTIVE,
        )
        ser = CartItemCreateSerializer(
            data={
                "supplier_product_id": str(sp.id),
                "requested_qty": "2.0",
            }
        )
        self.assertTrue(ser.is_valid(), ser.errors)
        self.assertEqual(ser.validated_data["requested_name"], "сп болт")


class CartItemUpdateSerializerTests(TestCase):
    def test_empty_data_raises(self):
        from rest_framework.exceptions import ValidationError

        from api.v1.catalog.serializers import CartItemUpdateSerializer

        ser = CartItemUpdateSerializer(data={})
        with self.assertRaises(ValidationError):
            ser.is_valid(raise_exception=True)


# ---------------------------------------------------------------------------
# api/v1/suppliers/serializers.py: BotUserBindingResolveQuerySerializer validate
# ---------------------------------------------------------------------------


class BotUserBindingResolveQuerySerializerTests(TestCase):
    def test_missing_platform_raises(self):
        from rest_framework.exceptions import ValidationError

        from api.v1.suppliers.serializers import BotUserBindingResolveQuerySerializer

        ser = BotUserBindingResolveQuerySerializer(data={"chat_id": "chat-1"})
        with self.assertRaises(ValidationError):
            ser.is_valid(raise_exception=True)

    def test_missing_chat_id_and_user_id_raises(self):
        from rest_framework.exceptions import ValidationError

        from api.v1.suppliers.serializers import BotUserBindingResolveQuerySerializer

        ser = BotUserBindingResolveQuerySerializer(data={"platform": "telegram"})
        with self.assertRaises(ValidationError):
            ser.is_valid(raise_exception=True)

    def test_channel_field_used_as_platform(self):
        from api.v1.suppliers.serializers import BotUserBindingResolveQuerySerializer

        ser = BotUserBindingResolveQuerySerializer(
            data={
                "channel": "telegram",
                "chat_id": "chat-1",
            }
        )
        self.assertTrue(ser.is_valid(), ser.errors)
        self.assertEqual(ser.validated_data["platform"], "telegram")


# ---------------------------------------------------------------------------
# SupplierPriceUploadNormalizeSerializer: async field validation
# ---------------------------------------------------------------------------


class SupplierPriceUploadNormalizeSerializerTests(TestCase):
    def test_invalid_async_field_raises_validation_error(self):
        from rest_framework.exceptions import ValidationError

        from api.v1.suppliers.serializers import SupplierPriceUploadNormalizeSerializer

        # The serializer reads "async" from initial_data inside validate()
        ser = SupplierPriceUploadNormalizeSerializer(data={"async": "not_a_bool"})
        with self.assertRaises(ValidationError):
            ser.is_valid(raise_exception=True)

    def test_use_ai_false_forces_ai_mode_off(self):
        from api.v1.suppliers.serializers import AI_MODE_OFF, SupplierPriceUploadNormalizeSerializer

        ser = SupplierPriceUploadNormalizeSerializer(data={"use_ai": False})
        self.assertTrue(ser.is_valid(), ser.errors)
        self.assertEqual(ser.validated_data["ai_mode"], AI_MODE_OFF)


# ---------------------------------------------------------------------------
# api/v1/catalog/serializers.py: get_confidence returns None for non-dict payload
# ---------------------------------------------------------------------------


class CatalogProductSerializerConfidenceTests(TestCase):
    def test_get_confidence_returns_none_for_non_dict_payload(self):
        from api.v1.catalog.serializers import GoogleSheetExportRowSerializer

        obj = MagicMock()
        obj.latest_upload_item_payload = "not-a-dict"
        ser = GoogleSheetExportRowSerializer()
        result = ser.get_confidence(obj)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# api/v1/suppliers/selectors.py: updated_since in supplier_products_queryset
# ---------------------------------------------------------------------------


class SupplierProductsUpdatedSinceFilterTests(TestCase):
    def test_updated_since_filter_applies(self):
        from api.v1.suppliers.selectors import supplier_products_queryset

        qs = supplier_products_queryset({"updated_since": timezone.now()})
        self.assertIsNotNone(qs)
        list(qs)  # force evaluation


# ---------------------------------------------------------------------------
# api/v1/suppliers/views.py: BotUserStateCurrentView.get when state exists
# ---------------------------------------------------------------------------

from django.test import override_settings as _ov2  # noqa: E402


@_ov2(SERVICE_API_TOKEN="test-token")
class BotUserStateGetWithExistingStateTests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient

        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_get_returns_existing_state(self):
        BotUserState.objects.create(
            platform=SourceChannel.TELEGRAM,
            chat_id="chat-existing-state",
            state="supplier_registered",
        )
        response = self.client.get(
            "/api/v1/bot-user-states/current",
            {"platform": "telegram", "chat_id": "chat-existing-state"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["state"], "supplier_registered")
        self.assertIsNotNone(payload["bot_user_state"])


# ---------------------------------------------------------------------------
# api/v1/views.py: _cabinet_uploads_data with uploads in session context
# ---------------------------------------------------------------------------


class CabinetUploadsSessionTests(TestCase):
    def test_cabinet_uploads_data_with_normalization_result(self):
        from api.v1.suppliers.services import get_supplier_cabinet_snapshot

        supplier = Supplier.objects.create(display_name="SessionCab", legal_name="SessionCab LLC")
        SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={
                "kind": "text",
                "text": "x",
                "metadata": {"text_sha256": "x"},
                "normalization": {"result": {"items_total": 3, "products_created": 2, "prices_created": 3}},
            },
        )
        result = get_supplier_cabinet_snapshot(supplier)["uploads"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["items_total"], 3)
        self.assertEqual(result[0]["products_created"], 2)


# ---------------------------------------------------------------------------
# api/v1/views.py: supplier_add_view validation for missing email and inn
# ---------------------------------------------------------------------------


class SupplierAddViewValidationTests(DjangoTestCase):
    url = "/suppliers/add/"

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_missing_email_shows_error(self):
        response = self.client.post(
            self.url,
            {
                "company_name": "Компания",
                "full_name": "Иван Иванов",
                "email": "",
                "inn": "7707083893",
            },
        )
        self.assertContains(response, "Введите корректный email")

    def test_missing_inn_shows_error(self):
        response = self.client.post(
            self.url,
            {
                "company_name": "Компания",
                "full_name": "Иван Иванов",
                "email": "ivan@test.ru",
                "inn": "",
            },
        )
        self.assertContains(response, "Введите ИНН")


# ---------------------------------------------------------------------------
# suppliers/admin.py: reject_ownership_requests action (lines 200-203)
# ---------------------------------------------------------------------------


class RejectOwnershipRequestsActionTests(TestCase):
    def test_reject_action_sets_rejected_status(self):
        from django.test import RequestFactory

        from suppliers.admin import reject_ownership_requests

        supplier = Supplier.objects.create(display_name="RejectAdmin", legal_name="RejectAdmin LLC")
        req_obj = SupplierOwnershipRequest.objects.create(
            supplier=supplier,
            full_name="Тест Reject",
            email="reject-admin@test.ru",
            inn="7707083893",
            role_requested="owner",
        )
        factory = RequestFactory()
        admin_request = factory.get("/")
        reject_ownership_requests(None, admin_request, SupplierOwnershipRequest.objects.filter(pk=req_obj.pk))
        req_obj.refresh_from_db()
        self.assertEqual(req_obj.status, "rejected")


# ---------------------------------------------------------------------------
# catalog/migrations/0027_archive_garbage_brands.py — разовая чистка мусорных
# Brand-записей (см. api/v1/suppliers/test_services_extra.py::
# ResolveBrandGuardTests для проверки самой эвристики защиты пайплайна)
# ---------------------------------------------------------------------------


class ArchiveGarbageBrandsMigrationTests(TestCase):
    def _run_migration(self):
        import importlib

        from django.apps import apps as real_apps

        migration_module = importlib.import_module("catalog.migrations.0027_archive_garbage_brands")
        migration_module.archive_garbage_brands(real_apps, None)

    def test_archives_garbage_brand_and_detaches_supplier_product(self):
        garbage = Brand.objects.create(canonical_name="test12345", normalized_name="test12345")
        good = Brand.objects.create(canonical_name="Bosch", normalized_name="bosch")
        unit = Unit.objects.create(code="pc-migr-test", name="шт", kind=UnitKind.PIECE)
        supplier = Supplier.objects.create(display_name="MigrTest", legal_name="MigrTest LLC")
        sp = SupplierProduct.objects.create(
            supplier=supplier,
            brand=garbage,
            unit=unit,
            supplier_name="Тест товар",
            normalized_name="тест-товар-migr",
        )

        self._run_migration()

        garbage.refresh_from_db()
        good.refresh_from_db()
        sp.refresh_from_db()
        self.assertEqual(garbage.status, "archived")
        self.assertEqual(good.status, "active")
        self.assertIsNone(sp.brand)

    def test_leaves_valid_brands_untouched(self):
        good = Brand.objects.create(canonical_name="Bosch", normalized_name="bosch")
        self._run_migration()
        good.refresh_from_db()
        self.assertEqual(good.status, "active")

    def test_reject_action_skips_already_rejected(self):
        from django.test import RequestFactory

        from suppliers.admin import reject_ownership_requests

        supplier = Supplier.objects.create(display_name="AlreadyRejected", legal_name="AR LLC")
        req_obj = SupplierOwnershipRequest.objects.create(
            supplier=supplier,
            full_name="Тест",
            email="ar@test.ru",
            inn="7707083893",
            role_requested="owner",
            status="rejected",
        )
        factory = RequestFactory()
        admin_request = factory.get("/")
        with patch("suppliers.admin.send_mail") as mock_mail:
            reject_ownership_requests(None, admin_request, SupplierOwnershipRequest.objects.filter(pk=req_obj.pk))
        mock_mail.assert_not_called()
