import shutil
import tempfile
import threading
import uuid
from datetime import date, timedelta
from datetime import datetime as dt
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, connection, connections, transaction
from django.db.models.deletion import RestrictedError
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from openpyxl import Workbook
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase, APITransactionTestCase

from api.v1.catalog.candidate_ranking_llm import CandidateRankingError
from api.v1.catalog.search import (
    build_primary_name_candidate_filter,
    filter_catalog_product_candidates,
    fuzzy_catalog_product_candidates,
    parse_search_query,
)
from api.v1.catalog.selectors import (
    find_candidate_master_products,
    individual_price_order_history_queryset,
    master_product_bulk_supplier_product_diff,
    master_product_detail_queryset,
    master_product_tender_offer_diff,
    master_product_venue_batch_current,
    master_product_venue_breakdown,
    master_products_queryset,
    resolve_master_product_import_rows,
    resolve_master_product_matches_for_import,
    resolve_supplier_product_price,
    resolve_venue_import_rows,
    supplier_product_venue_price_confirmed_qty,
    supplier_product_venue_price_history_queryset,
    venue_product_lists_queryset,
)
from api.v1.catalog.serializers import (
    MasterProductImportUploadStatusResponseSerializer,
    VenueProductMatrixItemSerializer,
)
from api.v1.catalog.services import (
    MasterProductVenueLinkConflict,
    SupplierProductVenuePriceConflict,
    apply_master_product_import,
    apply_master_product_import_matches,
    apply_master_product_venue_batch,
    apply_tender_offer,
    apply_venue_import,
    archive_supplier_product_venue_price,
    archive_venue_product_list,
    bulk_assign_supplier_product_to_master_product,
    create_master_product,
    create_product_matrix_item,
    create_venue_product_list,
    delete_future_supplier_product_venue_price,
    delete_master_product,
    delete_product_matrix_item,
    link_master_product_to_venue,
    master_product_bulk_supplier_product_targets,
    master_product_venue_batch_preview,
    rename_venue_product_list,
    set_supplier_product_venue_price,
    unlink_master_product_from_venue,
    update_master_product,
    update_supplier_product_venue_price,
)
from api.v1.catalog.unit_llm_client import UnitSuggestionError
from api.v1.catalog.vision_client import PhotoSearchError
from api.v1.permissions import (
    HasMasterCatalogAccess,
    HasMasterCatalogAccessToOwner,
    HasSupplierProductVenuePriceAccess,
    HasSupplierProductVenuePriceObjectAccess,
)
from api.v1.suppliers.services import create_supplier_price_upload_from_text, normalize_supplier_price_upload
from catalog.models import (
    Brand,
    CatalogImportDomain,
    CatalogImportItem,
    CatalogImportItemAction,
    CatalogImportSession,
    CatalogImportSessionStatus,
    GoogleSheetSyncLog,
    MasterProduct,
    MasterProductImportUpload,
    MasterProductImportUploadStatus,
    ProductCategory,
    SupplierProductVenuePrice,
    SupplierProductVenuePriceSourceType,
    SupplierProductVenuePriceStatus,
    Tender,
    TenderAccess,
    TenderItem,
    TenderOffer,
    TenderStatus,
    VenueProductImportAlias,
    VenueProductList,
    VenueProductMatrixItem,
    VenueProductMatrixItemSource,
    VenueSupplierProduct,
    Wishlist,
    WishlistItem,
)
from core.models import (
    AuditActorType,
    AuditLog,
    AuditLogAction,
    AuditLogEntityType,
    EntityStatus,
    OffSystemSupplier,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    SupplierOrderRequest,
    Unit,
    UnitKind,
    User,
    UserRole,
    Venue,
    VenueAddress,
    VenueCart,
    VenueCartItem,
    VenueSubdivision,
    VenueUser,
    VenueUserRole,
)
from file_imports.models import ImportFile
from suppliers.models import (
    PriceStatus,
    ProductAvailability,
    SourceChannel,
    Supplier,
    SupplierAddress,
    SupplierAddressType,
    SupplierContact,
    SupplierContactType,
    SupplierProduct,
    SupplierProductImage,
    SupplierProductPrice,
    SupplierUser,
    SupplierUserRole,
    UploadStatus,
)

_TODAY = timezone.localdate()
_TEST_DELIVERY_DATE = min(_TODAY + timedelta(days=1), date(_TODAY.year, 12, 31)).isoformat()


def _grouped_order_items(data):
    return [item for group in data["supplier_groups"] for item in group["items"]]


def _jwt_bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    return f"Bearer {RefreshToken.for_user(user).access_token}"


def _fake_embedding(text):
    """Deterministic bag-of-characters embedding for tests — avoids loading
    the real sentence-transformers model (slow, and every existing test that
    exercises embeddings mocks the boundary instead, e.g.
    api/v1/catalog/tests.py's @patch("api.v1.catalog.search.vector_supplier_product_scores", ...)).
    Cosine similarity between two of these vectors tracks character overlap:
    identical strings score ~1.0, strings with zero shared characters score
    exactly 0.0 — good enough to test ranking/threshold logic without a real
    model."""
    vector = [0.0] * 384
    for char in text.casefold():
        vector[ord(char) % 384] += 1.0
    norm = sum(component * component for component in vector) ** 0.5 or 1.0
    return [component / norm for component in vector]


def _fake_embeddings(texts):
    return [_fake_embedding(text) for text in texts]


def _xlsx_upload(rows, name="catalog.xlsx"):
    workbook = Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile(
        name,
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _make_venue_user(email="venue-user@example.com"):
    return User.objects.create(
        email=email, full_name="Venue User", role=UserRole.VENUE_USER, status=EntityStatus.ACTIVE
    )


def _make_venue_owner(venue, email="venue-owner@example.com"):
    """2026-09-08: cart routes stopped accepting the shared service token
    (a cart belongs to one core.User account now, a service token has no
    account to own) — tests that used to drive the cart with
    `Bearer test-token` need a real owner/manager VenueUser membership and a
    JWT instead."""
    user = User.objects.create(
        email=email, full_name="Venue Owner", role=UserRole.VENUE_USER, status=EntityStatus.ACTIVE
    )
    VenueUser.objects.create(venue=venue, user=user, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
    return user


@override_settings(SERVICE_API_TOKEN="test-token")
class GoogleSheetExportAPITests(APITestCase):
    url = "/api/v1/catalog/export/google-sheet"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_rejects_request_without_service_token(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 403)

    def test_export_sees_demo_products_after_seed(self):
        call_command("seed_demo_data", verbosity=0)
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["sheet"], "Товары от поставщиков")
        self.assertEqual(len(payload["rows"]), 3)
        names = {row["supplier_normalized_name"] for row in payload["rows"]}
        self.assertEqual(names, {"demo-flour", "demo-sunflower-oil", "demo-sugar"})
        self.assertTrue(all(row["supplier_product_id"] for row in payload["rows"]))
        self.assertTrue(all(row["supplier_id"] for row in payload["rows"]))
        self.assertTrue(all(row["price"] for row in payload["rows"]))
        self.assertTrue(all(row["currency"] == "RUB" for row in payload["rows"]))
        self.assertTrue(all(row["supplier_name"] == "Демо Поставщик" for row in payload["rows"]))

    def test_export_excludes_archived_supplier_products(self):
        call_command("seed_demo_data", verbosity=0)
        archived_product = SupplierProduct.objects.filter(supplier_sku="DEMO-FLOUR-25").get()
        archived_product.status = EntityStatus.ARCHIVED
        archived_product.save(update_fields=["status", "updated_at"])
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        exported_ids = {row["supplier_product_id"] for row in response.json()["rows"]}
        self.assertNotIn(str(archived_product.id), exported_ids)

    def test_export_filters_by_supplier_id(self):
        call_command("seed_demo_data", verbosity=0)
        supplier = Supplier.objects.get(display_name="Демо Поставщик")
        other = Supplier.objects.create(legal_name="Other LLC", display_name="Other")
        _make_supplier_product(supplier=other, price="99.00")
        self.authenticate()
        response = self.client.get(self.url, {"supplier_id": str(supplier.id)})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(row["supplier_id"] == str(supplier.id) for row in response.json()["rows"]))

    def test_export_filters_by_updated_since(self):
        call_command("seed_demo_data", verbosity=0)
        self.authenticate()
        response = self.client.get(self.url, {"updated_since": "2099-01-01T00:00:00Z"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["rows"]), 0)


@override_settings(SERVICE_API_TOKEN="test-token")
class GoogleSheetSyncLogAPITests(APITestCase):
    url = "/api/v1/catalog/google-sheet-syncs"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_rejects_request_without_service_token(self):
        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_creates_google_sheet_sync_log(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        self.authenticate()

        response = self.client.post(
            self.url,
            {
                "spreadsheet_id": "sheet-123",
                "sheet_name": "Товары от поставщиков",
                "status": "success",
                "trigger": "upload",
                "supplier_id": str(supplier.id),
                "n8n_execution_id": "n8n-123",
                "rows_read": 3,
                "rows_written": 3,
                "raw_payload": {"workflow": "Supplier Price Upload MVP"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        sync_log = GoogleSheetSyncLog.objects.get()
        self.assertEqual(sync_log.spreadsheet_id, "sheet-123")
        self.assertEqual(sync_log.supplier, supplier)
        self.assertEqual(sync_log.rows_written, 3)
        self.assertEqual(response.json()["status"], "success")


def _make_supplier_product(supplier=None, price=None, brand=None, category=None, normalized_name="test-product"):
    if supplier is None:
        supplier = Supplier.objects.create(legal_name="Test LLC", display_name="Test Supplier")
    unit = Unit.objects.filter(code="шт").first() or Unit.objects.create(code="шт", name="штука", kind="piece")
    sp = SupplierProduct.objects.create(
        supplier=supplier,
        supplier_name="Тестовый товар",
        normalized_name=normalized_name,
        unit=unit,
        brand=brand,
        category=category,
        status=EntityStatus.ACTIVE,
    )
    if price is not None:
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=price,
            currency="RUB",
            status=PriceStatus.ACTIVE,
        )
    return sp


@override_settings(
    CATALOG_QDRANT_AUTO_SYNC_ENABLED=True,
    QDRANT_URL="https://qdrant.example",
    QDRANT_API_KEY="secret",
)
class SupplierUploadToSearchE2ETests(APITestCase):
    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    @patch("api.v1.suppliers.tasks.sync_supplier_catalog_to_qdrant_task.delay")
    def test_new_supplier_text_upload_reaches_catalog_search_and_qdrant_queue(self, qdrant_delay, _vector_scores):
        qdrant_delay.return_value.id = "qdrant-sync-task"
        supplier = Supplier.objects.create(legal_name="E2E Supplier LLC", display_name="E2E Supplier")
        Unit.objects.get_or_create(code="шт", defaults={"name": "штука", "kind": "piece"})
        upload = create_supplier_price_upload_from_text(
            data={
                "supplier": supplier,
                "source_channel": SourceChannel.TELEGRAM,
                "text": "Артишок тестовый 777 руб шт",
            }
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = normalize_supplier_price_upload(upload_id=upload.id, use_ai=False)

        self.assertEqual(result["status"], UploadStatus.APPLIED)
        self.assertEqual(result["products_created"], 1)
        self.assertEqual(result["catalog_sync"], {"scheduled": True})
        product = SupplierProduct.objects.get(supplier=supplier)
        qdrant_delay.assert_called_once_with(supplier_id=str(supplier.id), upload_id=str(upload.id))

        response = self.client.get("/api/v1/catalog/products", {"q": "артишок тестовый"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(product.id))

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    @patch(
        "api.v1.suppliers.tasks.sync_supplier_catalog_to_qdrant_task.delay",
        side_effect=ConnectionError("broker unavailable"),
    )
    def test_catalog_remains_available_when_qdrant_enqueue_fails(self, qdrant_delay, _vector_scores):
        supplier = Supplier.objects.create(legal_name="Resilient Supplier LLC", display_name="Resilient Supplier")
        Unit.objects.get_or_create(
            code="\u0448\u0442", defaults={"name": "\u0448\u0442\u0443\u043a\u0430", "kind": "piece"}
        )
        upload = create_supplier_price_upload_from_text(
            data={
                "supplier": supplier,
                "source_channel": SourceChannel.TELEGRAM,
                "text": "\u0410\u0440\u0442\u0438\u0448\u043e\u043a \u0442\u0435\u0441\u0442\u043e\u0432\u044b\u0439 555 \u0440\u0443\u0431 \u0448\u0442",
            }
        )

        with self.assertLogs("autosnab.suppliers.services", level="ERROR") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                result = normalize_supplier_price_upload(upload_id=upload.id, use_ai=False)

        self.assertIn(result["status"], {UploadStatus.APPLIED, UploadStatus.NEEDS_REVIEW})
        self.assertEqual(result["products_created"], 1)
        qdrant_delay.assert_called_once_with(supplier_id=str(supplier.id), upload_id=str(upload.id))
        self.assertTrue(any("qdrant_sync.enqueue_failed" in message for message in logs.output))

        product = SupplierProduct.objects.get(supplier=supplier)
        response = self.client.get(
            "/api/v1/catalog/products",
            {"q": "\u0430\u0440\u0442\u0438\u0448\u043e\u043a \u0442\u0435\u0441\u0442\u043e\u0432\u044b\u0439"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(product.id))

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    @patch("api.v1.suppliers.tasks.sync_supplier_catalog_to_qdrant_task.delay")
    def test_failed_qdrant_sync_can_be_retried_after_upload_commit(self, qdrant_delay, _vector_scores):
        from celery.exceptions import Retry

        from api.v1.catalog import embeddings
        from api.v1.suppliers import tasks as supplier_tasks

        qdrant_delay.return_value.id = "qdrant-sync-task"
        supplier = Supplier.objects.create(legal_name="Recovery Supplier LLC", display_name="Recovery Supplier")
        Unit.objects.get_or_create(
            code="\u0448\u0442", defaults={"name": "\u0448\u0442\u0443\u043a\u0430", "kind": "piece"}
        )
        upload = create_supplier_price_upload_from_text(
            data={
                "supplier": supplier,
                "source_channel": SourceChannel.TELEGRAM,
                "text": "\u0410\u0440\u0442\u0438\u0448\u043e\u043a \u0442\u0435\u0441\u0442\u043e\u0432\u044b\u0439 444 \u0440\u0443\u0431 \u0448\u0442",
            }
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = normalize_supplier_price_upload(upload_id=upload.id, use_ai=False)

        self.assertIn(result["status"], {UploadStatus.APPLIED, UploadStatus.NEEDS_REVIEW})
        product = SupplierProduct.objects.get(supplier=supplier)

        with (
            patch.object(supplier_tasks, "close_old_connections"),
            patch.object(embeddings, "ensure_product_for_supplier_product"),
            patch.object(embeddings, "sync_supplier_products_to_qdrant", side_effect=ConnectionError("Qdrant down")),
        ):
            with self.assertRaises(Retry):
                supplier_tasks.sync_supplier_catalog_to_qdrant_task.apply(
                    kwargs={"supplier_id": str(supplier.id), "upload_id": str(upload.id)},
                    throw=False,
                )

        self.assertTrue(SupplierProduct.objects.filter(pk=product.id).exists())
        response = self.client.get(
            "/api/v1/catalog/products",
            {"q": "\u0430\u0440\u0442\u0438\u0448\u043e\u043a \u0442\u0435\u0441\u0442\u043e\u0432\u044b\u0439"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

        with (
            patch.object(supplier_tasks, "close_old_connections"),
            patch.object(embeddings, "ensure_product_for_supplier_product") as ensure_product,
            patch.object(embeddings, "sync_supplier_products_to_qdrant", return_value=(1, 0)) as sync,
        ):
            recovered_sync = supplier_tasks.sync_supplier_catalog_to_qdrant_task.apply(
                kwargs={"supplier_id": str(supplier.id), "upload_id": str(upload.id)},
                throw=False,
            )

        self.assertFalse(recovered_sync.failed())
        self.assertEqual(recovered_sync.result["processed"], 1)
        synced_items = sync.call_args.args[0]
        self.assertEqual([item.id for item in synced_items], [product.id])
        ensure_product.assert_called_once_with(synced_items[0])


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogProductListAPITests(APITestCase):
    url = "/api/v1/catalog/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_allows_anonymous_access(self):
        # Публичный просмотр каталога — покупателю не нужен вход, чтобы
        # смотреть товары (только для добавления в корзину/заказа).
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_returns_empty_list_when_no_products(self):
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)
        self.assertEqual(response.json()["results"], [])

    def test_returns_active_products_with_price(self):
        _make_supplier_product(price="150.00")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)
        row = data["results"][0]
        self.assertEqual(row["current_price"], "150.00")
        self.assertEqual(row["normalized_name"], "test-product")

    def test_excludes_products_without_price(self):
        _make_supplier_product(price=None)
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json()["count"], 0)

    def test_excludes_products_from_archived_supplier(self):
        """2026-07-29: найдено при реализации self-service удаления компании —
        раньше товары archived-поставщика продолжали быть видны в каталоге."""
        supplier = Supplier.objects.create(
            legal_name="Удалённый LLC", display_name="Удалённый", status=EntityStatus.ARCHIVED
        )
        _make_supplier_product(supplier=supplier, price="100.00")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json()["count"], 0)

    def test_excludes_products_from_unpublished_supplier(self):
        supplier = Supplier.objects.create(
            legal_name="Снят LLC", display_name="Снят", status=EntityStatus.ACTIVE, is_published=False
        )
        _make_supplier_product(supplier=supplier, price="100.00")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json()["count"], 0)

    def test_filters_by_supplier_id(self):
        sp1 = _make_supplier_product(price="100.00")
        _make_supplier_product(price="200.00")
        self.authenticate()
        response = self.client.get(self.url, {"supplier_id": str(sp1.supplier_id)})
        self.assertEqual(response.json()["count"], 1)

    def test_searches_by_name(self):
        sp = _make_supplier_product(price="100.00")
        sp.normalized_name = "особый-товар"
        sp.save(update_fields=["normalized_name", "updated_at"])
        _make_supplier_product(price="100.00")
        self.authenticate()
        response = self.client.get(self.url, {"q": "особый"})
        self.assertEqual(response.json()["count"], 1)

    def test_search_uses_pilot_aliases(self):
        _make_supplier_product(price="100.00", normalized_name="вишня замороженная")
        _make_supplier_product(price="100.00", normalized_name="яблоко зеленое")
        self.authenticate()
        response = self.client.get(self.url, {"q": "вишенки"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["normalized_name"], "вишня замороженная")

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_search_tolerates_single_letter_typo(self, _vector_scores):
        expected = _make_supplier_product(price="100.00", normalized_name="масло фритюрное")
        _make_supplier_product(price="100.00", normalized_name="масло сливочное")
        self.authenticate()

        response = self.client.get(self.url, {"q": "фретюрное"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_search_tolerates_missing_letter(self, _vector_scores):
        expected = _make_supplier_product(price="100.00", normalized_name="клубника свежая")
        _make_supplier_product(price="100.00", normalized_name="черника свежая")
        self.authenticate()

        response = self.client.get(self.url, {"q": "клубнка"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_search_falls_back_to_bounded_scan_when_trigram_has_no_valid_candidates(self, _vector_scores):
        expected = _make_supplier_product(price="100.00", normalized_name="клубника свежая")
        _make_supplier_product(price="100.00", normalized_name="черника свежая")
        self.authenticate()

        with patch(
            "api.v1.catalog.search.fuzzy_catalog_product_candidates",
            return_value=SupplierProduct.objects.none(),
        ):
            response = self.client.get(self.url, {"q": "клубнка"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_search_tolerates_transposed_letters(self, _vector_scores):
        expected = _make_supplier_product(price="100.00", normalized_name="макароны спагетти")
        _make_supplier_product(price="100.00", normalized_name="гречневая крупа")
        self.authenticate()

        response = self.client.get(self.url, {"q": "макарноы"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_search_handles_known_short_word_typo_without_generic_short_fuzzy(self, _vector_scores):
        expected = _make_supplier_product(price="100.00", normalized_name="кофе зерновой")
        _make_supplier_product(price="100.00", normalized_name="чай черный")
        self.authenticate()

        response = self.client.get(self.url, {"q": "кофэ"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_search_typo_does_not_turn_nonsense_into_results(self, _vector_scores):
        _make_supplier_product(price="100.00", normalized_name="масло фритюрное")
        _make_supplier_product(price="100.00", normalized_name="клубника свежая")
        self.authenticate()

        response = self.client.get(self.url, {"q": "абракадабра"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_sorted_search_keeps_typo_tolerance(self, _vector_scores):
        expected = _make_supplier_product(price="100.00", normalized_name="клубника свежая")
        _make_supplier_product(price="90.00", normalized_name="черника свежая")
        self.authenticate()

        response = self.client.get(self.url, {"q": "клубнка", "sort": "price_asc"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))

    def test_search_uses_semantic_category_terms(self):
        group = ProductCategory.objects.create(name="Овощи", slug="vegetables")
        category = ProductCategory.objects.create(name="Грибы", slug="mushrooms", parent=group)
        _make_supplier_product(price="100.00", normalized_name="вешенки свежие", category=category)
        _make_supplier_product(price="100.00", normalized_name="морковь свежая", category=group)
        self.authenticate()
        response = self.client.get(self.url, {"q": "грибы"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["normalized_name"], "вешенки свежие")

    def test_search_does_not_match_short_terms_inside_words(self):
        _make_supplier_product(price="100.00", normalized_name="ром темный")
        _make_supplier_product(price="100.00", normalized_name="ароматизатор ромовый")
        self.authenticate()
        response = self.client.get(self.url, {"q": "ром"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["normalized_name"], "ром темный")

    def test_search_extracts_price_cap_from_query(self):
        _make_supplier_product(price="90.00", normalized_name="кофе зерновой")
        _make_supplier_product(price="150.00", normalized_name="кофе молотый")
        self.authenticate()
        response = self.client.get(self.url, {"q": "кофе до 100"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["normalized_name"], "кофе зерновой")

    @patch("api.v1.catalog.search.vector_supplier_product_scores")
    def test_search_includes_vector_candidates(self, vector_scores):
        vector_match = _make_supplier_product(price="100.00", normalized_name="семантически близкий товар")
        _make_supplier_product(price="100.00", normalized_name="обычный товар")
        vector_scores.return_value = {str(vector_match.id): 0.91}
        self.authenticate()

        response = self.client.get(self.url, {"q": "сложный смысловой запрос"})

        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], str(vector_match.id))
        vector_scores.assert_called_once()

    @patch("api.v1.catalog.search.vector_supplier_product_scores")
    def test_exact_multiword_search_does_not_wait_for_qdrant(self, vector_scores):
        expected = _make_supplier_product(
            price="100.00",
            normalized_name="молоко ультрапастеризованное",
        )
        self.authenticate()

        response = self.client.get(self.url, {"q": "молоко ультрапастеризованное"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))
        vector_scores.assert_not_called()

    @patch("api.v1.catalog.search.vector_supplier_product_scores")
    def test_exact_single_word_search_does_not_wait_for_qdrant(self, vector_scores):
        expected = _make_supplier_product(price="100.00", normalized_name="молоко")
        self.authenticate()

        response = self.client.get(self.url, {"q": "молоко"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))
        vector_scores.assert_not_called()

    @patch("api.v1.catalog.search.filter_catalog_product_candidates")
    @patch("api.v1.catalog.search.vector_supplier_product_scores")
    def test_exact_name_search_uses_indexed_fast_path(self, vector_scores, broad_candidates):
        expected = _make_supplier_product(price="100.00", normalized_name="молоко пастеризованное")
        self.authenticate()

        response = self.client.get(self.url, {"q": "молоко"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))
        vector_scores.assert_not_called()
        broad_candidates.assert_not_called()

    def test_indexed_name_fast_path_does_not_mix_unindexed_sku_fields(self):
        query = parse_search_query("молоко")

        predicate = build_primary_name_candidate_filter(query)
        where = str(SupplierProduct.objects.filter(predicate).query.where)

        self.assertIn("normalized_name", where)
        self.assertNotIn("supplier_sku", where)
        self.assertNotIn("system_sku", where)

    @patch(
        "api.v1.catalog.search.fuzzy_catalog_product_candidates",
        wraps=fuzzy_catalog_product_candidates,
    )
    @patch("api.v1.catalog.search.vector_supplier_product_scores")
    def test_keyboard_layout_exact_match_does_not_start_fuzzy_fallback(
        self,
        vector_scores,
        fuzzy_candidates,
    ):
        expected = _make_supplier_product(price="100.00", normalized_name="мука пшеничная")
        self.authenticate()

        response = self.client.get(self.url, {"q": "verf"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))
        fuzzy_candidates.assert_not_called()
        vector_scores.assert_not_called()

    @patch("api.v1.catalog.search.filter_catalog_product_candidates", wraps=filter_catalog_product_candidates)
    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_non_name_search_falls_back_to_broad_fields(self, _vector_scores, broad_candidates):
        category = ProductCategory.objects.create(name="Молочная продукция", slug="dairy-fast-path-test")
        expected = _make_supplier_product(price="100.00", normalized_name="йогурт", category=category)
        self.authenticate()

        response = self.client.get(self.url, {"q": "молочная"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(expected.id))
        broad_candidates.assert_called_once()

    @override_settings(CATALOG_VECTOR_SEARCH_TOP_K=17)
    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_vector_search_uses_top_k_instead_of_lexical_candidate_limit(self, vector_scores):
        self.authenticate()

        response = self.client.get(self.url, {"q": "сложный смысловой запрос"})

        self.assertEqual(response.status_code, 200)
        vector_scores.assert_called_once_with("сложный смысловой запрос", limit=17)

    def test_is_favorite_null_without_venue_id(self):
        _make_supplier_product(price="100.00")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertIsNone(response.json()["results"][0]["is_favorite"])

    def test_is_favorite_annotated_with_venue_id(self):
        sp = _make_supplier_product(price="100.00")
        venue = Venue.objects.create(display_name="Test Cafe")
        VenueSupplierProduct.objects.create(
            venue=venue, supplier_product=sp, is_favorite=True, status=EntityStatus.ACTIVE
        )
        self.authenticate()
        response = self.client.get(self.url, {"venue_id": str(venue.id)})
        self.assertTrue(response.json()["results"][0]["is_favorite"])

    def test_pagination_limit_offset(self):
        for i in range(5):
            _make_supplier_product(price=f"{100 + i}.00")
        self.authenticate()
        response = self.client.get(self.url, {"limit": 2, "offset": 0})
        data = response.json()
        self.assertEqual(data["count"], 5)
        self.assertEqual(len(data["results"]), 2)

    def test_is_favorite_filter_returns_only_favorites(self):
        sp_fav = _make_supplier_product(price="100.00", normalized_name="fav-filter")
        _make_supplier_product(price="200.00", normalized_name="not-fav-filter")
        venue = Venue.objects.create(display_name="Filter Cafe")
        VenueSupplierProduct.objects.create(
            venue=venue, supplier_product=sp_fav, is_favorite=True, status=EntityStatus.ACTIVE
        )
        self.authenticate()
        response = self.client.get(self.url, {"is_favorite": "true", "venue_id": str(venue.id)})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], str(sp_fav.id))

    def test_wishlist_id_filter_returns_only_items_in_wishlist(self):
        sp_in_wishlist = _make_supplier_product(price="100.00", normalized_name="wishlist-filter-in")
        _make_supplier_product(price="200.00", normalized_name="wishlist-filter-out")
        user = _make_venue_user()
        wishlist = Wishlist.objects.create(user=user, name="Для бара")
        WishlistItem.objects.create(wishlist=wishlist, supplier_product=sp_in_wishlist)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.url, {"wishlist_id": str(wishlist.id)})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], str(sp_in_wishlist.id))

    def test_wishlist_id_filter_ignored_without_authentication(self):
        sp = _make_supplier_product(price="100.00")
        self.authenticate()
        response = self.client.get(self.url, {"wishlist_id": "00000000-0000-0000-0000-000000000000"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], str(sp.id))

    def test_wishlist_id_filter_scoped_to_requesting_user(self):
        sp = _make_supplier_product(price="100.00")
        owner = _make_venue_user(email="owner@example.com")
        other = _make_venue_user(email="other@example.com")
        wishlist = Wishlist.objects.create(user=owner, name="Для бара")
        WishlistItem.objects.create(wishlist=wishlist, supplier_product=sp)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other))
        response = self.client.get(self.url, {"wishlist_id": str(wishlist.id)})
        self.assertEqual(response.json()["count"], 0)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueFavoriteAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.sp = _make_supplier_product(price="100.00")
        self.list_url = f"/api/v1/catalog/venues/{self.venue.id}/favorites"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_rejects_without_service_token(self):
        response = self.client.get(self.list_url)
        # 401, not 403: the view also registers a JWT authenticator, so DRF's
        # permission_denied() reports "unauthenticated" rather than "forbidden"
        # for a request with no credentials at all.
        self.assertEqual(response.status_code, 401)

    def test_venue_user_can_access_own_venue(self):
        user = User.objects.create(
            email="own-venue@example.com", full_name="Owner", role=UserRole.VENUE_USER, venue=self.venue
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

    def test_venue_user_rejected_for_other_venue(self):
        # IDOR закрыт 2026-07-14 — раньше любой авторизованный core.User мог
        # подставить чужой venue_id и получить доступ к его избранному/заказам.
        other_venue = Venue.objects.create(display_name="Other Cafe")
        user = User.objects.create(
            email="wrong-venue@example.com", full_name="Wrong", role=UserRole.VENUE_USER, venue=other_venue
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 403)

    def test_venue_user_without_assigned_venue_rejected(self):
        user = User.objects.create(email="no-venue@example.com", full_name="No Venue", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 403)

    def test_venue_user_can_access_via_venueuser_membership(self):
        # 2026-09-02: self-service (POST /venues/mine) and invite-link/email
        # invite joins never set the legacy core.User.venue FK — only
        # Django admin does — so before this fix every self-service/invited
        # venue account got 403 here despite owning/managing the venue via
        # an active VenueUser row. Mirrors _user_manages_venue (Plan 1/2).
        user = User.objects.create(email="venueuser-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        VenueUser.objects.create(venue=self.venue, user=user, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

    def test_venue_user_rejected_for_inactive_venueuser_membership(self):
        user = User.objects.create(
            email="venueuser-inactive@example.com", full_name="Ex-Owner", role=UserRole.VENUE_USER
        )
        VenueUser.objects.create(venue=self.venue, user=user, role=VenueUserRole.OWNER, status=EntityStatus.ARCHIVED)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 403)

    def test_admin_can_access_any_venue(self):
        staff = User.objects.create(email="staff-any-venue@example.com", full_name="Staff", role=UserRole.ADMIN)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(staff))
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

    def test_supplier_role_rejected(self):
        supplier_user = User.objects.create(email="supplier-role@example.com", full_name="Supplier", role="supplier")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(supplier_user))
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 403)

    def test_list_favorites_empty(self):
        self.authenticate()
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_add_favorite(self):
        self.authenticate()
        response = self.client.post(self.list_url, {"supplier_product_id": str(self.sp.id)}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            VenueSupplierProduct.objects.filter(venue=self.venue, supplier_product=self.sp, is_favorite=True).exists()
        )

    def test_add_favorite_idempotent(self):
        self.authenticate()
        self.client.post(self.list_url, {"supplier_product_id": str(self.sp.id)}, format="json")
        self.client.post(self.list_url, {"supplier_product_id": str(self.sp.id)}, format="json")
        self.assertEqual(VenueSupplierProduct.objects.filter(venue=self.venue, supplier_product=self.sp).count(), 1)

    def test_remove_favorite(self):
        self.authenticate()
        resp = self.client.post(self.list_url, {"supplier_product_id": str(self.sp.id)}, format="json")
        fav_id = resp.json()["id"]
        detail_url = f"{self.list_url}/{fav_id}"
        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, 204)
        fav = VenueSupplierProduct.objects.get(pk=fav_id)
        self.assertEqual(fav.status, EntityStatus.ARCHIVED)

    def test_list_shows_added_favorites(self):
        VenueSupplierProduct.objects.create(
            venue=self.venue, supplier_product=self.sp, is_favorite=True, status=EntityStatus.ACTIVE
        )
        self.authenticate()
        response = self.client.get(self.list_url)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["supplier_product_id"], str(self.sp.id))


class SupplierProductDeleteRestrictionModelTests(TestCase):
    """2026-09-23 (backlog#task-69, отмена решения от 2026-08-11): удаление
    товара поставщика больше не должно безвозвратно стирать избранное и
    вишлисты покупателей — FK вернули RESTRICT (было CASCADE)."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe Restrict")
        self.sp = _make_supplier_product()

    def test_delete_restricted_while_favorited(self):
        VenueSupplierProduct.objects.create(venue=self.venue, supplier_product=self.sp, is_favorite=True)
        with self.assertRaises(RestrictedError):
            self.sp.delete()

    def test_delete_restricted_while_in_wishlist(self):
        user = _make_venue_user(email="restrict-wishlist@example.com")
        wishlist = Wishlist.objects.create(user=user, name="Список для рестрикта")
        WishlistItem.objects.create(wishlist=wishlist, supplier_product=self.sp)
        with self.assertRaises(RestrictedError):
            self.sp.delete()

    def test_delete_restricted_while_individual_venue_price_exists(self):
        SupplierProductVenuePrice.objects.create(supplier_product=self.sp, venue=self.venue, price=Decimal("500.00"))
        with self.assertRaises(RestrictedError):
            self.sp.delete()

    def test_delete_restricted_when_all_four_references_exist_at_once(self):
        """Пограничный случай: у товара одновременно есть все 4 типа истории,
        которые раньше терялись при CASCADE — убеждаемся, что ORM .delete()
        падает один раз (Django поднимает первую встреченную RestrictedError),
        а не молча удаляет часть связей до того, как наткнётся на защищённую."""
        user = _make_venue_user(email="restrict-combined@example.com")
        wishlist = Wishlist.objects.create(user=user, name="Комбинированный список")
        SupplierProductPrice.objects.create(supplier_product=self.sp, price=Decimal("10.00"), status=PriceStatus.ACTIVE)
        SupplierProductVenuePrice.objects.create(supplier_product=self.sp, venue=self.venue, price=Decimal("500.00"))
        VenueSupplierProduct.objects.create(venue=self.venue, supplier_product=self.sp, is_favorite=True)
        WishlistItem.objects.create(wishlist=wishlist, supplier_product=self.sp)

        with self.assertRaises(RestrictedError):
            self.sp.delete()

        self.assertTrue(SupplierProduct.objects.filter(pk=self.sp.pk).exists())
        self.assertEqual(SupplierProductPrice.objects.filter(supplier_product=self.sp).count(), 1)
        self.assertEqual(SupplierProductVenuePrice.objects.filter(supplier_product=self.sp).count(), 1)
        self.assertEqual(VenueSupplierProduct.objects.filter(supplier_product=self.sp).count(), 1)
        self.assertEqual(WishlistItem.objects.filter(supplier_product=self.sp).count(), 1)


class WishlistAPITests(APITestCase):
    def setUp(self):
        self.user = _make_venue_user()
        self.sp = _make_supplier_product(price="100.00")
        self.wishlists_url = "/api/v1/catalog/wishlists"

    def authenticate(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.user))

    def test_requires_authentication(self):
        response = self.client.get(self.wishlists_url)
        self.assertEqual(response.status_code, 401)

    def test_list_empty(self):
        self.authenticate()
        response = self.client.get(self.wishlists_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_create_wishlist(self):
        self.authenticate()
        response = self.client.post(self.wishlists_url, {"name": "Для бара"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["name"], "Для бара")
        self.assertEqual(response.json()["items_count"], 0)
        self.assertTrue(Wishlist.objects.filter(user=self.user, name="Для бара").exists())

    def test_create_duplicate_name_rejected(self):
        self.authenticate()
        self.client.post(self.wishlists_url, {"name": "Для бара"}, format="json")
        response = self.client.post(self.wishlists_url, {"name": "Для бара"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_only_own_wishlists_listed(self):
        other_user = _make_venue_user(email="other@example.com")
        Wishlist.objects.create(user=other_user, name="Чужой список")
        Wishlist.objects.create(user=self.user, name="Мой список")
        self.authenticate()
        response = self.client.get(self.wishlists_url)
        names = {w["name"] for w in response.json()}
        self.assertEqual(names, {"Мой список"})

    def test_rename_wishlist(self):
        self.authenticate()
        wishlist_id = self.client.post(self.wishlists_url, {"name": "Старое имя"}, format="json").json()["id"]
        response = self.client.patch(f"{self.wishlists_url}/{wishlist_id}", {"name": "Новое имя"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Новое имя")

    def test_cannot_rename_other_users_wishlist(self):
        other_user = _make_venue_user(email="other2@example.com")
        wishlist = Wishlist.objects.create(user=other_user, name="Чужой")
        self.authenticate()
        response = self.client.patch(f"{self.wishlists_url}/{wishlist.id}", {"name": "Переименовано"}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_archive_wishlist(self):
        self.authenticate()
        wishlist_id = self.client.post(self.wishlists_url, {"name": "На удаление"}, format="json").json()["id"]
        response = self.client.delete(f"{self.wishlists_url}/{wishlist_id}")
        self.assertEqual(response.status_code, 204)
        wishlist = Wishlist.objects.get(pk=wishlist_id)
        self.assertEqual(wishlist.status, EntityStatus.ARCHIVED)

    def test_add_and_list_items(self):
        self.authenticate()
        wishlist_id = self.client.post(self.wishlists_url, {"name": "Список"}, format="json").json()["id"]
        items_url = f"{self.wishlists_url}/{wishlist_id}/items"
        response = self.client.post(items_url, {"supplier_product_id": str(self.sp.id)}, format="json")
        self.assertEqual(response.status_code, 201)

        list_response = self.client.get(items_url)
        self.assertEqual(len(list_response.json()), 1)
        self.assertEqual(list_response.json()[0]["supplier_product_id"], str(self.sp.id))

        wishlists = self.client.get(self.wishlists_url).json()
        self.assertEqual(wishlists[0]["items_count"], 1)

    def test_add_item_idempotent(self):
        self.authenticate()
        wishlist_id = self.client.post(self.wishlists_url, {"name": "Список"}, format="json").json()["id"]
        items_url = f"{self.wishlists_url}/{wishlist_id}/items"
        self.client.post(items_url, {"supplier_product_id": str(self.sp.id)}, format="json")
        self.client.post(items_url, {"supplier_product_id": str(self.sp.id)}, format="json")
        self.assertEqual(WishlistItem.objects.filter(wishlist_id=wishlist_id, supplier_product=self.sp).count(), 1)

    def test_remove_item(self):
        self.authenticate()
        wishlist_id = self.client.post(self.wishlists_url, {"name": "Список"}, format="json").json()["id"]
        items_url = f"{self.wishlists_url}/{wishlist_id}/items"
        item_id = self.client.post(items_url, {"supplier_product_id": str(self.sp.id)}, format="json").json()["id"]
        response = self.client.delete(f"{items_url}/{item_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(WishlistItem.objects.get(pk=item_id).status, EntityStatus.ARCHIVED)

    def test_cannot_access_items_of_other_users_wishlist(self):
        other_user = _make_venue_user(email="other3@example.com")
        wishlist = Wishlist.objects.create(user=other_user, name="Чужой")
        self.authenticate()
        response = self.client.get(f"{self.wishlists_url}/{wishlist.id}/items")
        self.assertEqual(response.status_code, 404)


class WishlistConcurrencyTests(TransactionTestCase):
    """See CreateSupplierByOwnerConcurrencyTests (api/v1/suppliers/tests.py)
    for why TransactionTestCase + real threads is needed here."""

    def test_two_concurrent_creates_with_same_name_create_only_one_wishlist(self):
        from api.v1.catalog.services import create_wishlist

        user = _make_venue_user(email="wishlist-race@example.com")
        results = []
        errors = []

        def worker(label):
            connections.close_all()
            try:
                wishlist = create_wishlist(user=user, name="Гоночный список")
                results.append(wishlist.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(label,)) for label in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, f"expected exactly one wishlist created, got {results} errors={errors}")
        self.assertEqual(
            Wishlist.objects.filter(user=user, name="Гоночный список", status=EntityStatus.ACTIVE).count(), 1
        )


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogProductWishlistAnnotationAPITests(APITestCase):
    url = "/api/v1/catalog/products"

    def setUp(self):
        self.user = _make_venue_user()
        self.sp = _make_supplier_product(price="100.00")

    def authenticate_jwt(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))

    def authenticate_service_token(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_wishlists_empty_without_jwt(self):
        self.authenticate_service_token()
        response = self.client.get(self.url)
        self.assertEqual(response.json()["results"][0]["wishlists"], [])

    def test_wishlists_shows_membership_in_both_lists(self):
        wl1 = Wishlist.objects.create(user=self.user, name="Для бара")
        wl2 = Wishlist.objects.create(user=self.user, name="Постоянный заказ")
        WishlistItem.objects.create(wishlist=wl1, supplier_product=self.sp, status=EntityStatus.ACTIVE)
        WishlistItem.objects.create(wishlist=wl2, supplier_product=self.sp, status=EntityStatus.ACTIVE)

        self.authenticate_jwt()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        names = {w["name"] for w in response.json()["results"][0]["wishlists"]}
        self.assertEqual(names, {"Для бара", "Постоянный заказ"})

    def test_wishlists_scoped_to_requesting_user(self):
        other_user = _make_venue_user(email="other4@example.com")
        other_wishlist = Wishlist.objects.create(user=other_user, name="Не моё")
        WishlistItem.objects.create(wishlist=other_wishlist, supplier_product=self.sp, status=EntityStatus.ACTIVE)

        self.authenticate_jwt()
        response = self.client.get(self.url)
        self.assertEqual(response.json()["results"][0]["wishlists"], [])


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueOrderAPITests(APITestCase):
    """История/детали уже отправленных заказов. Сборка/редактирование заказа
    до отправки — это корзина (VenueCartAPITests), не Order (2026-08-28,
    Cart/Order split)."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = _make_venue_owner(self.venue)
        self.sp = _make_supplier_product(price="100.00")
        self.unit = Unit.objects.filter(code="шт").first()
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"
        self.orders_url = f"/api/v1/catalog/venues/{self.venue.id}/orders"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _submit_order_with_item(self, qty="5.0"):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": qty},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate()
        return order_id

    def test_rejects_without_service_token(self):
        response = self.client.get(self.orders_url)
        # 401, not 403: the view also registers a JWT authenticator, so DRF's
        # permission_denied() reports "unauthenticated" rather than "forbidden"
        # for a request with no credentials at all.
        self.assertEqual(response.status_code, 401)

    def test_list_orders(self):
        self.authenticate()
        self._submit_order_with_item()
        response = self.client.get(self.orders_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_list_orders_one_row_per_supplier(self):
        """2026-09-17, по прямой просьбе пользователя: "у тебя должны с
        бэка отдаваться на каждого поставщика заявки, а не одна заявка
        на двух и более поставщиков" — раньше одна строка отвечала
        одному Order (с suppliers[] внутри), теперь одна строка = одна
        заявка/группа одному поставщику, тем же паттерном, что уже был у
        поставщика (GET /suppliers/{supplier_id}/orders). Покрывает и
        обычных поставщиков (с персистентной SupplierOrderRequest), и
        вне-системных/нераспознанных (синтетический id, number=null)."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        other_supplier = Supplier.objects.create(legal_name="Other LLC", display_name="Other Supplier")
        other_sp = _make_supplier_product(supplier=other_supplier, normalized_name="other-product", price="50.00")
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")

        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(other_sp.id), "requested_qty": "1.0"},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/items",
            {
                "requested_name": "Микрозелень",
                "requested_qty": "1.0",
                "off_system_supplier_id": str(off_system.id),
            },
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate()

        response = self.client.get(self.orders_url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 3)
        rows = data["results"]
        self.assertTrue(all(row["order_id"] == order_id for row in rows))
        by_name = {row["supplier_name"]: row for row in rows}

        real_row = by_name[self.sp.supplier.display_name]
        self.assertEqual(real_row["supplier_id"], str(self.sp.supplier_id))
        self.assertFalse(real_row["is_off_system"])
        self.assertTrue(real_row["number"].startswith("REQ-"))
        self.assertNotIn("items", real_row)

        off_system_row = by_name["Рынок"]
        self.assertEqual(off_system_row["supplier_id"], str(off_system.id))
        self.assertTrue(off_system_row["is_off_system"])
        self.assertIsNone(off_system_row["number"])
        self.assertIsNotNone(off_system_row["id"])

    def test_list_orders_includes_row_for_item_without_recognized_supplier(self):
        """Позиция без supplier_product/off_system_supplier_id тоже
        получает свою строку (supplier_id/supplier_name/number — null)."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items", {"requested_name": "Без поставщика", "requested_qty": "1.0"}, format="json"
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate()
        row = self.client.get(self.orders_url).json()["results"][0]
        self.assertEqual(row["order_id"], order_id)
        self.assertIsNone(row["supplier_id"])
        self.assertIsNone(row["supplier_name"])
        self.assertIsNone(row["number"])
        self.assertFalse(row["is_off_system"])
        self.assertIsNotNone(row["id"])

    def test_list_orders_row_includes_minimum_order_amount(self):
        """2026-09-25, Павел (задача #Лк1, "Корзину и Историю заявок"): то же
        "Мин. заказ", что уже в корзине (см. VenueCartIndividualPriceAPITests/
        VenueCartAPITests), нужно и в Истории заявок — на уровне строки
        заявки/группы поставщика."""
        self.sp.supplier.delivery_min_order_amount = "1000.00"
        self.sp.supplier.delivery_currency = "RUB"
        self.sp.supplier.save(update_fields=["delivery_min_order_amount", "delivery_currency"])
        off_system = OffSystemSupplier.objects.create(
            venue=self.venue, display_name="Айсберг", delivery_min_order_amount=Decimal("10000.00")
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Баклажан", "requested_qty": "1.0", "off_system_supplier_id": str(off_system.id)},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        self.authenticate()

        rows = self.client.get(self.orders_url).json()["results"]
        by_name = {row["supplier_name"]: row for row in rows}
        real_row = by_name[self.sp.supplier.display_name]
        self.assertEqual(float(real_row["minimum_order_amount"]), 1000.0)
        self.assertEqual(real_row["currency"], "RUB")
        off_system_row = by_name["Айсберг"]
        self.assertEqual(float(off_system_row["minimum_order_amount"]), 10000.0)
        self.assertEqual(off_system_row["currency"], "RUB")

    def test_list_orders_row_minimum_order_amount_null_when_not_set(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок без минимума")
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Микрозелень", "requested_qty": "1.0", "off_system_supplier_id": str(off_system.id)},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        self.authenticate()
        row = self.client.get(self.orders_url).json()["results"][0]
        self.assertIsNone(row["minimum_order_amount"])
        # currency остаётся значением по умолчанию ("RUB") даже без минимума —
        # OffSystemSupplier.delivery_currency не nullable, тот же паттерн, что
        # у обычного Supplier.delivery_currency в корзине.
        self.assertEqual(row["currency"], "RUB")

    def test_synthetic_row_id_is_stable_across_requests(self):
        """У вне-системного поставщика нет персистентной заявки — id
        синтезируется, но должен быть одинаковым при повторном запросе
        того же заказа (иначе фронт не сможет опираться на него как на
        стабильный ключ списка)."""
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Микрозелень", "requested_qty": "1.0", "off_system_supplier_id": str(off_system.id)},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        self.authenticate()
        first_id = self.client.get(self.orders_url).json()["results"][0]["id"]
        second_id = self.client.get(self.orders_url).json()["results"][0]["id"]
        self.assertEqual(first_id, second_id)

    def test_list_orders_status_reflects_partial_accept(self):
        """2026-09-15, пользователь: "неверный статус у второго товара
        confirmed а должен быть partially-confirmed" — фикс поднят на
        уровень строки заявки в этом списке напрямую (не только на
        supplier_groups в detail)."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "10.0"}, format="json"
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        item_id = str(Order.objects.get(pk=order_id).items.get().id)
        self.authenticate()
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.sp.supplier_id}/orders/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE, "items": [{"item_id": item_id, "confirmed_qty": "6.0"}]},
            format="json",
        )
        response = self.client.get(self.orders_url)
        row = response.json()["results"][0]
        self.assertEqual(row["order_id"], order_id)
        self.assertEqual(row["status"], "partially_confirmed")

    def test_list_orders_query_count_does_not_grow_with_order_count(self):
        """Страхует prefetch_related в venue_supplier_order_rows — без
        него каждый заказ в списке добавлял бы свой запрос ради
        группировки по поставщику. 2026-09-17: строки не несут items,
        только сводку — 6 запросов: venue (get_object_or_404), orders,
        order_items, supplier_products, suppliers, supplier_order_requests."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        for _ in range(3):
            self.client.post(
                f"{self.cart_url}/items",
                {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
                format="json",
            )
            self.client.post(
                f"{self.cart_url}/submit",
                {"command_id": str(uuid4()), "expected_settings_version": 1},
                format="json",
            )
        self.authenticate()

        with self.assertNumQueries(6):
            response = self.client.get(self.orders_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)

    def test_order_detail_includes_items(self):
        self.authenticate()
        order_id = self._submit_order_with_item(qty="5.0")
        response = self.client.get(f"{self.orders_url}/{order_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(_grouped_order_items(data)), 1)
        self.assertEqual(_grouped_order_items(data)[0]["requested_qty"], "5.0000")

    def test_order_detail_reachable_via_supplier_request_row_id(self):
        """2026-09-17, пользователь: "ты сломал в истории этот запрос" —
        404 на GET .../orders/{order_id}. venue_supplier_order_rows
        (список истории) с этого же дня отдаёт id заявки отдельно от
        order_id заказа — если фронт по-прежнему переходит в детали по
        row.id (как раньше, когда id совпадал с order_id), нужно, чтобы
        detail тоже резолвился по id заявки, не только по id заказа."""
        order_id = self._submit_order_with_item(qty="5.0")
        self.authenticate()
        row = self.client.get(self.orders_url).json()["results"][0]
        self.assertNotEqual(row["id"], order_id)
        response = self.client.get(f"{self.orders_url}/{row['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], order_id)

    def test_order_detail_reachable_via_off_system_synthetic_row_id(self):
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Микрозелень", "requested_qty": "1.0", "off_system_supplier_id": str(off_system.id)},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate()
        row = self.client.get(self.orders_url).json()["results"][0]
        self.assertNotEqual(row["id"], order_id)
        response = self.client.get(f"{self.orders_url}/{row['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], order_id)

    def test_order_complete_reachable_via_supplier_request_row_id(self):
        order_id = self._submit_order_with_item(qty="5.0")
        self.authenticate()
        row_id = self.client.get(self.orders_url).json()["results"][0]["id"]
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.sp.supplier_id}/orders/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        response = self.client.post(f"{self.orders_url}/{row_id}/complete")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.get(pk=order_id).status, "completed")

    def test_order_detail_includes_venue_name(self):
        self.authenticate()
        order_id = self._submit_order_with_item(qty="5.0")
        data = self.client.get(f"{self.orders_url}/{order_id}").json()
        self.assertEqual(data["venue_name"], self.venue.display_name)

    def test_order_detail_items_count_reflects_actual_items(self):
        """2026-09-09, Витя: прислал JSON с items_count=0 при 3 реальных
        позициях в items — items_count наследовался с OrderSerializer как
        обычный IntegerField(default=0), ни один из вызывающих
        OrderDetailSerializer мест его не проставлял (в отличие от списочных
        ручек, которые считают его через annotate/queryset.count())."""
        self.authenticate()
        other_sp = _make_supplier_product(price="50.00", normalized_name="second-product")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"}, format="json"
        )
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(other_sp.id), "requested_qty": "1.0"},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate()
        data = self.client.get(f"{self.orders_url}/{order_id}").json()
        self.assertEqual(data["items_count"], 2)
        self.assertEqual(len(_grouped_order_items(data)), 2)

    def test_order_item_includes_order_level_dates(self):
        """2026-09-09, Витя: "должно быть еще и в айтемс" / "я же по
        массиву этому итерируюсь" — фронт строит таблицу из items, поэтому
        даты самой заявки нужны на каждой позиции, не только в корне ответа."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.patch(self.cart_url, {"requested_delivery_date": "2026-09-20"}, format="json")
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "5.0"}, format="json"
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate()
        response = self.client.get(f"{self.orders_url}/{order_id}")
        data = response.json()
        item = _grouped_order_items(data)[0]
        self.assertEqual(item["order_created_at"], data["created_at"])
        self.assertEqual(item["order_requested_delivery_date"], "2026-09-20")
        self.assertEqual(data["requested_delivery_date"], "2026-09-20")

    def test_order_item_order_requested_delivery_date_null_when_not_set(self):
        self.authenticate()
        order_id = self._submit_order_with_item(qty="5.0")
        data = self.client.get(f"{self.orders_url}/{order_id}").json()
        self.assertIsNone(_grouped_order_items(data)[0]["order_requested_delivery_date"])
        self.assertIsNone(data["requested_delivery_date"])

    def test_order_item_includes_venue_name(self):
        """2026-09-14, по замечанию пользователя на GET .../orders/{order_id}
        (supplier_order_retrieve, "в items не хватает venue_name") — тот же
        гэп применим и к venue-стороне: то же venue_name, что в корне
        ответа, продублировано на каждой позиции тем же паттерном, что
        order_created_at/order_requested_delivery_date."""
        self.authenticate()
        order_id = self._submit_order_with_item(qty="5.0")
        data = self.client.get(f"{self.orders_url}/{order_id}").json()
        self.assertEqual(
            _grouped_order_items(data)[0]["venue_name"],
            self.venue.display_name,
        )
        self.assertEqual(_grouped_order_items(data)[0]["venue_name"], data["venue_name"])

    def test_order_item_includes_supplier_product_image_url(self):
        """2026-09-09, Витя: «у товаров в корзине нету картинки» — тот же
        гэп на уже отправленной заявке."""
        self.sp.image_url = "https://example.com/product.jpg"
        self.sp.save(update_fields=["image_url"])
        self.authenticate()
        order_id = self._submit_order_with_item(qty="5.0")
        response = self.client.get(f"{self.orders_url}/{order_id}")
        data = response.json()
        self.assertEqual(
            _grouped_order_items(data)[0]["supplier_product_image_url"],
            "https://example.com/product.jpg",
        )

    def test_order_detail_groups_items_by_supplier(self):
        """2026-08-27, Vitya: "в корзине нет группировки по поставщикам" —
        supplier_groups собирает те же items в блоки: обычный поставщик /
        поставщик вне системы / без распознанного поставщика. Проверяем, что
        эта же группировка сохранилась на уже отправленном заказе."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        other_supplier = Supplier.objects.create(legal_name="Other LLC", display_name="Other Supplier")
        other_sp = _make_supplier_product(supplier=other_supplier, normalized_name="other-product", price="50.00")
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")

        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(other_sp.id), "requested_qty": "1.0"},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/items",
            {
                "requested_name": "Микрозелень",
                "requested_qty": "1.0",
                "off_system_supplier_id": str(off_system.id),
            },
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Без поставщика", "requested_qty": "1.0"},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]

        response = self.client.get(f"{self.orders_url}/{order_id}")
        self.assertEqual(response.status_code, 200)
        groups = response.json()["supplier_groups"]
        self.assertEqual(len(groups), 4)

        by_name = {g["supplier_name"]: g for g in groups}
        self.assertEqual(len(by_name[self.sp.supplier.display_name]["items"]), 1)
        self.assertFalse(by_name[self.sp.supplier.display_name]["is_off_system"])
        self.assertEqual(str(self.sp.supplier_id), by_name[self.sp.supplier.display_name]["supplier_id"])

        self.assertEqual(len(by_name["Рынок"]["items"]), 1)
        self.assertTrue(by_name["Рынок"]["is_off_system"])
        self.assertEqual(str(off_system.id), by_name["Рынок"]["supplier_id"])

        unmatched = by_name[None]
        self.assertEqual(len(unmatched["items"]), 1)
        self.assertIsNone(unmatched["supplier_id"])

    def test_list_orders_filtered_by_status(self):
        self.authenticate()
        submitted_id = self._submit_order_with_item()
        response = self.client.get(self.orders_url, {"status": "submitted"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["order_id"], submitted_id)

        response = self.client.get(self.orders_url, {"status": "confirmed"})
        self.assertEqual(response.json()["count"], 0)


class VenueMineOrdersAPITests(APITestCase):
    """2026-09-08: агрегация истории заказов по всем заведениям аккаунта —
    прямая просьба пользователя ("нужна!") по итогам сверки с макетом
    "История заказов" от Вити (venue/cabinet/companies, колонка "Ресторан"
    подразумевает несколько заведений в одной ленте). Мирроит
    VenueOrderAPITests, но actor — core.User напрямую (не venue_id из URL),
    а venue_ids собираются из активных VenueUser."""

    def setUp(self):
        self.venue_a = Venue.objects.create(display_name="Cafe A")
        self.venue_b = Venue.objects.create(display_name="Cafe B")
        self.other_venue = Venue.objects.create(display_name="Cafe Other")
        self.owner = User.objects.create(
            email="mine-orders-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        VenueUser.objects.create(
            venue=self.venue_a, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.venue_b, user=self.owner, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.sp = _make_supplier_product(price="100.00")
        self.url = "/api/v1/catalog/venues/mine/orders"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _submit_order(self, venue, qty="5.0"):
        cart_url = f"/api/v1/catalog/venues/{venue.id}/cart"
        self.authenticate()
        self.client.post(
            f"{cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": qty}, format="json"
        )
        return self.client.post(
            f"{cart_url}/submit", {"command_id": str(uuid4()), "expected_settings_version": 1}, format="json"
        ).json()["id"]

    def test_requires_authentication(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_aggregates_orders_across_my_venues(self):
        order_a = self._submit_order(self.venue_a)
        order_b = self._submit_order(self.venue_b)
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 2)
        order_ids = {row["order_id"] for row in data["results"]}
        self.assertEqual(order_ids, {order_a, order_b})

    def test_includes_venue_name_per_row(self):
        order_a = self._submit_order(self.venue_a)
        self.authenticate()
        response = self.client.get(self.url)
        row = next(r for r in response.json()["results"] if r["order_id"] == order_a)
        self.assertEqual(row["venue_name"], "Cafe A")
        self.assertEqual(row["venue_id"], str(self.venue_a.id))

    def test_excludes_orders_from_venues_i_do_not_manage(self):
        other_owner = User.objects.create(email="other-owner@example.com", full_name="Other", role=UserRole.VENUE_USER)
        VenueUser.objects.create(
            venue=self.other_venue, user=other_owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        cart_url = f"/api/v1/catalog/venues/{self.other_venue.id}/cart"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other_owner))
        self.client.post(
            f"{cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"}, format="json"
        )
        self.client.post(
            f"{cart_url}/submit", {"command_id": str(uuid4()), "expected_settings_version": 1}, format="json"
        )

        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json()["count"], 0)

    def test_filters_by_status(self):
        submitted_id = self._submit_order(self.venue_a)
        self.authenticate()
        response = self.client.get(self.url, {"status": "submitted"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["order_id"], submitted_id)

        response = self.client.get(self.url, {"status": "confirmed"})
        self.assertEqual(response.json()["count"], 0)

    def test_filters_by_venue_id(self):
        """2026-09-18, Дима (фронт): "История заявок... отображаются все
        заявки всех заведений... должен быть фильтр: Заведения (с
        мультиселектом по заведениям)"."""
        order_a = self._submit_order(self.venue_a)
        self._submit_order(self.venue_b)
        self.authenticate()
        response = self.client.get(self.url, {"venue_id": str(self.venue_a.id)})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["order_id"], order_a)

    def test_filters_by_multiple_venue_ids(self):
        order_a = self._submit_order(self.venue_a)
        order_b = self._submit_order(self.venue_b)
        self.authenticate()
        response = self.client.get(f"{self.url}?venue_id={self.venue_a.id}&venue_id={self.venue_b.id}")
        data = response.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual({row["order_id"] for row in data["results"]}, {order_a, order_b})

    def test_venue_id_filter_cannot_leak_unmanaged_venue(self):
        self._submit_order(self.venue_a)
        cart_url = f"/api/v1/catalog/venues/{self.other_venue.id}/cart"
        other_owner = User.objects.create(
            email="leak-owner@example.com", full_name="Leak Owner", role=UserRole.VENUE_USER
        )
        VenueUser.objects.create(
            venue=self.other_venue, user=other_owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other_owner))
        self.client.post(
            f"{cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"}, format="json"
        )
        self.client.post(
            f"{cart_url}/submit", {"command_id": str(uuid4()), "expected_settings_version": 1}, format="json"
        )

        self.authenticate()
        response = self.client.get(self.url, {"venue_id": str(self.other_venue.id)})
        self.assertEqual(response.json()["count"], 0)

    def test_new_venue_membership_appears_without_extra_setup(self):
        """No hardcoded venue_id anywhere in the view — joining a new venue
        (or losing membership) changes the result set on the next request."""
        order_a = self._submit_order(self.venue_a)
        self.authenticate()
        self.assertEqual(self.client.get(self.url).json()["count"], 1)

        third_venue = Venue.objects.create(display_name="Cafe C")
        VenueUser.objects.create(
            venue=third_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        order_c = self._submit_order(third_venue)
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json()["count"], 2)
        self.assertEqual({r["order_id"] for r in response.json()["results"]}, {order_a, order_c})


# ── New filter & meta endpoint tests ────────────────────────────────────────


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogProductFilterTests(APITestCase):
    url = "/api/v1/catalog/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_filters_by_price_min(self):
        _make_supplier_product(price="50.00")
        _make_supplier_product(price="200.00")
        self.authenticate()
        response = self.client.get(self.url, {"price_min": "100"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["current_price"], "200.00")

    def test_filters_by_price_max(self):
        _make_supplier_product(price="50.00")
        _make_supplier_product(price="200.00")
        self.authenticate()
        response = self.client.get(self.url, {"price_max": "100"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["current_price"], "50.00")

    def test_price_min_and_max_combined(self):
        _make_supplier_product(price="10.00")
        _make_supplier_product(price="75.00")
        _make_supplier_product(price="300.00")
        self.authenticate()
        response = self.client.get(self.url, {"price_min": "50", "price_max": "100"})
        self.assertEqual(response.json()["count"], 1)

    def test_filters_by_brand_id(self):
        brand = Brand.objects.create(canonical_name="BrandA", normalized_name="branda")
        _make_supplier_product(price="100.00", brand=brand)
        _make_supplier_product(price="100.00")
        self.authenticate()
        response = self.client.get(self.url, {"brand_id": str(brand.id)})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["brand_id"], str(brand.id))

    def test_sort_price_asc(self):
        _make_supplier_product(price="300.00")
        _make_supplier_product(price="100.00")
        _make_supplier_product(price="200.00")
        self.authenticate()
        response = self.client.get(self.url, {"sort": "price_asc"})
        prices = [r["current_price"] for r in response.json()["results"]]
        self.assertEqual(prices, sorted(prices))

    def test_sort_price_desc(self):
        _make_supplier_product(price="100.00")
        _make_supplier_product(price="300.00")
        _make_supplier_product(price="200.00")
        self.authenticate()
        response = self.client.get(self.url, {"sort": "price_desc"})
        prices = [r["current_price"] for r in response.json()["results"]]
        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_filters_by_parent_category_id(self):
        parent = ProductCategory.objects.create(name="Мясо", slug="meat")
        leaf = ProductCategory.objects.create(name="Говядина", slug="beef", parent=parent)
        _make_supplier_product(price="100.00", category=leaf)
        _make_supplier_product(price="200.00")  # no category
        self.authenticate()
        response = self.client.get(self.url, {"category_id": str(parent.id)})
        self.assertEqual(response.json()["count"], 1)

    def test_filters_by_group_category_id(self):
        group = ProductCategory.objects.create(name="Продукты", slug="food")
        parent = ProductCategory.objects.create(name="Мясо", slug="meat2", parent=group)
        leaf = ProductCategory.objects.create(name="Свинина", slug="pork", parent=parent)
        _make_supplier_product(price="100.00", category=leaf)
        _make_supplier_product(price="200.00")
        self.authenticate()
        response = self.client.get(self.url, {"category_id": str(group.id)})
        self.assertEqual(response.json()["count"], 1)

    def test_response_includes_brand_fields(self):
        brand = Brand.objects.create(canonical_name="BrandX", normalized_name="brandx")
        _make_supplier_product(price="100.00", brand=brand)
        self.authenticate()
        response = self.client.get(self.url)
        row = response.json()["results"][0]
        self.assertEqual(row["brand_id"], str(brand.id))
        self.assertEqual(row["brand"], "BrandX")

    def test_response_includes_parent_category(self):
        parent = ProductCategory.objects.create(name="Молочное", slug="dairy")
        leaf = ProductCategory.objects.create(name="Сыр", slug="cheese", parent=parent)
        _make_supplier_product(price="100.00", category=leaf)
        self.authenticate()
        response = self.client.get(self.url)
        row = response.json()["results"][0]
        self.assertEqual(row["category"], "Сыр")
        self.assertEqual(row["parent_category"], "Молочное")
        self.assertEqual(row["parent_category_id"], str(parent.id))

    def test_response_includes_group_hierarchy(self):
        group = ProductCategory.objects.create(name="Бакалея", slug="grocery")
        parent = ProductCategory.objects.create(name="Мука", slug="flour-cat", parent=group)
        leaf = ProductCategory.objects.create(name="Пшеничная", slug="wheat", parent=parent)
        _make_supplier_product(price="100.00", category=leaf)
        self.authenticate()
        response = self.client.get(self.url)
        row = response.json()["results"][0]
        self.assertEqual(row["group"], "Бакалея")
        self.assertEqual(row["group_id"], str(group.id))

    def test_image_url_placeholder_when_not_set(self):
        # 2026-09-30 (TASK-125): без своего фото и без категории — общая картинка-заглушка, не null.
        _make_supplier_product(price="100.00")
        self.authenticate()
        row = self.client.get(self.url).json()["results"][0]
        self.assertTrue(row["image_url"].endswith("/catalog/category-placeholders/generic-grocery.svg"))
        self.assertTrue(row["image_is_placeholder"])


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogBrandListAPITests(APITestCase):
    url = "/api/v1/catalog/brands"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_allows_anonymous_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_returns_empty_when_no_brands(self):
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_returns_brands_with_active_priced_products(self):
        brand = Brand.objects.create(canonical_name="ActiveBrand", normalized_name="activebrand")
        _make_supplier_product(price="100.00", brand=brand)
        self.authenticate()
        response = self.client.get(self.url)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(brand.id))
        self.assertEqual(data[0]["name"], "ActiveBrand")

    def test_excludes_brands_without_priced_products(self):
        brand = Brand.objects.create(canonical_name="EmptyBrand", normalized_name="emptybrand")
        _make_supplier_product(price=None, brand=brand)  # no price → excluded
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), [])

    def test_excludes_inactive_brands(self):
        brand = Brand.objects.create(
            canonical_name="InactiveBrand", normalized_name="inactivebrand", status=EntityStatus.ARCHIVED
        )
        _make_supplier_product(price="100.00", brand=brand)
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), [])


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogCategoryListAPITests(APITestCase):
    url = "/api/v1/catalog/categories"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_allows_anonymous_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_returns_empty_when_no_categories(self):
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_returns_leaf_categories_with_active_products(self):
        cat = ProductCategory.objects.create(name="Сыр", slug="cheese2")
        _make_supplier_product(price="100.00", category=cat)
        self.authenticate()
        response = self.client.get(self.url)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(cat.id))
        self.assertEqual(data[0]["name"], "Сыр")

    def test_response_includes_parent_and_group(self):
        group = ProductCategory.objects.create(name="Группа", slug="group1")
        parent = ProductCategory.objects.create(name="Подгруппа", slug="sub1", parent=group)
        leaf = ProductCategory.objects.create(name="Лист", slug="leaf1", parent=parent)
        _make_supplier_product(price="100.00", category=leaf)
        self.authenticate()
        response = self.client.get(self.url)
        data = response.json()
        self.assertEqual(len(data), 1)
        row = data[0]
        self.assertEqual(row["name"], "Лист")
        self.assertEqual(row["parent_id"], str(parent.id))
        self.assertEqual(row["parent_name"], "Подгруппа")
        self.assertEqual(row["group_id"], str(group.id))
        self.assertEqual(row["group_name"], "Группа")

    def test_excludes_categories_without_active_products(self):
        cat = ProductCategory.objects.create(name="Пустая", slug="empty-cat")
        _make_supplier_product(price=None, category=cat)
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), [])


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogSupplierListAPITests(APITestCase):
    url = "/api/v1/catalog/suppliers"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_allows_anonymous_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_returns_empty_when_no_suppliers(self):
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_returns_suppliers_with_active_priced_products(self):
        supplier = Supplier.objects.create(legal_name="Поставщик LLC", display_name="Поставщик")
        _make_supplier_product(supplier=supplier, price="100.00")
        self.authenticate()
        response = self.client.get(self.url)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(supplier.id))
        self.assertEqual(data[0]["name"], "Поставщик")

    def test_excludes_suppliers_without_priced_products(self):
        supplier = Supplier.objects.create(legal_name="Пустой LLC", display_name="Пустой")
        _make_supplier_product(supplier=supplier, price=None)
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), [])

    def test_excludes_inactive_suppliers(self):
        supplier = Supplier.objects.create(legal_name="Архив LLC", display_name="Архив", status=EntityStatus.ARCHIVED)
        _make_supplier_product(supplier=supplier, price="100.00")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), [])

    def test_excludes_unpublished_suppliers(self):
        """2026-07-29: найдено при реализации self-service удаления компании —
        is_published=False (снят с публикации, но status ещё active) раньше
        никак не влиял на каталог, товары продолжали быть видны покупателям."""
        supplier = Supplier.objects.create(
            legal_name="Снят LLC", display_name="Снят", status=EntityStatus.ACTIVE, is_published=False
        )
        _make_supplier_product(supplier=supplier, price="100.00")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), [])

    def test_search_by_display_name(self):
        s1 = Supplier.objects.create(legal_name="Alpha LLC", display_name="AlphaMart")
        s2 = Supplier.objects.create(legal_name="Beta LLC", display_name="BetaShop")
        _make_supplier_product(supplier=s1, price="100.00")
        _make_supplier_product(supplier=s2, price="100.00")
        self.authenticate()
        response = self.client.get(self.url, {"q": "alpha"})
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(s1.id))

    def test_search_by_inn(self):
        supplier = Supplier.objects.create(legal_name="INN LLC", display_name="INN Supplier", inn="7700000001")
        _make_supplier_product(supplier=supplier, price="100.00")
        self.authenticate()
        response = self.client.get(self.url, {"q": "7700000001"})
        self.assertEqual(len(response.json()), 1)

    def test_response_includes_legal_name_and_inn(self):
        supplier = Supplier.objects.create(legal_name="Юр LLC", display_name="Юр Поставщик", inn="7700000002")
        _make_supplier_product(supplier=supplier, price="100.00")
        self.authenticate()
        response = self.client.get(self.url)
        row = response.json()[0]
        self.assertEqual(row["legal_name"], "Юр LLC")
        self.assertEqual(row["inn"], "7700000002")


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogCountryListAPITests(APITestCase):
    url = "/api/v1/catalog/countries"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_allows_anonymous_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_returns_empty_when_no_countries(self):
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_returns_distinct_countries(self):
        _make_supplier_product(price="100.00", normalized_name="p1")
        sp2 = _make_supplier_product(price="200.00", normalized_name="p2")
        sp3 = _make_supplier_product(price="300.00", normalized_name="p3")
        SupplierProduct.objects.filter(pk__in=[sp2.pk, sp3.pk]).update(country_of_origin="Россия")
        sp4 = _make_supplier_product(price="400.00", normalized_name="p4")
        sp4.country_of_origin = "Италия"
        sp4.save(update_fields=["country_of_origin"])
        self.authenticate()
        response = self.client.get(self.url)
        countries = [r["country"] for r in response.json()]
        self.assertIn("Россия", countries)
        self.assertIn("Италия", countries)
        self.assertEqual(len(countries), 2)  # null/blank excluded, Россия deduplicated

    def test_excludes_products_without_price(self):
        sp = _make_supplier_product(price=None)
        sp.country_of_origin = "Германия"
        sp.save(update_fields=["country_of_origin"])
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), [])


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogProductAvailabilityFilterTests(APITestCase):
    url = "/api/v1/catalog/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_default_availability_is_in_stock(self):
        sp = _make_supplier_product(price="100.00")
        self.assertEqual(sp.availability, ProductAvailability.IN_STOCK)

    def test_filters_in_stock(self):
        _make_supplier_product(price="100.00", normalized_name="in1")
        sp_out = _make_supplier_product(price="200.00", normalized_name="out1")
        sp_out.availability = ProductAvailability.OUT_OF_STOCK
        sp_out.save(update_fields=["availability"])
        self.authenticate()
        response = self.client.get(self.url, {"availability": "in_stock"})
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["availability"], "in_stock")

    def test_filters_out_of_stock(self):
        _make_supplier_product(price="100.00", normalized_name="in2")
        sp_out = _make_supplier_product(price="200.00", normalized_name="out2")
        sp_out.availability = ProductAvailability.OUT_OF_STOCK
        sp_out.save(update_fields=["availability"])
        self.authenticate()
        response = self.client.get(self.url, {"availability": "out_of_stock"})
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["availability"], "out_of_stock")

    def test_response_includes_availability_field(self):
        _make_supplier_product(price="100.00")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertIn("availability", response.json()["results"][0])

    def test_filters_on_order(self):
        """2026-09-18, Pavel (скрин "Под заказ"/"В наличии"/"Нет в наличии"):
        добавлен 3-й статус ProductAvailability.ON_ORDER."""
        _make_supplier_product(price="100.00", normalized_name="in3")
        sp_order = _make_supplier_product(price="200.00", normalized_name="order3")
        sp_order.availability = ProductAvailability.ON_ORDER
        sp_order.save(update_fields=["availability"])
        self.authenticate()
        response = self.client.get(self.url, {"availability": "on_order"})
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["availability"], "on_order")

    def test_sort_in_stock_first(self):
        sp_out = _make_supplier_product(price="100.00", normalized_name="z-out")
        sp_out.availability = ProductAvailability.OUT_OF_STOCK
        sp_out.save(update_fields=["availability"])
        sp_order = _make_supplier_product(price="150.00", normalized_name="m-order")
        sp_order.availability = ProductAvailability.ON_ORDER
        sp_order.save(update_fields=["availability"])
        _make_supplier_product(price="200.00", normalized_name="a-in")
        self.authenticate()
        response = self.client.get(self.url, {"sort": "in_stock_first"})
        results = response.json()["results"]
        self.assertEqual(results[0]["availability"], "in_stock")
        self.assertEqual(results[1]["availability"], "on_order")
        self.assertEqual(results[2]["availability"], "out_of_stock")


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogProductOffersFilterTests(APITestCase):
    url = "/api/v1/catalog/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _sp_with_offer(self, flag, price="100.00", name="offer-product"):
        sp = _make_supplier_product(price=price, normalized_name=name)
        setattr(sp, flag, True)
        sp.save(update_fields=[flag])
        return sp

    def test_filters_promo(self):
        self._sp_with_offer("is_promo", name="promo1")
        _make_supplier_product(price="200.00", normalized_name="plain1")
        self.authenticate()
        response = self.client.get(self.url, {"offers": "promo"})
        self.assertEqual(response.json()["count"], 1)
        self.assertTrue(response.json()["results"][0]["is_promo"])

    def test_filters_discount(self):
        self._sp_with_offer("is_discount", name="disc1")
        _make_supplier_product(price="200.00", normalized_name="plain2")
        self.authenticate()
        response = self.client.get(self.url, {"offers": "discount"})
        self.assertEqual(response.json()["count"], 1)
        self.assertTrue(response.json()["results"][0]["is_discount"])

    def test_filters_new(self):
        self._sp_with_offer("is_new", name="new1")
        _make_supplier_product(price="200.00", normalized_name="plain3")
        self.authenticate()
        response = self.client.get(self.url, {"offers": "new"})
        self.assertEqual(response.json()["count"], 1)
        self.assertTrue(response.json()["results"][0]["is_new"])

    def test_multi_offers_or_logic(self):
        self._sp_with_offer("is_promo", name="mp1")
        self._sp_with_offer("is_new", name="mn1")
        _make_supplier_product(price="200.00", normalized_name="plain4")
        self.authenticate()
        response = self.client.get("/api/v1/catalog/products?offers=promo&offers=new")
        self.assertEqual(response.json()["count"], 2)


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogProductMultiSelectFilterTests(APITestCase):
    url = "/api/v1/catalog/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_multi_brand_id(self):
        b1 = Brand.objects.create(canonical_name="B1", normalized_name="b1")
        b2 = Brand.objects.create(canonical_name="B2", normalized_name="b2")
        _make_supplier_product(price="100.00", brand=b1, normalized_name="mb1")
        _make_supplier_product(price="100.00", brand=b2, normalized_name="mb2")
        _make_supplier_product(price="100.00", normalized_name="mb3")  # no brand
        self.authenticate()
        response = self.client.get(f"{self.url}?brand_id={b1.id}&brand_id={b2.id}")
        self.assertEqual(response.json()["count"], 2)

    def test_multi_category_id(self):
        c1 = ProductCategory.objects.create(name="Cat1", slug="c1ms")
        c2 = ProductCategory.objects.create(name="Cat2", slug="c2ms")
        _make_supplier_product(price="100.00", category=c1, normalized_name="mc1")
        _make_supplier_product(price="100.00", category=c2, normalized_name="mc2")
        _make_supplier_product(price="100.00", normalized_name="mc3")
        self.authenticate()
        response = self.client.get(f"{self.url}?category_id={c1.id}&category_id={c2.id}")
        self.assertEqual(response.json()["count"], 2)

    def test_multi_supplier_id(self):
        s1 = Supplier.objects.create(legal_name="S1 LLC", display_name="S1")
        s2 = Supplier.objects.create(legal_name="S2 LLC", display_name="S2")
        _make_supplier_product(supplier=s1, price="100.00", normalized_name="ms1")
        _make_supplier_product(supplier=s2, price="100.00", normalized_name="ms2")
        _make_supplier_product(price="100.00", normalized_name="ms3")
        self.authenticate()
        response = self.client.get(f"{self.url}?supplier_id={s1.id}&supplier_id={s2.id}")
        self.assertEqual(response.json()["count"], 2)

    def test_multi_country(self):
        sp1 = _make_supplier_product(price="100.00", normalized_name="mco1")
        sp2 = _make_supplier_product(price="100.00", normalized_name="mco2")
        _make_supplier_product(price="100.00", normalized_name="mco3")
        SupplierProduct.objects.filter(pk=sp1.pk).update(country_of_origin="Россия")
        SupplierProduct.objects.filter(pk=sp2.pk).update(country_of_origin="Италия")
        self.authenticate()
        response = self.client.get(f"{self.url}?country=Россия&country=Италия")
        self.assertEqual(response.json()["count"], 2)


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogProductAndLogicTests(APITestCase):
    """Проверяет что разные фильтры соединяются через AND."""

    url = "/api/v1/catalog/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_brand_and_availability(self):
        brand = Brand.objects.create(canonical_name="X", normalized_name="x-and")
        sp_match = _make_supplier_product(price="100.00", brand=brand, normalized_name="and-match")
        sp_wrong_avail = _make_supplier_product(price="100.00", brand=brand, normalized_name="and-wrong-avail")
        sp_wrong_brand = _make_supplier_product(price="100.00", normalized_name="and-wrong-brand")
        SupplierProduct.objects.filter(pk=sp_wrong_avail.pk).update(availability=ProductAvailability.OUT_OF_STOCK)
        self.authenticate()
        response = self.client.get(f"{self.url}?brand_id={brand.id}&availability=in_stock")
        ids = [r["id"] for r in response.json()["results"]]
        self.assertIn(str(sp_match.id), ids)
        self.assertNotIn(str(sp_wrong_avail.id), ids)
        self.assertNotIn(str(sp_wrong_brand.id), ids)

    def test_category_and_offers(self):
        cat = ProductCategory.objects.create(name="AndCat", slug="and-cat")
        sp_match = _make_supplier_product(price="100.00", category=cat, normalized_name="and-co-match")
        _make_supplier_product(price="100.00", category=cat, normalized_name="and-co-nooffer")
        sp_no_cat = _make_supplier_product(price="100.00", normalized_name="and-co-nocat")
        SupplierProduct.objects.filter(pk=sp_match.pk).update(is_promo=True)
        SupplierProduct.objects.filter(pk=sp_no_cat.pk).update(is_promo=True)
        self.authenticate()
        response = self.client.get(f"{self.url}?category_id={cat.id}&offers=promo")
        ids = [r["id"] for r in response.json()["results"]]
        self.assertEqual(len(ids), 1)
        self.assertIn(str(sp_match.id), ids)

    def test_brand_and_country(self):
        brand = Brand.objects.create(canonical_name="Y", normalized_name="y-and")
        sp_match = _make_supplier_product(price="100.00", brand=brand, normalized_name="and-bc-match")
        sp_wrong_country = _make_supplier_product(price="100.00", brand=brand, normalized_name="and-bc-wrong")
        SupplierProduct.objects.filter(pk=sp_match.pk).update(country_of_origin="Россия")
        SupplierProduct.objects.filter(pk=sp_wrong_country.pk).update(country_of_origin="Германия")
        self.authenticate()
        response = self.client.get(f"{self.url}?brand_id={brand.id}&country=Россия")
        ids = [r["id"] for r in response.json()["results"]]
        self.assertEqual(len(ids), 1)
        self.assertIn(str(sp_match.id), ids)


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogProductSortTests(APITestCase):
    url = "/api/v1/catalog/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_sort_with_offers_first(self):
        sp_plain = _make_supplier_product(price="100.00", normalized_name="sort-plain")
        sp_promo = _make_supplier_product(price="200.00", normalized_name="sort-promo")
        sp_new = _make_supplier_product(price="300.00", normalized_name="sort-new")
        SupplierProduct.objects.filter(pk=sp_promo.pk).update(is_promo=True)
        SupplierProduct.objects.filter(pk=sp_new.pk).update(is_new=True)
        self.authenticate()
        response = self.client.get(self.url, {"sort": "with_offers"})
        results = response.json()["results"]
        offer_ids = {str(sp_promo.id), str(sp_new.id)}
        plain_id = str(sp_plain.id)
        ids = [r["id"] for r in results]
        # все товары с меткой идут раньше, чем без метки
        last_offer_pos = max(i for i, r in enumerate(results) if r["id"] in offer_ids)
        plain_pos = ids.index(plain_id)
        self.assertLess(last_offer_pos, plain_pos)

    def test_sort_new(self):
        import time

        sp_old = _make_supplier_product(price="100.00", normalized_name="sort-old")
        time.sleep(0.01)
        sp_recent = _make_supplier_product(price="200.00", normalized_name="sort-recent")
        self.authenticate()
        response = self.client.get(self.url, {"sort": "new"})
        ids = [r["id"] for r in response.json()["results"]]
        self.assertLess(ids.index(str(sp_recent.id)), ids.index(str(sp_old.id)))

    def test_sort_name_asc(self):
        _make_supplier_product(price="100.00", normalized_name="zebra")
        _make_supplier_product(price="100.00", normalized_name="apple")
        _make_supplier_product(price="100.00", normalized_name="mango")
        self.authenticate()
        response = self.client.get(self.url, {"sort": "name_asc"})
        names = [r["normalized_name"] for r in response.json()["results"]]
        self.assertEqual(names, sorted(names))

    def test_sort_name_desc(self):
        _make_supplier_product(price="100.00", normalized_name="zebra2")
        _make_supplier_product(price="100.00", normalized_name="apple2")
        _make_supplier_product(price="100.00", normalized_name="mango2")
        self.authenticate()
        response = self.client.get(self.url, {"sort": "name_desc"})
        names = [r["normalized_name"] for r in response.json()["results"]]
        self.assertEqual(names, sorted(names, reverse=True))


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogSupplierSearchTests(APITestCase):
    url = "/api/v1/catalog/suppliers"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_search_by_legal_name(self):
        s = Supplier.objects.create(legal_name="ООО Ромашка", display_name="Ромашка")
        _make_supplier_product(supplier=s, price="100.00")
        self.authenticate()
        response = self.client.get(self.url, {"q": "ромашка"})
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["id"], str(s.id))

    def test_search_no_match_returns_empty(self):
        s = Supplier.objects.create(legal_name="ООО Тюльпан", display_name="Тюльпан")
        _make_supplier_product(supplier=s, price="100.00")
        self.authenticate()
        response = self.client.get(self.url, {"q": "роза"})
        self.assertEqual(response.json(), [])


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogSupplierDetailAPITests(APITestCase):
    """2026-09-15, по прямой просьбе пользователя: карточка поставщика по
    клику на его название в истории заказов заведения."""

    def setUp(self):
        self.supplier = Supplier.objects.create(
            legal_name="ООО Ромашка",
            display_name="Ромашка",
            inn="1234567890",
            logo_url="https://example.com/logo.png",
            description="Молочная продукция",
            delivery_cost="300.00",
        )
        self.url = f"/api/v1/catalog/suppliers/{self.supplier.id}"

    def test_allows_anonymous_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_returns_supplier_fields(self):
        response = self.client.get(self.url)
        data = response.json()
        self.assertEqual(data["id"], str(self.supplier.id))
        self.assertEqual(data["display_name"], "Ромашка")
        self.assertEqual(data["legal_name"], "ООО Ромашка")
        self.assertEqual(data["inn"], "1234567890")
        self.assertEqual(data["logo_url"], "https://example.com/logo.png")
        self.assertEqual(data["description"], "Молочная продукция")
        self.assertEqual(float(data["delivery_cost"]), 300.0)
        self.assertNotIn("bank_accounts", data)
        self.assertNotIn("status", data)
        self.assertNotIn("is_published", data)

    def test_includes_addresses_and_contacts(self):
        SupplierAddress.objects.create(
            supplier=self.supplier, address_type=SupplierAddressType.LEGAL, address="г. Москва, ул. Ленина, 1"
        )
        SupplierContact.objects.create(
            supplier=self.supplier, contact_type=SupplierContactType.PHONE, value="+79990000000"
        )
        response = self.client.get(self.url)
        data = response.json()
        self.assertEqual(data["addresses"][0]["address"], "г. Москва, ул. Ленина, 1")
        self.assertEqual(data["contacts"][0]["value"], "+79990000000")

    def test_returns_unpublished_supplier(self):
        """Заказ мог быть оформлен у уже снятого с публикации поставщика —
        карточка должна открываться так же, как у активного/опубликованного."""
        self.supplier.is_published = False
        self.supplier.save(update_fields=["is_published"])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_returns_404_for_unknown_id(self):
        response = self.client.get(f"/api/v1/catalog/suppliers/{uuid4()}")
        self.assertEqual(response.status_code, 404)


class CatalogProductDetailAPITests(APITestCase):
    """GET /catalog/products/{id} — карточка товара в каталоге: те же поля и та же
    видимость, что у строки списка, плюс галерея фото."""

    def setUp(self):
        self.sp = _make_supplier_product(price="150.00", normalized_name="Молоко 3.2%")
        self.url = f"/api/v1/catalog/products/{self.sp.id}"

    def test_allows_anonymous_access_and_returns_list_row_fields(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        list_row = self.client.get("/api/v1/catalog/products").json()["results"][0]
        card_only = {"images", "short_description", "net_value", "net_unit_id", "net_unit", "package_composition"}
        self.assertEqual(set(data) - card_only, set(list_row))
        self.assertIsNone(data["package_composition"])
        self.assertEqual(data["id"], str(self.sp.id))
        self.assertEqual(data["normalized_name"], "Молоко 3.2%")
        self.assertEqual(float(data["current_price"]), 150.0)
        self.assertIsNone(data["is_favorite"])
        self.assertEqual(data["wishlists"], [])
        self.assertEqual(data["images"], [])

    def test_returns_gallery_ordered_by_position(self):
        SupplierProductImage.objects.create(supplier_product=self.sp, image_url="https://e.com/2.png", position=1)
        SupplierProductImage.objects.create(supplier_product=self.sp, image_url="https://e.com/1.png", position=0)
        images = self.client.get(self.url).json()["images"]
        self.assertEqual([i["image_url"] for i in images], ["https://e.com/1.png", "https://e.com/2.png"])
        self.assertEqual([i["position"] for i in images], [0, 1])

    def test_is_favorite_with_venue_id(self):
        venue = Venue.objects.create(display_name="Test Cafe")
        VenueSupplierProduct.objects.create(
            venue=venue, supplier_product=self.sp, is_favorite=True, status=EntityStatus.ACTIVE
        )
        other_venue = Venue.objects.create(display_name="Other Cafe")
        self.assertTrue(self.client.get(self.url, {"venue_id": str(venue.id)}).json()["is_favorite"])
        self.assertFalse(self.client.get(self.url, {"venue_id": str(other_venue.id)}).json()["is_favorite"])

    def test_wishlists_of_requesting_user_only(self):
        user = _make_venue_user()
        other_user = _make_venue_user(email="other-detail@example.com")
        mine = Wishlist.objects.create(user=user, name="Для бара")
        WishlistItem.objects.create(wishlist=mine, supplier_product=self.sp, status=EntityStatus.ACTIVE)
        others = Wishlist.objects.create(user=other_user, name="Не моё")
        WishlistItem.objects.create(wishlist=others, supplier_product=self.sp, status=EntityStatus.ACTIVE)

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        wishlists = self.client.get(self.url).json()["wishlists"]
        self.assertEqual(wishlists, [{"id": str(mine.id), "name": "Для бара"}])

    def test_invalid_venue_id_returns_400(self):
        response = self.client.get(self.url, {"venue_id": "nope"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("venue_id", response.json())

    def test_returns_404_for_unknown_id(self):
        response = self.client.get(f"/api/v1/catalog/products/{uuid4()}")
        self.assertEqual(response.status_code, 404)
        self.assertIn("detail", response.json())

    def test_returns_404_when_hidden_from_catalog_list(self):
        """Та же видимость, что у списка: архивный товар, товар без цены и товар
        снятого с публикации поставщика в каталоге не открываются."""
        archived = _make_supplier_product(price="10.00", normalized_name="archived")
        archived.supplier_name = "Архивный"
        archived.status = EntityStatus.ARCHIVED
        archived.save(update_fields=["supplier_name", "status"])
        no_price_supplier = Supplier.objects.create(legal_name="No Price LLC", display_name="No Price")
        no_price = _make_supplier_product(supplier=no_price_supplier)
        unpublished_supplier = Supplier.objects.create(
            legal_name="Hidden LLC", display_name="Hidden", is_published=False
        )
        unpublished = _make_supplier_product(supplier=unpublished_supplier, price="10.00")

        for sp in (archived, no_price, unpublished):
            with self.subTest(sp=sp.normalized_name):
                response = self.client.get(f"/api/v1/catalog/products/{sp.id}")
                self.assertEqual(response.status_code, 404)


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogProductResponseFieldTests(APITestCase):
    url = "/api/v1/catalog/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_country_of_origin_in_response(self):
        sp = _make_supplier_product(price="100.00")
        SupplierProduct.objects.filter(pk=sp.pk).update(country_of_origin="Россия")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.json()["results"][0]["country_of_origin"], "Россия")

    def test_country_of_origin_null_when_not_set(self):
        _make_supplier_product(price="100.00")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertIsNone(response.json()["results"][0]["country_of_origin"])

    def test_offer_flags_false_by_default(self):
        _make_supplier_product(price="100.00")
        self.authenticate()
        row = self.client.get(self.url).json()["results"][0]
        self.assertFalse(row["is_promo"])
        self.assertFalse(row["is_discount"])
        self.assertFalse(row["is_new"])


def _photo(name="product.jpg", content_type="image/jpeg", size=1024):
    return SimpleUploadedFile(name, b"\xff" * size, content_type=content_type)


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogPhotoSearchAPITests(APITestCase):
    url = "/api/v1/catalog/products/photo-search"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    @patch("api.v1.catalog.views.products.recognize_product_search_query")
    def test_allows_anonymous_access(self, mock_recognize):
        # Публичный доступ, как и у остального каталога — без авторизации,
        # с реальным фото и мокнутым AI-вызовом, до самого конца флоу.
        mock_recognize.return_value = "сыр"
        response = self.client.post(self.url, {"photo": _photo()}, format="multipart")
        self.assertEqual(response.status_code, 200)

    def test_missing_photo_returns_400(self):
        self.authenticate()
        response = self.client.post(self.url, {}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_oversized_photo_returns_400(self):
        self.authenticate()
        with override_settings(CATALOG_PHOTO_SEARCH_MAX_BYTES=100):
            response = self.client.post(self.url, {"photo": _photo(size=200)}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_disallowed_content_type_returns_400(self):
        self.authenticate()
        response = self.client.post(
            self.url,
            {"photo": _photo(name="price.pdf", content_type="application/pdf")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)

    @patch("api.v1.catalog.views.products.recognize_product_search_query")
    def test_recognized_query_matches_products(self, mock_recognize):
        mock_recognize.return_value = "особый-товар"
        sp = _make_supplier_product(price="100.00")
        sp.normalized_name = "особый-товар"
        sp.save(update_fields=["normalized_name", "updated_at"])
        _make_supplier_product(price="200.00")
        self.authenticate()

        response = self.client.post(self.url, {"photo": _photo()}, format="multipart")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["recognized_query"], "особый-товар")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], str(sp.id))

    @patch("api.v1.catalog.views.products.recognize_product_search_query")
    def test_empty_recognition_returns_no_results_without_matching_everything(self, mock_recognize):
        mock_recognize.return_value = ""
        _make_supplier_product(price="100.00")
        self.authenticate()

        response = self.client.post(self.url, {"photo": _photo()}, format="multipart")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["recognized_query"], "")
        self.assertEqual(data["count"], 0)

    @patch("api.v1.catalog.views.products.recognize_product_search_query")
    def test_provider_error_returns_503(self, mock_recognize):
        mock_recognize.side_effect = PhotoSearchError("Поиск по фото не настроен: нет AI-ключа.")
        self.authenticate()

        response = self.client.post(self.url, {"photo": _photo()}, format="multipart")

        self.assertEqual(response.status_code, 503)

    @patch("api.v1.catalog.views.products.recognize_product_search_query")
    def test_respects_limit_offset(self, mock_recognize):
        mock_recognize.return_value = "паста"
        for i in range(3):
            _make_supplier_product(price=f"{100 + i}.00", normalized_name=f"паста-{i}")
        self.authenticate()

        response = self.client.post(f"{self.url}?limit=2", {"photo": _photo()}, format="multipart")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 2)


# ---------------------------------------------------------------------------
# GET/POST /suppliers/{id}/orders[/accept|/decline] — 2026-08-24, supplier
# side of the venue cart/order flow discussed on the 2026-08-21 sync call
# (docs/wiki/logs/2026-08-21-sync-call.md). Only the part that's actually
# decided: supplier sees orders containing their own items, accepts/declines
# with a mandatory confirmed_delivery_date — no товарная матрица, no
# delivery-cost logic, no partial accept/смена поставщика (Pavel hadn't
# finished designing those as of the call).
# ---------------------------------------------------------------------------


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierOrderAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.supplier_a = Supplier.objects.create(legal_name="A LLC", display_name="Supplier A")
        self.supplier_b = Supplier.objects.create(legal_name="B LLC", display_name="Supplier B")
        self.sp_a = _make_supplier_product(supplier=self.supplier_a, price="100.00", normalized_name="product-a")
        self.sp_b = _make_supplier_product(supplier=self.supplier_b, price="50.00", normalized_name="product-b")
        self.owner = _make_venue_owner(self.venue)
        self.orders_url = f"/api/v1/catalog/venues/{self.venue.id}/orders"
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _submitted_order_with_both_suppliers(self):
        """Builds one Order with items from supplier_a and supplier_b, submitted."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp_a.id), "requested_qty": "2.0"},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp_b.id), "requested_qty": "3.0"},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.client.credentials()
        return order_id

    def _submitted_order_single_supplier(self, supplier_product, qty="1.0"):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(supplier_product.id), "requested_qty": qty},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.client.credentials()
        return order_id

    def orders_url_for(self, supplier):
        return f"/api/v1/catalog/suppliers/{supplier.id}/orders"

    def test_rejects_without_auth(self):
        response = self.client.get(self.orders_url_for(self.supplier_a))
        self.assertEqual(response.status_code, 401)

    def test_rejects_non_member_supplier_user(self):
        other_supplier = Supplier.objects.create(legal_name="C LLC", display_name="Supplier C")
        user = User.objects.create(email="not-member@example.com", full_name="X", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=other_supplier, user=user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.orders_url_for(self.supplier_a))
        self.assertEqual(response.status_code, 403)

    def test_allows_active_member_via_jwt(self):
        user = User.objects.create(email="member@example.com", full_name="X", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier_a, user=user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self._submitted_order_with_both_suppliers()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.orders_url_for(self.supplier_a))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_list_only_shows_orders_with_own_items(self):
        self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        response = self.client.get(self.orders_url_for(self.supplier_b))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_list_items_count_scoped_to_own_items(self):
        self._submitted_order_with_both_suppliers()
        self.authenticate()
        response = self.client.get(self.orders_url_for(self.supplier_a))
        self.assertEqual(response.json()["results"][0]["items_count"], 1)

    def test_filters_by_venue_id(self):
        """2026-09-18, Дима (фронт): "У Поставщика также должен быть такой
        фильтр (тк нужно иногда фильтровать по заведениям)" — заявки этому
        поставщику могут приходить от разных заведений."""
        other_venue = Venue.objects.create(display_name="Other Cafe")
        other_owner = _make_venue_owner(other_venue, email="other-venue-owner@example.com")
        order_id = self._submitted_order_single_supplier(self.sp_a)
        other_cart_url = f"/api/v1/catalog/venues/{other_venue.id}/cart"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other_owner))
        self.client.post(
            f"{other_cart_url}/items",
            {"supplier_product_id": str(self.sp_a.id), "requested_qty": "1.0"},
            format="json",
        )
        self.client.post(
            f"{other_cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        self.client.credentials()

        self.authenticate()
        response = self.client.get(self.orders_url_for(self.supplier_a), {"venue_id": str(self.venue.id)})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["order_id"], order_id)

    def test_list_includes_venue_name(self):
        """2026-09-14, по замечанию пользователя на GET /suppliers/{supplier_id}/orders
        (Swagger, "тоже самое" — тот же класс гэпа, что был на venue-стороне
        2026-09-09): venue_id уже есть, но нет читаемого имени заведения-заказчика."""
        self._submitted_order_with_both_suppliers()
        self.authenticate()
        response = self.client.get(self.orders_url_for(self.supplier_a))
        self.assertEqual(response.json()["results"][0]["venue_name"], self.venue.display_name)

    def test_list_includes_supplier_name(self):
        """2026-09-15, пользователь на supplier_order_retrieve (Swagger):
        "тут нужно добавить название поставщиков" — до этого заявка не
        называла своего же поставщика по имени вообще (только supplier_id
        подразумевался из URL)."""
        self._submitted_order_with_both_suppliers()
        self.authenticate()
        response = self.client.get(self.orders_url_for(self.supplier_a))
        row = response.json()["results"][0]
        self.assertEqual(row["supplier_id"], str(self.supplier_a.id))
        self.assertEqual(row["supplier_name"], self.supplier_a.display_name)

    def test_detail_includes_supplier_name(self):
        order_id = self._submitted_order_with_both_suppliers()
        self.authenticate()
        data = self.client.get(f"{self.orders_url_for(self.supplier_a)}/{order_id}").json()
        self.assertEqual(data["supplier_id"], str(self.supplier_a.id))
        self.assertEqual(data["supplier_name"], self.supplier_a.display_name)

    def test_list_query_count_does_not_grow_with_order_count(self):
        """Страхует select_related("venue") в supplier_orders_queryset — без
        него каждый заказ в списке добавлял бы свой запрос ради venue_name."""
        for _ in range(3):
            self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        with self.assertNumQueries(3):
            response = self.client.get(self.orders_url_for(self.supplier_a))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)

    def test_detail_scopes_items_to_own_supplier(self):
        order_id = self._submitted_order_with_both_suppliers()
        self.authenticate()
        response = self.client.get(f"{self.orders_url_for(self.supplier_a)}/{order_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["requested_name"], "product-a")

    def test_detail_includes_venue_name(self):
        order_id = self._submitted_order_with_both_suppliers()
        self.authenticate()
        response = self.client.get(f"{self.orders_url_for(self.supplier_a)}/{order_id}")
        self.assertEqual(response.json()["venue_name"], self.venue.display_name)

    def test_detail_item_includes_venue_name(self):
        """2026-09-14, по замечанию пользователя: "в items не хватает
        venue_name" на supplier_order_retrieve."""
        order_id = self._submitted_order_with_both_suppliers()
        self.authenticate()
        response = self.client.get(f"{self.orders_url_for(self.supplier_a)}/{order_id}")
        data = response.json()
        self.assertEqual(data["items"][0]["venue_name"], self.venue.display_name)
        self.assertEqual(data["items"][0]["venue_name"], data["venue_name"])

    def test_detail_includes_supplier_groups(self):
        """2026-09-15, пользователь: "куда ты дел supplier_groups из ответа".
        До SupplierOrderRequest деталь поставщика наследовала это поле от
        OrderDetailSerializer — потерялось при переходе на отдельный
        сериализатор. Заявка одного поставщика — группа ровно одна, с теми
        же items, что и плоское items выше."""
        order_id = self._submitted_order_with_both_suppliers()
        self.authenticate()
        data = self.client.get(f"{self.orders_url_for(self.supplier_a)}/{order_id}").json()
        self.assertEqual(len(data["supplier_groups"]), 1)
        group = data["supplier_groups"][0]
        self.assertEqual(group["supplier_id"], str(self.supplier_a.id))
        self.assertEqual(group["supplier_name"], self.supplier_a.display_name)
        self.assertFalse(group["is_off_system"])
        self.assertEqual([item["id"] for item in group["items"]], [item["id"] for item in data["items"]])

    def test_detail_404_when_supplier_has_no_items_in_order(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        response = self.client.get(f"{self.orders_url_for(self.supplier_b)}/{order_id}")
        self.assertEqual(response.status_code, 404)

    def test_list_includes_number_status_and_viewed_at(self):
        """2026-09-15 (raw ТЗ «ЛК Заведения» §3.3/§11/§13): заявка теперь
        персистентная сущность SupplierOrderRequest со своим номером,
        статусом (5 значений) и признаком «Просмотрено» — не вычисляемый
        фильтр Order.items по supplier_id."""
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        response = self.client.get(self.orders_url_for(self.supplier_a))
        row = response.json()["results"][0]
        self.assertTrue(row["number"].startswith("REQ-"))
        self.assertEqual(row["status"], "submitted")
        self.assertIsNone(row["viewed_at"])
        self.assertEqual(row["order_id"], order_id)

    def test_detail_reachable_via_request_id_not_only_order_id(self):
        """2026-09-15, пользователь: "во всех заказах у поставщика не
        открываются заказы, 404". Список отдаёт `id` заявки (её собственный
        pk) и отдельно `order_id` (pk заказа) — фронт, построенный ещё под
        старый контракт (когда список отдавал сами Order и `id` совпадал
        с order_id), вероятно продолжает переходить в деталь по `row["id"]`.
        detail/accept/decline должны резолвиться по ОБОИМ полям."""
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        request_id = self.client.get(self.orders_url_for(self.supplier_a)).json()["results"][0]["id"]
        self.assertNotEqual(request_id, order_id)
        response = self.client.get(f"{self.orders_url_for(self.supplier_a)}/{request_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["order_id"], order_id)

    def test_accept_reachable_via_request_id_not_only_order_id(self):
        self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        request_id = self.client.get(self.orders_url_for(self.supplier_a)).json()["results"][0]["id"]
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{request_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "confirmed")

    def test_detail_marks_viewed_on_first_open_only(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        detail_url = f"{self.orders_url_for(self.supplier_a)}/{order_id}"

        first = self.client.get(detail_url)
        self.assertIsNotNone(first.json()["viewed_at"])
        first_viewed_at = first.json()["viewed_at"]

        second = self.client.get(detail_url)
        self.assertEqual(second.json()["viewed_at"], first_viewed_at)

    def test_list_reflects_viewed_at_after_detail_open(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        self.assertIsNone(self.client.get(self.orders_url_for(self.supplier_a)).json()["results"][0]["viewed_at"])
        self.client.get(f"{self.orders_url_for(self.supplier_a)}/{order_id}")
        self.assertIsNotNone(self.client.get(self.orders_url_for(self.supplier_a)).json()["results"][0]["viewed_at"])

    def test_request_status_becomes_confirmed_when_fully_accepted(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.assertEqual(response.json()["status"], "confirmed")

    def test_request_status_becomes_declined_on_full_decline(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        response = self.client.post(f"{self.orders_url_for(self.supplier_a)}/{order_id}/decline")
        self.assertEqual(response.json()["status"], "declined")
        # 2026-09-22, пользователь: "почему если я отклонил заказ поставщиком
        # статус cancelled когда declined должен быть" — эта позиция была
        # неотличима от cancelled на уровне item.status, хотя заявка уже
        # верно называла себя declined на уровне выше.
        self.assertEqual(response.json()["items"][0]["status"], "declined")

    def test_request_status_becomes_declined_when_accepted_with_all_zero_qty(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        item_id = str(Order.objects.get(pk=order_id).items.get().id)
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE, "items": [{"item_id": item_id, "confirmed_qty": "0"}]},
            format="json",
        )
        self.assertEqual(response.json()["status"], "declined")

    def test_request_status_becomes_partially_confirmed_on_partial_qty(self):
        order_id = self._submitted_order_single_supplier(self.sp_a, qty="10.0")
        item_id = str(Order.objects.get(pk=order_id).items.get().id)
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE, "items": [{"item_id": item_id, "confirmed_qty": "6.0"}]},
            format="json",
        )
        self.assertEqual(response.json()["status"], "partially_confirmed")

    def test_request_status_becomes_partially_confirmed_when_mix_of_items_resolved(self):
        """2 позиции у одного поставщика: одна принята целиком, другая
        отклонена — заявка «Принята частично» (raw ТЗ §11), даже когда ни
        одна отдельная позиция сама по себе не частична."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        other_sp_a = _make_supplier_product_for(self.supplier_a, "product-a-2", "20.00")
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp_a.id), "requested_qty": "1.0"}, format="json"
        )
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(other_sp_a.id), "requested_qty": "1.0"},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        item_a = Order.objects.get(pk=order_id).items.get(supplier_product=other_sp_a)
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {
                "confirmed_delivery_date": _TEST_DELIVERY_DATE,
                "items": [{"item_id": str(item_a.id), "confirmed_qty": "0"}],
            },
            format="json",
        )
        self.assertEqual(response.json()["status"], "partially_confirmed")

    def test_request_status_becomes_completed_when_order_completed(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(f"/api/v1/catalog/venues/{self.venue.id}/orders/{order_id}/complete")
        self.authenticate()
        response = self.client.get(f"{self.orders_url_for(self.supplier_a)}/{order_id}")
        self.assertEqual(response.json()["status"], "completed")

    def test_declined_request_not_marked_completed_when_order_completed(self):
        """Заказ можно завершить, только когда он confirmed (требует хотя бы
        одну confirmed-позицию где-то в заказе) — declined-заявка ДРУГОГО
        поставщика в том же заказе не должна стать completed заодно."""
        order_id = self._submitted_order_with_both_suppliers()
        self.authenticate()
        self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.client.post(f"{self.orders_url_for(self.supplier_b)}/{order_id}/decline")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(f"/api/v1/catalog/venues/{self.venue.id}/orders/{order_id}/complete")
        self.authenticate()
        response_a = self.client.get(f"{self.orders_url_for(self.supplier_a)}/{order_id}")
        response_b = self.client.get(f"{self.orders_url_for(self.supplier_b)}/{order_id}")
        self.assertEqual(response_a.json()["status"], "completed")
        self.assertEqual(response_b.json()["status"], "declined")

    def test_accept_requires_confirmed_delivery_date(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        response = self.client.post(f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept", {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_accept_confirms_own_items_and_leaves_other_supplier_pending(self):
        order_id = self._submitted_order_with_both_suppliers()
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["status"], "confirmed")
        self.assertEqual(item["confirmed_qty"], "2.0000")
        self.assertEqual(item["confirmed_delivery_date"], _TEST_DELIVERY_DATE)

        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.status, OrderStatus.SUBMITTED)
        item_b = order.items.get(supplier_product=self.sp_b)
        self.assertEqual(item_b.status, "submitted")

    def test_order_status_advances_to_confirmed_when_single_supplier_accepts(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.status, OrderStatus.CONFIRMED)

    def test_order_status_advances_to_confirmed_when_one_accepts_and_other_declines(self):
        order_id = self._submitted_order_with_both_suppliers()
        self.authenticate()
        self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        response = self.client.post(f"{self.orders_url_for(self.supplier_b)}/{order_id}/decline")
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.items.get(supplier_product=self.sp_b).status, "cancelled")

    def test_order_status_becomes_cancelled_when_all_suppliers_decline(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        self.client.post(f"{self.orders_url_for(self.supplier_a)}/{order_id}/decline")
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    def test_accept_twice_fails_second_time(self):
        order_id = self._submitted_order_single_supplier(self.sp_a)
        self.authenticate()
        self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.assertEqual(response.status_code, 409)

    def test_accept_partial_confirms_less_than_requested(self):
        order_id = self._submitted_order_single_supplier(self.sp_a, qty="10.0")
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {
                "confirmed_delivery_date": _TEST_DELIVERY_DATE,
                "items": [{"item_id": str(item_id), "confirmed_qty": "4.0"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["status"], "partially_confirmed")
        self.assertEqual(item["confirmed_qty"], "4.0000")
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.status, OrderStatus.CONFIRMED)

    def test_accept_with_zero_qty_declines_just_that_item(self):
        order_id = self._submitted_order_single_supplier(self.sp_a, qty="10.0")
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {
                "confirmed_delivery_date": _TEST_DELIVERY_DATE,
                "items": [{"item_id": str(item_id), "confirmed_qty": "0"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["status"], "declined")
        self.assertIsNone(item["confirmed_qty"])

    def test_accept_comment_saved_on_request(self):
        order_id = self._submitted_order_single_supplier(self.sp_a, qty="10.0")
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE, "comment": "Задержка поставки на 1 день"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["supplier_comment"], "Задержка поставки на 1 день")
        # 2026-09-22: отдельное поле от comment на детали заказа (venue's own
        # note, set at cart submit time) — ничего не подставил на этот заказ.
        self.assertIsNone(response.json()["comment"])

    def test_accept_comment_optional(self):
        order_id = self._submitted_order_single_supplier(self.sp_a, qty="10.0")
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["supplier_comment"])

    def test_order_item_includes_min_order_qty_and_delivery_multiple(self):
        """2026-09-09 (кабинет поставщика, "Принять частично"): фронту нужна
        кратность/минимальное количество для степпера фактического
        количества в модалке — было только на CartItemSerializer (до
        отправки), на уже отправленной заявке отсутствовало."""
        self.sp_a.min_order_qty = "2.0000"
        self.sp_a.delivery_multiple = "5.0000"
        self.sp_a.save(update_fields=["min_order_qty", "delivery_multiple"])
        order_id = self._submitted_order_single_supplier(self.sp_a, qty="10.0")
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        item = response.json()["items"][0]
        self.assertEqual(float(item["min_order_qty"]), 2.0)
        self.assertEqual(float(item["delivery_multiple"]), 5.0)

    def test_accept_items_unlisted_item_still_gets_full_qty(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp_a.id), "requested_qty": "3.0"}, format="json"
        )
        sp_a2 = SupplierProduct.objects.create(
            supplier=self.supplier_a,
            supplier_name="Тестовый товар 2",
            normalized_name="product-a2",
            unit=self.sp_a.unit,
            status=EntityStatus.ACTIVE,
        )
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(sp_a2.id), "requested_qty": "5.0"}, format="json"
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate()
        order = Order.objects.get(pk=order_id)
        item_a = order.items.get(supplier_product=self.sp_a)
        item_a2 = order.items.get(supplier_product=sp_a2)
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {
                "confirmed_delivery_date": _TEST_DELIVERY_DATE,
                "items": [{"item_id": str(item_a.id), "confirmed_qty": "1.0"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        by_id = {row["id"]: row for row in response.json()["items"]}
        self.assertEqual(by_id[str(item_a.id)]["confirmed_qty"], "1.0000")
        self.assertEqual(by_id[str(item_a2.id)]["confirmed_qty"], "5.0000")
        self.assertEqual(by_id[str(item_a2.id)]["status"], "confirmed")

    def test_accept_items_confirmed_qty_exceeds_requested_rejected(self):
        order_id = self._submitted_order_single_supplier(self.sp_a, qty="2.0")
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {
                "confirmed_delivery_date": _TEST_DELIVERY_DATE,
                "items": [{"item_id": str(item_id), "confirmed_qty": "5.0"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_accept_items_unknown_item_id_rejected(self):
        order_id = self._submitted_order_single_supplier(self.sp_a, qty="2.0")
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url_for(self.supplier_a)}/{order_id}/accept",
            {
                "confirmed_delivery_date": _TEST_DELIVERY_DATE,
                "items": [{"item_id": str(uuid.uuid4()), "confirmed_qty": "1.0"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueOrderItemChangeSupplierAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.supplier_a = Supplier.objects.create(legal_name="A LLC", display_name="Supplier A")
        self.supplier_c = Supplier.objects.create(legal_name="C LLC", display_name="Supplier C")
        self.sp_a = _make_supplier_product(supplier=self.supplier_a, price="100.00", normalized_name="product-a")
        self.sp_c = _make_supplier_product(supplier=self.supplier_c, price="90.00", normalized_name="product-c")
        self.owner = _make_venue_owner(self.venue)
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _submitted_order(self, qty="10.0"):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp_a.id), "requested_qty": qty}, format="json"
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.client.credentials()
        return order_id, item_id

    def change_supplier_url(self, order_id, item_id):
        return f"/api/v1/catalog/venues/{self.venue.id}/orders/{order_id}/items/{item_id}/change-supplier"

    def test_change_supplier_after_decline_creates_new_submitted_item(self):
        order_id, item_id = self._submitted_order(qty="10.0")
        self.authenticate()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        response = self.client.post(
            self.change_supplier_url(order_id, item_id), {"supplier_product_id": str(self.sp_c.id)}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        items = {row["supplier_id"]: row for row in data["items"]}
        self.assertEqual(items[str(self.supplier_a.id)]["status"], "replaced")
        new_item = items[str(self.supplier_c.id)]
        self.assertEqual(new_item["status"], "submitted")
        self.assertEqual(new_item["requested_qty"], "10.0000")
        self.assertEqual(new_item["replaces_id"], str(item_id))
        self.assertEqual(data["status"], "submitted")
        # 2026-09-21: the original item keeps its own decision moment even
        # after being marked replaced — the reroute isn't a new supplier
        # response, it's a bookkeeping move on the OLD item.
        self.assertIsNotNone(items[str(self.supplier_a.id)]["supplier_responded_at"])
        self.assertIsNone(new_item["supplier_responded_at"])

    def test_change_supplier_after_partial_accept_uses_remainder_qty(self):
        order_id, item_id = self._submitted_order(qty="10.0")
        self.authenticate()
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/accept",
            {
                "confirmed_delivery_date": _TEST_DELIVERY_DATE,
                "items": [{"item_id": str(item_id), "confirmed_qty": "4.0"}],
            },
            format="json",
        )
        response = self.client.post(
            self.change_supplier_url(order_id, item_id), {"supplier_product_id": str(self.sp_c.id)}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        items = {row["supplier_id"]: row for row in response.json()["items"]}
        self.assertEqual(items[str(self.supplier_a.id)]["status"], "replaced")
        self.assertEqual(items[str(self.supplier_a.id)]["confirmed_qty"], "4.0000")
        self.assertEqual(items[str(self.supplier_c.id)]["requested_qty"], "6.0000")

    def test_change_supplier_rejects_fully_confirmed_item(self):
        order_id, item_id = self._submitted_order(qty="10.0")
        self.authenticate()
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        response = self.client.post(
            self.change_supplier_url(order_id, item_id), {"supplier_product_id": str(self.sp_c.id)}, format="json"
        )
        self.assertEqual(response.status_code, 409)

    def test_change_supplier_rejects_same_supplier_product(self):
        order_id, item_id = self._submitted_order(qty="10.0")
        self.authenticate()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        response = self.client.post(
            self.change_supplier_url(order_id, item_id), {"supplier_product_id": str(self.sp_a.id)}, format="json"
        )
        self.assertEqual(response.status_code, 409)

    def test_change_supplier_rejects_without_auth(self):
        order_id, item_id = self._submitted_order(qty="10.0")
        self.authenticate()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.client.credentials()
        response = self.client.post(
            self.change_supplier_url(order_id, item_id), {"supplier_product_id": str(self.sp_c.id)}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_change_supplier_creates_request_for_new_supplier_and_declines_old(self):
        """2026-09-15 (raw ТЗ «ЛК Заведения» §19 «Найти замену»): «смена
        поставщика» — по сути замена товара, новый поставщик определяется
        выбранным supplier_product_id. У нового поставщика должна появиться
        собственная SupplierOrderRequest (submitted), а у старого — его
        заявка пересчитывается в declined (весь остаток заменён)."""
        order_id, item_id = self._submitted_order(qty="10.0")
        self.authenticate()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.client.post(
            self.change_supplier_url(order_id, item_id), {"supplier_product_id": str(self.sp_c.id)}, format="json"
        )
        old_request = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}")
        new_request = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier_c.id}/orders/{order_id}")
        self.assertEqual(old_request.json()["status"], "declined")
        self.assertEqual(new_request.json()["status"], "submitted")
        self.assertTrue(new_request.json()["number"].startswith("REQ-"))


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueOrderCancelAPITests(APITestCase):
    """2026-09-15 (raw ТЗ «ЛК Заведения» §20-23): заведение отменяет
    заявку/позицию до ответа поставщика — отдельно от отказа поставщика
    (cancelled_by_venue, не cancelled), не попадает в «Неразобранные»."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.supplier_a = Supplier.objects.create(legal_name="A LLC", display_name="Supplier A")
        self.supplier_b = Supplier.objects.create(legal_name="B LLC", display_name="Supplier B")
        self.sp_a = _make_supplier_product(supplier=self.supplier_a, price="100.00", normalized_name="product-a")
        self.sp_b = _make_supplier_product(supplier=self.supplier_b, price="50.00", normalized_name="product-b")
        self.owner = _make_venue_owner(self.venue)
        self.orders_url = f"/api/v1/catalog/venues/{self.venue.id}/orders"
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def authenticate_supplier(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _submitted_order(self, supplier_products_qty):
        self.authenticate()
        for sp, qty in supplier_products_qty:
            self.client.post(
                f"{self.cart_url}/items", {"supplier_product_id": str(sp.id), "requested_qty": qty}, format="json"
            )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        order_id = response.json()["id"]
        self.authenticate()
        return order_id

    def test_cancel_item_sets_cancelled_by_venue_not_cancelled(self):
        order_id = self._submitted_order([(self.sp_a, "5.0")])
        item_id = Order.objects.get(pk=order_id).items.get().id
        response = self.client.post(
            f"{self.orders_url}/{order_id}/items/{item_id}/cancel", {"reason": "Больше не нужно"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["status"], "cancelled_by_venue")
        self.assertEqual(data["cancellation_reason"], "Больше не нужно")
        # 2026-09-21: venue action, not the supplier's — never sets this.
        self.assertIsNone(data["supplier_responded_at"])

    def test_cancel_item_reason_is_optional(self):
        order_id = self._submitted_order([(self.sp_a, "5.0")])
        item_id = Order.objects.get(pk=order_id).items.get().id
        response = self.client.post(f"{self.orders_url}/{order_id}/items/{item_id}/cancel", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["cancellation_reason"])

    def test_cancel_item_rejected_once_supplier_responded(self):
        order_id = self._submitted_order([(self.sp_a, "5.0")])
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.authenticate_supplier()
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.authenticate()
        response = self.client.post(f"{self.orders_url}/{order_id}/items/{item_id}/cancel", {}, format="json")
        self.assertEqual(response.status_code, 409)

    def test_cancel_item_does_not_affect_other_suppliers_items(self):
        order_id = self._submitted_order([(self.sp_a, "5.0"), (self.sp_b, "3.0")])
        item_a = Order.objects.get(pk=order_id).items.get(supplier_product=self.sp_a)
        self.client.post(f"{self.orders_url}/{order_id}/items/{item_a.id}/cancel", {}, format="json")
        item_b = Order.objects.get(pk=order_id).items.get(supplier_product=self.sp_b)
        self.assertEqual(item_b.status, "submitted")

    def test_cancelled_item_not_eligible_for_find_replacement(self):
        """§21: заведение отменило — потребность отменена, не нужен
        поиск замены (в отличие от отказа поставщика)."""
        order_id = self._submitted_order([(self.sp_a, "5.0")])
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.client.post(f"{self.orders_url}/{order_id}/items/{item_id}/cancel", {}, format="json")
        response = self.client.post(
            f"{self.orders_url}/{order_id}/items/{item_id}/change-supplier",
            {"supplier_product_id": str(self.sp_b.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 409)

    def test_cancel_supplier_request_cancels_all_its_pending_items(self):
        self.authenticate()
        other_sp_a = _make_supplier_product_for(self.supplier_a, "product-a-2", "20.00")
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp_a.id), "requested_qty": "1.0"}, format="json"
        )
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(other_sp_a.id), "requested_qty": "1.0"},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate()
        response = self.client.post(
            f"{self.orders_url}/{order_id}/suppliers/{self.supplier_a.id}/cancel", {}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        items = Order.objects.get(pk=order_id).items.filter(supplier_product__supplier=self.supplier_a)
        self.assertTrue(all(item.status == "cancelled_by_venue" for item in items))

        self.authenticate_supplier()
        request_status = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}").json()[
            "status"
        ]
        self.assertEqual(request_status, "declined")

    def test_cancel_supplier_request_404_for_unknown_supplier(self):
        order_id = self._submitted_order([(self.sp_a, "5.0")])
        response = self.client.post(
            f"{self.orders_url}/{order_id}/suppliers/{self.supplier_b.id}/cancel", {}, format="json"
        )
        self.assertEqual(response.status_code, 404)

    def test_cancel_rejects_without_auth(self):
        order_id = self._submitted_order([(self.sp_a, "5.0")])
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.client.credentials()
        response = self.client.post(f"{self.orders_url}/{order_id}/items/{item_id}/cancel", {}, format="json")
        self.assertEqual(response.status_code, 401)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueUnresolvedOrderItemsAPITests(APITestCase):
    """2026-09-15 (raw ТЗ §3.5/§16-18 «Неразобранные»)."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.supplier_a = Supplier.objects.create(legal_name="A LLC", display_name="Supplier A")
        self.sp_a = _make_supplier_product(supplier=self.supplier_a, price="100.00", normalized_name="product-a")
        self.owner = _make_venue_owner(self.venue)
        self.orders_url = f"/api/v1/catalog/venues/{self.venue.id}/orders"
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"
        self.unresolved_url = f"{self.orders_url}/unresolved-items"
        self.unresolved_count_url = f"{self.orders_url}/unresolved-items/count"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def authenticate_supplier(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _submitted_order(self, qty="5.0"):
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp_a.id), "requested_qty": qty}, format="json"
        )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        order_id = response.json()["id"]
        self.authenticate()
        return order_id

    def test_includes_fully_declined_item(self):
        order_id = self._submitted_order(qty="10.0")
        self.authenticate_supplier()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.authenticate()
        response = self.client.get(self.unresolved_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        row = response.json()["results"][0]
        self.assertEqual(row["status"], "declined")
        self.assertEqual(float(row["remaining_qty"]), 10.0)
        self.assertIsNotNone(row["supplier_responded_at"])

    def test_includes_partially_confirmed_item(self):
        order_id = self._submitted_order(qty="10.0")
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.authenticate_supplier()
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/accept",
            {
                "confirmed_delivery_date": _TEST_DELIVERY_DATE,
                "comment": "Осталось на складе только 6 шт.",
                "items": [{"item_id": str(item_id), "confirmed_qty": "6.0"}],
            },
            format="json",
        )
        self.authenticate()
        response = self.client.get(self.unresolved_url)
        self.assertEqual(response.json()["count"], 1)
        row = response.json()["results"][0]
        self.assertEqual(row["confirmed_qty"], "6.0000")
        self.assertEqual(row["requested_qty"], "10.0000")
        self.assertEqual(float(row["remaining_qty"]), 4.0)
        self.assertEqual(row["status"], "partially_confirmed")
        self.assertIsNotNone(row["supplier_responded_at"])
        # 2026-09-22, Дима: "к частично принятым товарам нужно подтягивать
        # коммент из заявки" — раньше комментарий, оставленный на accept,
        # был виден только в ответе самого accept, не на "Неразобранных".
        self.assertEqual(row["supplier_comment"], "Осталось на складе только 6 шт.")

    def test_remaining_qty_zero_when_fully_confirmed(self):
        order_id = self._submitted_order(qty="10.0")
        self.authenticate_supplier()
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE, "comment": "Всё в порядке"},
            format="json",
        )
        self.authenticate()
        row = _grouped_order_items(self.client.get(f"{self.orders_url}/{order_id}").json())[0]
        self.assertEqual(float(row["remaining_qty"]), 0.0)
        # 2026-09-22, Дима: комментарий заявки эхом на каждой её позиции —
        # не только на частично принятых (это общий комментарий на весь
        # вызов accept, а не признак частичности).
        self.assertEqual(row["supplier_comment"], "Всё в порядке")
        self.assertEqual(row["status"], "confirmed")
        self.assertIsNotNone(row["supplier_responded_at"])

    def test_remaining_qty_null_while_not_yet_answered(self):
        order_id = self._submitted_order(qty="10.0")
        row = _grouped_order_items(self.client.get(f"{self.orders_url}/{order_id}").json())[0]
        self.assertEqual(row["status"], "submitted")
        self.assertIsNone(row["remaining_qty"])
        self.assertIsNone(row["supplier_responded_at"])
        self.assertIsNone(row["supplier_comment"])

    def test_excludes_fully_confirmed_item(self):
        order_id = self._submitted_order()
        self.authenticate_supplier()
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.authenticate()
        response = self.client.get(self.unresolved_url)
        self.assertEqual(response.json()["count"], 0)

    def test_excludes_item_cancelled_by_venue(self):
        """§23: заведение отменило само — не «Неразобранные»."""
        order_id = self._submitted_order()
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.client.post(f"{self.orders_url}/{order_id}/items/{item_id}/cancel", {}, format="json")
        response = self.client.get(self.unresolved_url)
        self.assertEqual(response.json()["count"], 0)

    def test_excludes_replaced_item(self):
        """§19: остаток уже переотправлен через «Найти замену» — только
        НОВАЯ позиция может позже стать «Неразобранной», не старая."""
        other_supplier = Supplier.objects.create(legal_name="C LLC", display_name="Supplier C")
        other_sp = _make_supplier_product(supplier=other_supplier, price="80.00", normalized_name="product-c")
        order_id = self._submitted_order()
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.authenticate_supplier()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.authenticate()
        self.client.post(
            f"{self.orders_url}/{order_id}/items/{item_id}/change-supplier",
            {"supplier_product_id": str(other_sp.id)},
            format="json",
        )
        response = self.client.get(self.unresolved_url)
        self.assertEqual(response.json()["count"], 0)

    def test_scoped_to_venue(self):
        other_venue = Venue.objects.create(display_name="Other Cafe")
        other_owner = _make_venue_owner(other_venue, email="other-venue-owner@example.com")
        other_cart_url = f"/api/v1/catalog/venues/{other_venue.id}/cart"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other_owner))
        self.client.post(
            f"{other_cart_url}/items", {"supplier_product_id": str(self.sp_a.id), "requested_qty": "1.0"}, format="json"
        )
        other_order_id = self.client.post(
            f"{other_cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate_supplier()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{other_order_id}/decline")

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other_owner))
        response = self.client.get(f"/api/v1/catalog/venues/{other_venue.id}/orders/unresolved-items")
        self.assertEqual(response.json()["count"], 1)
        self.authenticate()
        response = self.client.get(self.unresolved_url)
        self.assertEqual(response.json()["count"], 0)

    def test_rejects_without_auth(self):
        response = self.client.get(self.unresolved_url)
        self.assertEqual(response.status_code, 401)

    def test_count_matches_list_count_for_multiple_unresolved_items(self):
        """2026-09-18, пользователь: "нужна ручка получения кол-ва не
        разобранных заказов по id заведения" — лёгкий счётчик для бейджа,
        без похода за самим списком. Считает позиции (2 неразобранные
        позиции → count=2), не заказы."""
        order_id = self._submitted_order(qty="10.0")
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp_a.id), "requested_qty": "5.0"}, format="json"
        )
        other_order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate_supplier()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{other_order_id}/decline")
        self.authenticate()

        count_response = self.client.get(self.unresolved_count_url)
        self.assertEqual(count_response.status_code, 200)
        self.assertEqual(count_response.json(), {"count": 2})
        self.assertEqual(count_response.json()["count"], self.client.get(self.unresolved_url).json()["count"])

    def test_count_zero_when_nothing_unresolved(self):
        self.authenticate()
        response = self.client.get(self.unresolved_count_url)
        self.assertEqual(response.json(), {"count": 0})

    def test_count_scoped_to_venue(self):
        other_venue = Venue.objects.create(display_name="Other Cafe")
        other_owner = _make_venue_owner(other_venue, email="other-venue-owner-count@example.com")
        other_cart_url = f"/api/v1/catalog/venues/{other_venue.id}/cart"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other_owner))
        self.client.post(
            f"{other_cart_url}/items", {"supplier_product_id": str(self.sp_a.id), "requested_qty": "1.0"}, format="json"
        )
        other_order_id = self.client.post(
            f"{other_cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate_supplier()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{other_order_id}/decline")

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other_owner))
        response = self.client.get(f"/api/v1/catalog/venues/{other_venue.id}/orders/unresolved-items/count")
        self.assertEqual(response.json(), {"count": 1})
        self.authenticate()
        self.assertEqual(self.client.get(self.unresolved_count_url).json(), {"count": 0})

    def test_count_rejects_without_auth(self):
        response = self.client.get(self.unresolved_count_url)
        self.assertEqual(response.status_code, 401)

    def test_query_count_does_not_grow_with_item_count(self):
        for _ in range(3):
            order_id = self._submitted_order()
            self.authenticate_supplier()
            self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.authenticate()
        with self.assertNumQueries(5):
            response = self.client.get(self.unresolved_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)

    def test_filters_by_search_query(self):
        """2026-09-21, по просьбе пользователя: поиск по названию товара."""
        apples = _make_supplier_product_for(self.supplier_a, "apples", "50.00")
        order_id = self._submitted_order()
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(apples.id), "requested_qty": "1.0"}, format="json"
        )
        other_order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate_supplier()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{other_order_id}/decline")
        self.authenticate()

        response = self.client.get(self.unresolved_url, {"q": "appl"})
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["requested_name"], "apples")

    def test_filters_by_subdivision_id(self):
        """2026-09-21, по просьбе пользователя: фильтр по подразделению."""
        kitchen = VenueSubdivision.objects.create(venue=self.venue, name="Кухня")
        bar = VenueSubdivision.objects.create(venue=self.venue, name="Бар")
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp_a.id), "requested_qty": "1.0", "subdivision_id": str(kitchen.id)},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        self.authenticate_supplier()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.authenticate()

        response = self.client.get(self.unresolved_url, {"subdivision_id": str(kitchen.id)})
        self.assertEqual(response.json()["count"], 1)

        response = self.client.get(self.unresolved_url, {"subdivision_id": str(bar.id)})
        self.assertEqual(response.json()["count"], 0)

    def test_rejects_invalid_subdivision_id(self):
        self.authenticate()
        response = self.client.get(self.unresolved_url, {"subdivision_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 400)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueSupplierOrderRequestAuditLogAPITests(APITestCase):
    """2026-09-15 (raw ТЗ §29 «История»): submitted/viewed/confirmed/
    partially_confirmed/declined/cancelled_by_venue/completed для заявки,
    плюс item-уровневая cancelled_by_venue — всё в одной хронологии."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.supplier_a = Supplier.objects.create(legal_name="A LLC", display_name="Supplier A")
        self.sp_a = _make_supplier_product(supplier=self.supplier_a, price="100.00", normalized_name="product-a")
        self.owner = _make_venue_owner(self.venue)
        self.orders_url = f"/api/v1/catalog/venues/{self.venue.id}/orders"
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def authenticate_supplier(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _submitted_order(self, qty="5.0"):
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp_a.id), "requested_qty": qty}, format="json"
        )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        order_id = response.json()["id"]
        self.authenticate()
        return order_id

    def history_url(self, order_id):
        return f"{self.orders_url}/{order_id}/suppliers/{self.supplier_a.id}/history"

    def test_shows_submitted_after_submit(self):
        order_id = self._submitted_order()
        response = self.client.get(self.history_url(order_id))
        self.assertEqual(response.status_code, 200)
        actions = [row["action"] for row in response.json()]
        self.assertIn("submitted", actions)

    def test_shows_viewed_after_supplier_opens_detail(self):
        order_id = self._submitted_order()
        self.authenticate_supplier()
        self.client.get(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}")
        self.authenticate()
        actions = [row["action"] for row in self.client.get(self.history_url(order_id)).json()]
        self.assertIn("viewed", actions)

    def test_viewed_logged_only_once_on_repeat_opens(self):
        order_id = self._submitted_order()
        self.authenticate_supplier()
        detail_url = f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}"
        self.client.get(detail_url)
        self.client.get(detail_url)
        self.authenticate()
        actions = [row["action"] for row in self.client.get(self.history_url(order_id)).json()]
        self.assertEqual(actions.count("viewed"), 1)

    def test_shows_confirmed_after_full_accept(self):
        order_id = self._submitted_order()
        self.authenticate_supplier()
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.authenticate()
        actions = [row["action"] for row in self.client.get(self.history_url(order_id)).json()]
        self.assertIn("confirmed", actions)

    def test_shows_declined_after_decline(self):
        order_id = self._submitted_order()
        self.authenticate_supplier()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.authenticate()
        actions = [row["action"] for row in self.client.get(self.history_url(order_id)).json()]
        self.assertIn("declined", actions)

    def test_shows_cancelled_by_venue_for_item_cancel(self):
        order_id = self._submitted_order()
        item_id = Order.objects.get(pk=order_id).items.get().id
        self.client.post(f"{self.orders_url}/{order_id}/items/{item_id}/cancel", {}, format="json")
        actions = [row["action"] for row in self.client.get(self.history_url(order_id)).json()]
        self.assertIn("cancelled_by_venue", actions)

    def test_shows_completed_after_order_completed(self):
        order_id = self._submitted_order()
        self.authenticate_supplier()
        self.client.post(
            f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.authenticate()
        self.client.post(f"{self.orders_url}/{order_id}/complete")
        actions = [row["action"] for row in self.client.get(self.history_url(order_id)).json()]
        self.assertIn("completed", actions)

    def test_declined_request_not_logged_completed(self):
        order_id = self._submitted_order()
        self.authenticate_supplier()
        self.client.post(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/orders/{order_id}/decline")
        self.authenticate()
        actions = [row["action"] for row in self.client.get(self.history_url(order_id)).json()]
        self.assertNotIn("completed", actions)

    def test_rejects_without_auth(self):
        order_id = self._submitted_order()
        self.client.credentials()
        response = self.client.get(self.history_url(order_id))
        self.assertEqual(response.status_code, 401)


class SupplierOrderRequestBackfillMigrationTests(TestCase):
    """2026-09-15: core.0027-0029 added SupplierOrderRequest, but only
    OrderItem creation (submit_venue_cart) wired the new FK — every Order
    submitted before this migration's own deploy had items with
    supplier_product set but supplier_request left null, so GET
    /suppliers/{id}/orders/{order_id} (looks up SupplierOrderRequest by
    order_id+supplier_id) 404s for every pre-existing order. Confirmed
    live on dev via VictoriaLogs — reported by the user directly.
    core.0030 is a RunPython data migration backfilling one request per
    (order, supplier) for orphaned items, deriving status from CURRENT
    item state. Since manage.py test always runs the full migration chain
    (0030 included) before any test starts, this can't test "pre-0030
    state" through the ORM directly — it imports the migration's own
    function and re-runs it against orphan rows built by hand (status set
    directly via .update(), bypassing services.py, to simulate exactly
    what the old, pre-migration submit_venue_cart left behind)."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.supplier = Supplier.objects.create(legal_name="A LLC", display_name="Supplier A")
        self.sp = _make_supplier_product(supplier=self.supplier, price="100.00", normalized_name="product-a")

    def _run_backfill(self):
        import importlib

        from django.apps import apps as django_apps

        module = importlib.import_module("core.migrations.0030_backfill_supplier_order_requests")
        module.backfill_supplier_order_requests(django_apps, None)

    def _make_orphan_item(self, *, order_status, item_status, confirmed_qty=None, requested_qty="5.0"):
        order = Order.objects.create(
            venue=self.venue, order_number=f"ORD-TEST-{uuid.uuid4().hex[:8].upper()}", status=order_status
        )
        item = OrderItem.objects.create(
            order=order,
            supplier_product=self.sp,
            requested_name="Тест",
            requested_qty=Decimal(requested_qty),
            requested_unit=Unit.objects.filter(code="шт").first(),
        )
        # bypass services.py entirely — status/confirmed_qty set directly,
        # exactly what pre-migration data looked like (supplier_request
        # left at its model default: null)
        OrderItem.objects.filter(pk=item.pk).update(
            status=item_status, confirmed_qty=Decimal(confirmed_qty) if confirmed_qty else None
        )
        item.refresh_from_db()
        return order, item

    def test_backfills_submitted_order(self):
        _order, item = self._make_orphan_item(order_status="submitted", item_status="submitted")
        self._run_backfill()
        item.refresh_from_db()
        self.assertIsNotNone(item.supplier_request_id)
        self.assertEqual(item.supplier_request.status, "submitted")
        self.assertTrue(item.supplier_request.number.startswith("REQ-"))

    def test_backfills_fully_confirmed_order(self):
        _order, item = self._make_orphan_item(order_status="confirmed", item_status="confirmed", confirmed_qty="5.0")
        self._run_backfill()
        item.refresh_from_db()
        self.assertEqual(item.supplier_request.status, "confirmed")

    def test_backfills_partially_confirmed_order(self):
        _order, item = self._make_orphan_item(
            order_status="confirmed", item_status="confirmed", confirmed_qty="3.0", requested_qty="5.0"
        )
        self._run_backfill()
        item.refresh_from_db()
        self.assertEqual(item.supplier_request.status, "partially_confirmed")

    def test_backfills_declined_order(self):
        _order, item = self._make_orphan_item(order_status="cancelled", item_status="cancelled")
        self._run_backfill()
        item.refresh_from_db()
        self.assertEqual(item.supplier_request.status, "declined")

    def test_backfills_completed_order_as_completed_not_confirmed(self):
        _order, item = self._make_orphan_item(order_status="completed", item_status="confirmed", confirmed_qty="5.0")
        self._run_backfill()
        item.refresh_from_db()
        self.assertEqual(item.supplier_request.status, "completed")

    def test_declined_stays_declined_even_if_order_completed(self):
        """Edge case that shouldn't occur in practice (an order can't
        reach `completed` unless it was `confirmed` first, which requires
        at least one non-declined item somewhere) — still, the backfill
        must never promote a fully-declined заявка to completed."""
        _order, item = self._make_orphan_item(order_status="completed", item_status="cancelled")
        self._run_backfill()
        item.refresh_from_db()
        self.assertEqual(item.supplier_request.status, "declined")

    def test_skips_items_already_linked(self):
        order, item = self._make_orphan_item(order_status="submitted", item_status="submitted")
        existing = SupplierOrderRequest.objects.create(order=order, supplier=self.supplier, number="REQ-EXISTING01")
        OrderItem.objects.filter(pk=item.pk).update(supplier_request=existing)
        self._run_backfill()
        self.assertEqual(SupplierOrderRequest.objects.filter(order=order).count(), 1)

    def test_groups_by_supplier_within_one_order(self):
        order = Order.objects.create(
            venue=self.venue, order_number=f"ORD-TEST-{uuid.uuid4().hex[:8].upper()}", status="submitted"
        )
        other_supplier = Supplier.objects.create(legal_name="B LLC", display_name="Supplier B")
        other_sp = _make_supplier_product(supplier=other_supplier, price="50.00", normalized_name="product-b")
        unit = Unit.objects.filter(code="шт").first()
        item_a = OrderItem.objects.create(
            order=order, supplier_product=self.sp, requested_name="A", requested_qty=Decimal("1.0"), requested_unit=unit
        )
        item_b = OrderItem.objects.create(
            order=order,
            supplier_product=other_sp,
            requested_name="B",
            requested_qty=Decimal("1.0"),
            requested_unit=unit,
        )
        self._run_backfill()
        item_a.refresh_from_db()
        item_b.refresh_from_db()
        self.assertNotEqual(item_a.supplier_request_id, item_b.supplier_request_id)
        self.assertEqual(item_a.supplier_request.supplier_id, self.supplier.id)
        self.assertEqual(item_b.supplier_request.supplier_id, other_supplier.id)

    def test_leaves_off_system_items_untouched(self):
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")
        order = Order.objects.create(
            venue=self.venue, order_number=f"ORD-TEST-{uuid.uuid4().hex[:8].upper()}", status="submitted"
        )
        item = OrderItem.objects.create(
            order=order,
            off_system_supplier=off_system,
            requested_name="Микрозелень",
            requested_qty=Decimal("1.0"),
        )
        self._run_backfill()
        item.refresh_from_db()
        self.assertIsNone(item.supplier_request_id)


class VenueCartBackfillMigrationTests(TestCase):
    """2026-09-18: core.0033 backfills VenueCart.venue — always NULL before
    this migration (0024_venuecart_user made it optional/unused, nothing
    since has set it). core.0034 (run right after, in the same deploy)
    tightens `venue`/`user` to NOT NULL, so by the time normal tests run a
    null-venue row can no longer be built through the current ORM/schema —
    unlike SupplierOrderRequestBackfillMigrationTests above
    (supplier_request stays nullable forever). setUp relaxes just that one
    constraint via raw DDL for the duration of this test's transaction;
    Postgres DDL is transactional, so Django's per-test rollback (TestCase,
    not TransactionTestCase) undoes it automatically — no manual teardown,
    and no risk of leaking a relaxed schema into other test classes."""

    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE venue_carts ALTER COLUMN venue_id DROP NOT NULL")
            cursor.execute("ALTER TABLE venue_carts DROP CONSTRAINT venue_carts_venue_user_uniq")

    def _run_backfill(self):
        import importlib

        from django.apps import apps as django_apps

        module = importlib.import_module("core.migrations.0033_backfill_venuecart_venue")
        module.backfill_venuecart_venue(django_apps, None)

    def test_unambiguous_item_signal_resolves_venue(self):
        venue = Venue.objects.create(display_name="Cafe")
        user = User.objects.create(email="cart-backfill-1@example.com", full_name="U", role=UserRole.VENUE_USER)
        subdivision = VenueSubdivision.objects.create(venue=venue, name="Кухня")
        cart = VenueCart.objects.create(user=user, venue=None)
        VenueCartItem.objects.create(
            cart=cart, subdivision=subdivision, requested_name="Тест", requested_qty=Decimal("1.0")
        )

        self._run_backfill()

        cart.refresh_from_db()
        self.assertEqual(cart.venue_id, venue.id)

    def test_conflicting_item_signal_deletes_cart(self):
        venue_a = Venue.objects.create(display_name="Cafe A")
        venue_b = Venue.objects.create(display_name="Cafe B")
        user = User.objects.create(email="cart-backfill-2@example.com", full_name="U", role=UserRole.VENUE_USER)
        sub_a = VenueSubdivision.objects.create(venue=venue_a, name="Кухня")
        sub_b = VenueSubdivision.objects.create(venue=venue_b, name="Бар")
        cart = VenueCart.objects.create(user=user, venue=None)
        VenueCartItem.objects.create(cart=cart, subdivision=sub_a, requested_name="A", requested_qty=Decimal("1.0"))
        VenueCartItem.objects.create(cart=cart, subdivision=sub_b, requested_name="B", requested_qty=Decimal("1.0"))
        cart_id = cart.pk

        self._run_backfill()

        self.assertFalse(VenueCart.objects.filter(pk=cart_id).exists())

    def test_no_signal_with_exactly_one_managed_venue_resolves_it(self):
        venue = Venue.objects.create(display_name="Cafe")
        user = User.objects.create(email="cart-backfill-3@example.com", full_name="U", role=UserRole.VENUE_USER)
        VenueUser.objects.create(venue=venue, user=user, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        cart = VenueCart.objects.create(user=user, venue=None)
        VenueCartItem.objects.create(cart=cart, requested_name="Товар", requested_qty=Decimal("1.0"))

        self._run_backfill()

        cart.refresh_from_db()
        self.assertEqual(cart.venue_id, venue.id)

    def test_no_signal_with_zero_managed_venues_deletes_cart(self):
        user = User.objects.create(email="cart-backfill-4@example.com", full_name="U", role=UserRole.VENUE_USER)
        cart = VenueCart.objects.create(user=user, venue=None)
        VenueCartItem.objects.create(cart=cart, requested_name="Товар", requested_qty=Decimal("1.0"))
        cart_id = cart.pk

        self._run_backfill()

        self.assertFalse(VenueCart.objects.filter(pk=cart_id).exists())

    def test_no_signal_with_multiple_managed_venues_deletes_cart(self):
        venue_a = Venue.objects.create(display_name="Cafe A")
        venue_b = Venue.objects.create(display_name="Cafe B")
        user = User.objects.create(email="cart-backfill-5@example.com", full_name="U", role=UserRole.VENUE_USER)
        VenueUser.objects.create(venue=venue_a, user=user, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        VenueUser.objects.create(venue=venue_b, user=user, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        cart = VenueCart.objects.create(user=user, venue=None)
        VenueCartItem.objects.create(cart=cart, requested_name="Товар", requested_qty=Decimal("1.0"))
        cart_id = cart.pk

        self._run_backfill()

        self.assertFalse(VenueCart.objects.filter(pk=cart_id).exists())


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueSubdivisionAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/subdivisions"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_create_and_list(self):
        self.authenticate()
        response = self.client.post(self.url, {"name": "Кухня"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["name"], "Кухня")

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["name"] for row in response.json()], ["Кухня"])

    def test_duplicate_name_rejected(self):
        self.authenticate()
        self.client.post(self.url, {"name": "Бар"}, format="json")
        response = self.client.post(self.url, {"name": "Бар"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_archive_removes_from_list(self):
        self.authenticate()
        subdivision_id = self.client.post(self.url, {"name": "Зал"}, format="json").json()["id"]
        response = self.client.delete(f"{self.url}/{subdivision_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get(self.url).json(), [])


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueAddressAPITests(APITestCase):
    """2026-09-03 (Витя, фронт): та же VenueAddress, что и в кабинете
    (venues/cabinet/addresses), но через venue_id+JWT — доступна из
    контекста корзины/заявки, где cabinet-JWT нет."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/addresses"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_create_and_list(self):
        self.authenticate()
        response = self.client.post(
            self.url, {"address_type": "actual", "address": "г. Москва, ул. Ленина, 1"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["address"], "г. Москва, ул. Ленина, 1")

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["address_type"], "actual")

    def test_delete_removes_from_list(self):
        self.authenticate()
        address_id = self.client.post(self.url, {"address_type": "legal"}, format="json").json()["id"]
        response = self.client.delete(f"{self.url}/{address_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get(self.url).json(), [])

    def test_delete_scoped_to_venue(self):
        other_venue = Venue.objects.create(display_name="Other Cafe")
        self.authenticate()
        address_id = self.client.post(self.url, {"address_type": "legal"}, format="json").json()["id"]
        response = self.client.delete(f"/api/v1/catalog/venues/{other_venue.id}/addresses/{address_id}")
        self.assertEqual(response.status_code, 404)

    def test_rejects_without_auth(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)


@override_settings(SERVICE_API_TOKEN="test-token")
class OffSystemSupplierAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/off-system-suppliers"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_create_list_update_archive(self):
        self.authenticate()
        response = self.client.post(self.url, {"display_name": "Рынок у Петровича", "phone": "+7900"}, format="json")
        self.assertEqual(response.status_code, 201)
        supplier_id = response.json()["id"]

        response = self.client.get(self.url)
        self.assertEqual(response.json()[0]["display_name"], "Рынок у Петровича")

        response = self.client.patch(f"{self.url}/{supplier_id}", {"comment": "звонить утром"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["comment"], "звонить утром")

        response = self.client.delete(f"{self.url}/{supplier_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get(self.url).json(), [])

    def test_duplicate_display_name_rejected(self):
        self.authenticate()
        self.client.post(self.url, {"display_name": "Молочник"}, format="json")
        response = self.client.post(self.url, {"display_name": "Молочник"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_create_defaults_delivery_min_order_amount_to_null(self):
        """2026-09-25, задача #Лк1: минимум не обязателен — по умолчанию не задан."""
        self.authenticate()
        response = self.client.post(self.url, {"display_name": "Рынок"}, format="json")
        data = response.json()
        self.assertIsNone(data["delivery_min_order_amount"])
        self.assertEqual(data["delivery_currency"], "RUB")

    def test_create_with_delivery_min_order_amount(self):
        self.authenticate()
        response = self.client.post(
            self.url,
            {"display_name": "Айсберг", "delivery_min_order_amount": "10000.00", "delivery_currency": "RUB"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(float(data["delivery_min_order_amount"]), 10000.0)
        self.assertEqual(data["delivery_currency"], "RUB")

    def test_update_delivery_min_order_amount(self):
        self.authenticate()
        supplier_id = self.client.post(self.url, {"display_name": "Айсберг"}, format="json").json()["id"]
        response = self.client.patch(
            f"{self.url}/{supplier_id}", {"delivery_min_order_amount": "5000.00"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(float(response.json()["delivery_min_order_amount"]), 5000.0)

    def test_update_clears_delivery_min_order_amount_with_null(self):
        self.authenticate()
        supplier_id = self.client.post(
            self.url, {"display_name": "Айсберг", "delivery_min_order_amount": "5000.00"}, format="json"
        ).json()["id"]
        response = self.client.patch(f"{self.url}/{supplier_id}", {"delivery_min_order_amount": None}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["delivery_min_order_amount"])


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueProductMatrixAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.sp = _make_supplier_product(price="180.00")
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_create_unmatched_row(self):
        self.authenticate()
        response = self.client.post(
            self.url,
            {"requested_name": "Баклажаны свежие грунт.", "requested_unit_label": "кг", "note": "плотные"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIsNone(data["supplier_product_id"])
        self.assertIsNone(data["price"])

    def test_match_supplier_product_via_patch(self):
        self.authenticate()
        item_id = self.client.post(self.url, {"requested_name": "Трюфель черный"}, format="json").json()["id"]
        response = self.client.patch(f"{self.url}/{item_id}", {"supplier_product_id": str(self.sp.id)}, format="json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["supplier_product_id"], str(self.sp.id))
        self.assertEqual(float(data["price"]), 180.0)
        self.assertEqual(data["supplier_product_name"], "test-product")

    def test_list_includes_delivery_multiple(self):
        """2026-09-15, по замечанию пользователя на venue_company_products_list
        (Swagger): "не хватает кратности товара"."""
        SupplierProduct.objects.filter(pk=self.sp.id).update(delivery_multiple=Decimal("5.0"))
        self.authenticate()
        item_id = self.client.post(self.url, {"requested_name": "Мука"}, format="json").json()["id"]
        self.client.patch(f"{self.url}/{item_id}", {"supplier_product_id": str(self.sp.id)}, format="json")
        row = self.client.get(self.url).json()["results"][0]
        self.assertEqual(float(row["delivery_multiple"]), 5.0)

    def test_list_delivery_multiple_null_when_not_linked(self):
        self.authenticate()
        self.client.post(self.url, {"requested_name": "Соль"}, format="json")
        row = self.client.get(self.url).json()["results"][0]
        self.assertIsNone(row["delivery_multiple"])

    def test_list_includes_minimum_order_amount(self):
        self.sp.supplier.delivery_min_order_amount = Decimal("5000.00")
        self.sp.supplier.save(update_fields=["delivery_min_order_amount"])
        self.authenticate()
        item_id = self.client.post(self.url, {"requested_name": "Мука"}, format="json").json()["id"]
        self.client.patch(f"{self.url}/{item_id}", {"supplier_product_id": str(self.sp.id)}, format="json")
        row = self.client.get(self.url).json()["results"][0]
        self.assertEqual(row["minimum_order_amount"], "5000.00")

    def test_list_minimum_order_amount_null_when_not_linked(self):
        self.authenticate()
        self.client.post(self.url, {"requested_name": "Соль"}, format="json")
        row = self.client.get(self.url).json()["results"][0]
        self.assertIsNone(row["minimum_order_amount"])

    def test_list_includes_master_product_name(self):
        """2026-09-15, по замечанию пользователя на venue_company_products_list
        (Swagger): "нужно добавить название мастер товара" — master_product_id
        уже был, названия не было."""
        owner = User.objects.create(email="matrix-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        mp = MasterProduct.objects.create(owner=owner, canonical_name="Сахар песок")
        self.authenticate()
        self.client.post(self.url, {"requested_name": "Сахар"}, format="json")
        item_id = VenueProductMatrixItem.objects.get().id
        VenueProductMatrixItem.objects.filter(pk=item_id).update(master_product=mp)
        response = self.client.get(self.url)
        row = response.json()["results"][0]
        self.assertEqual(row["master_product_id"], str(mp.id))
        self.assertEqual(row["master_product_name"], "Сахар песок")

    def test_list_master_product_name_null_when_not_linked(self):
        self.authenticate()
        self.client.post(self.url, {"requested_name": "Соль"}, format="json")
        row = self.client.get(self.url).json()["results"][0]
        self.assertIsNone(row["master_product_id"])
        self.assertIsNone(row["master_product_name"])

    def test_create_with_subdivision(self):
        """2026-09-17, по прямой просьбе пользователя: subdivision_id на
        справочнике товаров компании."""
        subdivision = VenueSubdivision.objects.create(venue=self.venue, name="Кухня")
        self.authenticate()
        response = self.client.post(
            self.url, {"requested_name": "Мука", "subdivision_id": str(subdivision.id)}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["subdivision_id"], str(subdivision.id))
        self.assertEqual(data["subdivision_name"], "Кухня")

    def test_create_rejects_subdivision_from_another_venue(self):
        other_venue = Venue.objects.create(display_name="Other Cafe")
        foreign_subdivision = VenueSubdivision.objects.create(venue=other_venue, name="Кухня")
        self.authenticate()
        response = self.client.post(
            self.url, {"requested_name": "Мука", "subdivision_id": str(foreign_subdivision.id)}, format="json"
        )
        self.assertEqual(response.status_code, 404)

    def test_patch_sets_and_clears_subdivision(self):
        subdivision = VenueSubdivision.objects.create(venue=self.venue, name="Бар")
        self.authenticate()
        item_id = self.client.post(self.url, {"requested_name": "Лимон"}, format="json").json()["id"]

        response = self.client.patch(f"{self.url}/{item_id}", {"subdivision_id": str(subdivision.id)}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["subdivision_id"], str(subdivision.id))

        response = self.client.patch(f"{self.url}/{item_id}", {"subdivision_id": None}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["subdivision_id"])
        self.assertIsNone(response.json()["subdivision_name"])

    def test_subdivision_null_when_not_set(self):
        self.authenticate()
        self.client.post(self.url, {"requested_name": "Перец"}, format="json")
        row = self.client.get(self.url).json()["results"][0]
        self.assertIsNone(row["subdivision_id"])
        self.assertIsNone(row["subdivision_name"])

    def test_list_filtered_by_subdivision_id(self):
        kitchen = VenueSubdivision.objects.create(venue=self.venue, name="Кухня")
        bar = VenueSubdivision.objects.create(venue=self.venue, name="Бар")
        self.authenticate()
        self.client.post(self.url, {"requested_name": "Мука", "subdivision_id": str(kitchen.id)}, format="json")
        self.client.post(self.url, {"requested_name": "Лимон", "subdivision_id": str(bar.id)}, format="json")
        self.client.post(self.url, {"requested_name": "Без подразделения"}, format="json")

        response = self.client.get(self.url, {"subdivision_id": str(kitchen.id)})
        rows = response.json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requested_name"], "Мука")

        self.assertEqual(self.client.get(self.url).json()["count"], 3)

    def test_list_query_count_does_not_grow_with_master_product_count(self):
        owner = User.objects.create(email="matrix-owner-qc@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.authenticate()
        for i in range(3):
            mp = MasterProduct.objects.create(owner=owner, canonical_name=f"Товар {i}")
            item_id = self.client.post(self.url, {"requested_name": f"Товар {i}"}, format="json").json()["id"]
            VenueProductMatrixItem.objects.filter(pk=item_id).update(master_product=mp)
        with self.assertNumQueries(4):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 3)

    def test_archive_removes_from_list(self):
        self.authenticate()
        item_id = self.client.post(self.url, {"requested_name": "Горчица"}, format="json").json()["id"]
        response = self.client.delete(f"{self.url}/{item_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get(self.url).json()["results"], [])
        item = VenueProductMatrixItem.objects.get(pk=item_id)
        self.assertEqual(item.status, EntityStatus.ARCHIVED)

    def test_archive_keeps_import_alias(self):
        """2026-09-03, дополнение к ТЗ — DELETE не должен физически удалять
        строку и молча каскадом стирать VenueProductImportAlias (память
        подтверждённых решений импорта, Plan 5)."""
        self.authenticate()
        owner = User.objects.create(
            email="owner-archive-alias@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        item_id = self.client.post(self.url, {"requested_name": "Соль"}, format="json").json()["id"]
        alias = VenueProductImportAlias.objects.create(
            venue=self.venue, raw_name="соль экстра", matrix_item_id=item_id, confirmed_by=owner
        )
        response = self.client.delete(f"{self.url}/{item_id}")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(VenueProductImportAlias.objects.filter(pk=alias.pk).exists())


@override_settings(SERVICE_API_TOKEN="test-token")
class OrderTemplateAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = _make_venue_owner(self.venue)
        self.sp = _make_supplier_product(price="100.00")
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"
        self.templates_url = f"/api/v1/catalog/venues/{self.venue.id}/order-templates"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _add_cart_item(self, qty="5.0"):
        return self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": qty},
            format="json",
        ).json()

    def test_save_as_template_and_apply(self):
        self.authenticate()
        self._add_cart_item()
        response = self.client.post(
            f"{self.cart_url}/save-as-template",
            {"name": "Стандартная неделя"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        template = response.json()
        self.assertEqual(template["name"], "Стандартная неделя")
        self.assertEqual(template["items_count"], 1)

        response = self.client.get(self.templates_url)
        self.assertEqual(response.json()[0]["name"], "Стандартная неделя")

        # submit clears the cart's items — the template is a snapshot, independent of it.
        self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )

        response = self.client.post(f"{self.templates_url}/{template['id']}/apply")
        self.assertEqual(response.status_code, 200)
        cart = response.json()
        self.assertNotIn("status", cart)
        self.assertNotIn("order_number", cart)
        self.assertEqual(len(cart["items"]), 1)
        self.assertEqual(cart["items"][0]["requested_qty"], "5.0000")

    def test_apply_adds_to_existing_cart_items(self):
        self.authenticate()
        self._add_cart_item()
        template_id = self.client.post(f"{self.cart_url}/save-as-template", {"name": "Довоз"}, format="json").json()[
            "id"
        ]
        self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )

        # Fresh round — cart already has 1 item before the template is applied.
        self._add_cart_item()

        response = self.client.post(f"{self.templates_url}/{template_id}/apply")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["items"]), 2)

    def test_duplicate_template_name_rejected(self):
        self.authenticate()
        self._add_cart_item()
        self.client.post(f"{self.cart_url}/save-as-template", {"name": "Шаблон"}, format="json")
        response = self.client.post(f"{self.cart_url}/save-as-template", {"name": "Шаблон"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_archive_template(self):
        self.authenticate()
        self._add_cart_item()
        template_id = self.client.post(f"{self.cart_url}/save-as-template", {"name": "Разовый"}, format="json").json()[
            "id"
        ]
        response = self.client.delete(f"{self.templates_url}/{template_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get(self.templates_url).json(), [])

    def test_apply_lands_in_the_templates_own_venue_not_another_managed_one(self):
        """2026-09-18: apply_order_template derives venue from
        template.venue (the template is, and always was, venue-scoped) —
        items must land in that SAME venue's cart even when the caller
        also manages another venue, never in whichever cart happens to be
        "current"."""
        other_venue = Venue.objects.create(display_name="Other Cafe")
        VenueUser.objects.create(
            venue=other_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        other_cart_url = f"/api/v1/catalog/venues/{other_venue.id}/cart"

        self.authenticate()
        self._add_cart_item()
        template_id = self.client.post(
            f"{self.cart_url}/save-as-template", {"name": "Недельный"}, format="json"
        ).json()["id"]
        self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )

        response = self.client.post(f"{self.templates_url}/{template_id}/apply")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 1)

        self.assertEqual(self.client.get(other_cart_url).json()["items"], [])


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueCartAPITests(APITestCase):
    """Сборка/редактирование корзины до отправки — отдельная от Order сущность
    (VenueCart), 2026-08-28, Cart/Order split по требованию Вити: "cart и
    order это разные бизнес сущности". Одноместные (single-venue) сценарии
    здесь — multi-venue-специфика (разные корзины на разные заведения,
    изоляция между ними) в VenueCartVenueScopedAPITests ниже, привязка по
    (user, venue) с 2026-09-18."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = _make_venue_owner(self.venue)
        self.sp = _make_supplier_product(price="100.00")
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def test_creates_cart_when_none_exists(self):
        self.authenticate()
        self.assertEqual(VenueCart.objects.filter(user=self.owner).count(), 0)
        response = self.client.get(self.cart_url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn("order_number", data)
        self.assertNotIn("status", data)
        self.assertEqual(data["items"], [])
        self.assertEqual(VenueCart.objects.filter(user=self.owner).count(), 1)

    def test_returns_same_cart_on_repeat_calls(self):
        self.authenticate()
        first = self.client.get(self.cart_url).json()["id"]
        second = self.client.get(self.cart_url).json()["id"]
        self.assertEqual(first, second)
        self.assertEqual(VenueCart.objects.filter(user=self.owner).count(), 1)

    def test_patch_updates_comment_and_delivery_date(self):
        self.authenticate()
        response = self.client.patch(
            self.cart_url, {"comment": "К пятнице", "requested_delivery_date": "2026-09-05"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["comment"], "К пятнице")
        self.assertEqual(data["requested_delivery_date"], "2026-09-05")

    def test_patch_requires_at_least_one_field(self):
        self.authenticate()
        response = self.client.patch(self.cart_url, {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_add_item_with_supplier_product(self):
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "2.0000"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["requested_name"], "test-product")
        self.assertNotIn("status", data)
        self.assertNotIn("confirmed_qty", data)
        self.assertNotIn("confirmed_price", data)
        self.assertNotIn("confirmed_delivery_date", data)

    def test_add_item_without_supplier_product(self):
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Неизвестный товар", "requested_qty": "1.0000"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["requested_name"], "Неизвестный товар")

    def test_add_item_requires_product_or_name(self):
        self.authenticate()
        response = self.client.post(f"{self.cart_url}/items", {"requested_qty": "1.0"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_update_item_qty(self):
        self.authenticate()
        item_id = self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        ).json()["id"]
        response = self.client.patch(f"{self.cart_url}/items/{item_id}", {"requested_qty": "7.0"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requested_qty"], "7.0000")

    def test_update_item_requires_at_least_one_field(self):
        self.authenticate()
        item_id = self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        ).json()["id"]
        response = self.client.patch(f"{self.cart_url}/items/{item_id}", {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_delete_item(self):
        self.authenticate()
        item_id = self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        ).json()["id"]
        response = self.client.delete(f"{self.cart_url}/items/{item_id}")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(VenueCartItem.objects.filter(pk=item_id).exists())

    def test_cart_supplier_groups(self):
        """2026-08-27/28, Vitya: та же группировка по поставщику, что и у
        заказа, но без confirmed-полей/status у позиций — их у корзины
        принципиально не может быть."""
        self.authenticate()
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Микрозелень", "requested_qty": "1.0", "off_system_supplier_id": str(off_system.id)},
            format="json",
        )
        response = self.client.get(self.cart_url)
        groups = response.json()["supplier_groups"]
        self.assertEqual(len(groups), 2)
        by_name = {g["supplier_name"]: g for g in groups}
        self.assertNotIn("status", by_name["Рынок"]["items"][0])

    def test_item_has_price_and_step_fields(self):
        """2026-08-30, Vitya: "этих полей нет в апи (корзина)" — цена и итог
        по позиции, кратность/минимальное количество заказа. price/item_total
        резолвятся через resolve_supplier_product_price (базовая цена
        SupplierProduct, если нет активной индивидуальной — см.
        VenueCartIndividualPriceAPITests), не из confirmed_* (тех у корзины
        нет и не будет)."""
        self.sp.min_order_qty = "2.0000"
        self.sp.delivery_multiple = "3.0000"
        self.sp.image_url = "https://example.com/product.jpg"
        self.sp.save(update_fields=["min_order_qty", "delivery_multiple", "image_url"])
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "6.0"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(float(data["price"]), 100.0)
        self.assertEqual(float(data["item_total"]), 600.0)
        self.assertEqual(float(data["min_order_qty"]), 2.0)
        self.assertEqual(float(data["delivery_multiple"]), 3.0)
        self.assertEqual(data["supplier_product_image_url"], "https://example.com/product.jpg")

    def test_item_without_photo_gets_catalog_placeholder_image(self):
        """2026-10-01: в каталоге у товара без фото есть картинка-заглушка (TASK-125), а в корзине
        supplier_product_image_url был null — фронт показывал товар без картинки."""
        self.authenticate()
        created = self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "1"}, format="json"
        ).json()
        expected = "//testserver/static/catalog/category-placeholders/generic-grocery.svg"
        self.assertEqual(created["supplier_product_image_url"], expected)
        cart = self.client.get(self.cart_url).json()
        self.assertEqual(cart["items"][0]["supplier_product_image_url"], expected)
        self.assertEqual(cart["supplier_groups"][0]["items"][0]["supplier_product_image_url"], expected)

    def test_item_without_supplier_product_has_null_price(self):
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Неизвестный товар", "requested_qty": "1.0"},
            format="json",
        )
        data = response.json()
        self.assertIsNone(data["price"])
        self.assertIsNone(data["item_total"])
        self.assertIsNone(data["min_order_qty"])
        self.assertIsNone(data["delivery_multiple"])
        self.assertIsNone(data["supplier_product_image_url"])

    def test_supplier_group_subtotal_and_minimum_order(self):
        """2026-08-30, Vitya: не увидел в корзине сумму по поставщику и
        минимальную сумму заказа — оба поля показывались во фронтовом макете
        ("Осталось: 200 ₽" / "Мин. заказ: 10 000 ₽")."""
        self.sp.supplier.delivery_min_order_amount = "1000.00"
        self.sp.supplier.delivery_currency = "RUB"
        self.sp.supplier.save(update_fields=["delivery_min_order_amount", "delivery_currency"])
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "6.0"},
            format="json",
        )
        response = self.client.get(self.cart_url)
        group = response.json()["supplier_groups"][0]
        self.assertEqual(float(group["subtotal"]), 600.0)
        self.assertEqual(float(group["minimum_order_amount"]), 1000.0)
        self.assertEqual(group["currency"], "RUB")
        self.assertEqual(float(group["remaining_to_minimum"]), 400.0)

    def test_supplier_group_remaining_to_minimum_floors_at_zero(self):
        self.sp.supplier.delivery_min_order_amount = "50.00"
        self.sp.supplier.save(update_fields=["delivery_min_order_amount"])
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "6.0"},
            format="json",
        )
        response = self.client.get(self.cart_url)
        group = response.json()["supplier_groups"][0]
        self.assertEqual(float(group["remaining_to_minimum"]), 0.0)

    def test_supplier_group_includes_delivery_cost_and_total(self):
        """2026-09-15 (raw ТЗ §8-9): доставка показывается отдельно от
        subtotal (товары), не входит в minimum_order_amount/
        remaining_to_minimum — только в total_with_delivery."""
        self.sp.supplier.delivery_min_order_amount = "1000.00"
        self.sp.supplier.delivery_cost = "300.00"
        self.sp.supplier.save(update_fields=["delivery_min_order_amount", "delivery_cost"])
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "6.0"},
            format="json",
        )
        response = self.client.get(self.cart_url)
        group = response.json()["supplier_groups"][0]
        self.assertEqual(float(group["subtotal"]), 600.0)
        self.assertEqual(float(group["delivery_cost"]), 300.0)
        self.assertEqual(float(group["total_with_delivery"]), 900.0)
        # доставка не входит в расчёт минимальной суммы товаров
        self.assertEqual(float(group["remaining_to_minimum"]), 400.0)

    def test_supplier_group_delivery_cost_null_when_not_set(self):
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "6.0"},
            format="json",
        )
        response = self.client.get(self.cart_url)
        group = response.json()["supplier_groups"][0]
        self.assertIsNone(group["delivery_cost"])
        self.assertEqual(float(group["total_with_delivery"]), float(group["subtotal"]))

    def test_off_system_group_has_null_subtotal_and_minimum(self):
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Микрозелень", "requested_qty": "1.0", "off_system_supplier_id": str(off_system.id)},
            format="json",
        )
        response = self.client.get(self.cart_url)
        group = response.json()["supplier_groups"][0]
        self.assertIsNone(group["subtotal"])
        self.assertIsNone(group["minimum_order_amount"])
        self.assertIsNone(group["remaining_to_minimum"])

    def test_off_system_group_shows_minimum_order_amount_when_set(self):
        """2026-09-25, задача #Лк1: если у поставщика вне системы задан
        delivery_min_order_amount, группа корзины должна его показывать —
        тот же смысл, что у обычного Supplier.delivery_min_order_amount."""
        off_system = OffSystemSupplier.objects.create(
            venue=self.venue, display_name="Айсберг", delivery_min_order_amount=Decimal("10000.00")
        )
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Баклажан Африка", "requested_qty": "1.0", "off_system_supplier_id": str(off_system.id)},
            format="json",
        )
        response = self.client.get(self.cart_url)
        group = response.json()["supplier_groups"][0]
        self.assertEqual(float(group["minimum_order_amount"]), 10000.0)
        self.assertEqual(group["currency"], "RUB")
        # позиция без supplier_product — subtotal остаётся null (нет цены), но
        # минимум/валюта не зависят от subtotal
        self.assertIsNone(group["subtotal"])

    def test_submit_creates_order_and_clears_cart(self):
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "3.0"},
            format="json",
        )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "submitted")
        self.assertTrue(data["order_number"].startswith("ORD-"))
        order = Order.objects.get(pk=data["id"])
        self.assertEqual(order.status, OrderStatus.SUBMITTED)
        self.assertIsNotNone(order.submitted_at)
        self.assertEqual(order.items.count(), 1)

        cart_after = self.client.get(self.cart_url).json()
        self.assertEqual(cart_after["items"], [])

    def test_submit_empty_cart_fails(self):
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 409)

    def test_cart_is_reused_after_submit(self):
        self.authenticate()
        cart_id = self.client.get(self.cart_url).json()["id"]
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        )
        self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )

        same_cart_id = self.client.get(self.cart_url).json()["id"]
        self.assertEqual(same_cart_id, cart_id)


class VenueCartIndividualPriceAPITests(APITestCase):
    """2026-09-23 (Дима: "цена у заведения как есть отображается а нужно
    индивидуальную показывать") — CartItemSerializer.price/item_total и
    CartSupplierGroupSerializer.subtotal читали SupplierProduct.current_price
    напрямую, игнорируя активную SupplierProductVenuePrice — тот же класс
    бага, что TASK-44 закрыл для Справочника компании, но обошедший корзину
    (submit уже резолвил цену правильно, см. SubmitVenueCartAppliedPriceTests
    — applied_price снимок; сама корзина до отправки показывала не то)."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = _make_venue_owner(self.venue)
        self.sp = _make_supplier_product(price="100.00")
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def test_item_price_uses_active_individual_price(self):
        set_supplier_product_venue_price(
            supplier_product=self.sp, venues=[self.venue], price=Decimal("85.00"), created_by=self.owner
        )
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "3.0"},
            format="json",
        )
        data = response.json()
        self.assertEqual(float(data["price"]), 85.0)
        self.assertEqual(float(data["item_total"]), 255.0)

    def test_item_price_falls_back_to_base_price_without_individual_price(self):
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "3.0"},
            format="json",
        )
        data = response.json()
        self.assertEqual(float(data["price"]), 100.0)
        self.assertEqual(float(data["item_total"]), 300.0)

    def test_get_cart_reflects_individual_price(self):
        self.authenticate()
        item_id = self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "3.0"},
            format="json",
        ).json()["id"]
        set_supplier_product_venue_price(
            supplier_product=self.sp, venues=[self.venue], price=Decimal("85.00"), created_by=self.owner
        )
        data = self.client.get(self.cart_url).json()
        item = data["items"][0]
        self.assertEqual(item["id"], item_id)
        self.assertEqual(float(item["price"]), 85.0)
        self.assertEqual(float(item["item_total"]), 255.0)

    def test_supplier_group_subtotal_uses_individual_price(self):
        set_supplier_product_venue_price(
            supplier_product=self.sp, venues=[self.venue], price=Decimal("85.00"), created_by=self.owner
        )
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "3.0"},
            format="json",
        )
        group = self.client.get(self.cart_url).json()["supplier_groups"][0]
        self.assertEqual(float(group["subtotal"]), 255.0)

    def test_individual_price_of_other_venue_does_not_affect_this_cart(self):
        other_venue = Venue.objects.create(display_name="Other Cafe")
        set_supplier_product_venue_price(
            supplier_product=self.sp, venues=[other_venue], price=Decimal("55.00"), created_by=self.owner
        )
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "3.0"},
            format="json",
        )
        self.assertEqual(float(response.json()["price"]), 100.0)


class VenueCartVenueScopedAPITests(APITestCase):
    """2026-09-18, пользователь: "нужно получать корзину по ид заведения" →
    "Привязать по-настоящему (отдельная корзина на каждое заведение)" —
    разворот решения 2026-09-08 (было: singleton на пользователя, один
    аккаунт с несколькими заведениями вёл одну общую корзину, venue_id
    требовался только в POST /cart/submit). Теперь снова одна корзина на
    пару (пользователь, заведение), venue_id — часть URL везде.

    `test_submit_rejects_item_tied_to_a_different_venue_than_submitted`
    (старый защитный тест на смешение заведений в одной корзине) убран —
    сценарий физически недостижим через публичный API теперь: позицию
    нельзя добавить в корзину с subdivision/off_system_supplier чужого
    заведения (см. test_item_subdivision_must_belong_to_the_url_venue
    ниже), значит и противоречивой при submit её взять неоткуда."""

    def setUp(self):
        self.venue_a = Venue.objects.create(display_name="Cafe A")
        self.venue_b = Venue.objects.create(display_name="Cafe B")
        self.owner = User.objects.create(
            email="multi-venue-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        VenueUser.objects.create(
            venue=self.venue_a, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.venue_b, user=self.owner, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.other_owner = User.objects.create(
            email="not-my-venue@example.com", full_name="Other", role=UserRole.VENUE_USER
        )
        self.sp = _make_supplier_product(price="100.00")
        self.cart_url_a = f"/api/v1/catalog/venues/{self.venue_a.id}/cart"
        self.cart_url_b = f"/api/v1/catalog/venues/{self.venue_b.id}/cart"

    def authenticate(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.owner))

    def test_service_token_no_longer_grants_cart_access(self):
        """A service token has no core.User account to own a cart under —
        IsVenueMember's HasServiceToken branch would otherwise pass since
        the service token isn't scoped to a venue."""
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.get(self.cart_url_a)
        self.assertEqual(response.status_code, 401)

    def test_different_cart_per_venue_i_manage(self):
        """The whole point of the change — each managed venue gets its own
        cart, not one shared across all of them."""
        self.authenticate()
        cart_id_a = self.client.get(self.cart_url_a).json()["id"]
        cart_id_b = self.client.get(self.cart_url_b).json()["id"]
        self.assertNotEqual(cart_id_a, cart_id_b)
        self.assertEqual(VenueCart.objects.filter(user=self.owner).count(), 2)

    def test_get_or_create_cart_idempotent_per_venue(self):
        """get_or_create_cart(user, venue) must not create a second row for
        the same (user, venue) pair on repeat calls — the UniqueConstraint
        this relies on."""
        self.authenticate()
        first = self.client.get(self.cart_url_a).json()["id"]
        second = self.client.get(self.cart_url_a).json()["id"]
        self.assertEqual(first, second)
        self.assertEqual(VenueCart.objects.filter(user=self.owner, venue=self.venue_a).count(), 1)

    def test_unique_constraint_rejects_duplicate_venue_user_pair(self):
        VenueCart.objects.create(user=self.owner, venue=self.venue_a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VenueCart.objects.create(user=self.owner, venue=self.venue_a)

    def test_submit_rejects_venue_i_do_not_manage(self):
        self.authenticate(self.other_owner)
        response = self.client.post(
            f"{self.cart_url_a}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_items_added_to_venue_a_cart_never_appear_in_venue_b_cart(self):
        self.authenticate()
        self.client.post(
            f"{self.cart_url_a}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"}, format="json"
        )
        self.assertEqual(len(self.client.get(self.cart_url_a).json()["items"]), 1)
        self.assertEqual(len(self.client.get(self.cart_url_b).json()["items"]), 0)

        response = self.client.post(
            f"{self.cart_url_a}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["venue_id"], str(self.venue_a.id))

    def test_item_subdivision_must_belong_to_the_url_venue(self):
        """2026-09-18: tightened from "any venue I manage" — subdivision_b
        belongs to a venue I DO manage, just not the one in the URL, so
        adding it to venue_a's cart is now rejected (was allowed before)."""
        subdivision_b = VenueSubdivision.objects.create(venue=self.venue_b, name="Бар")
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url_a}/items",
            {
                "supplier_product_id": str(self.sp.id),
                "requested_qty": "1.0",
                "subdivision_id": str(subdivision_b.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_item_subdivision_from_venue_i_do_not_manage_rejected(self):
        foreign_venue = Venue.objects.create(display_name="Not mine")
        foreign_subdivision = VenueSubdivision.objects.create(venue=foreign_venue, name="Кухня")
        self.authenticate()
        response = self.client.post(
            f"{self.cart_url_a}/items",
            {
                "supplier_product_id": str(self.sp.id),
                "requested_qty": "1.0",
                "subdivision_id": str(foreign_subdivision.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)


def _make_supplier_product_for(supplier, name, price):
    """Несколько товаров ОДНОГО поставщика — общий _make_supplier_product для
    этого не годится, у него supplier_name захардкожен, а на
    (supplier_id, supplier_name) висит unique-констрейнт."""
    unit = Unit.objects.filter(code="шт").first() or Unit.objects.create(code="шт", name="штука", kind="piece")
    sp = SupplierProduct.objects.create(
        supplier=supplier,
        supplier_name=name,
        normalized_name=name,
        unit=unit,
        status=EntityStatus.ACTIVE,
    )
    SupplierProductPrice.objects.create(supplier_product=sp, price=price, currency="RUB", status=PriceStatus.ACTIVE)
    return sp


class CartItemOrderStabilityAPITests(APITestCase):
    """2026-09-08 (Витя, баг "список прыгает при быстром вводе количества",
    3 независимые записи экрана): порядок позиций в блоке поставщика менялся
    ровно в момент прихода ответа cart API — правишь qty у одной позиции, а
    наверх выпрыгивает совсем другая, нетронутая.

    Причина оказалась не в "бэкенд отдаёт последнюю изменённую первой" (такой
    логики не было вообще), а в ОТСУТСТВИИ какого-либо ordering у
    VenueCartItem/OrderItem: Postgres отдавал строки в heap-порядке, а он
    меняется при каждом UPDATE (MVCC дописывает обновлённую версию строки в
    конец heap). То есть порядок был просто неопределённым."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = _make_venue_owner(self.venue)
        self.supplier = Supplier.objects.create(legal_name="A LLC", display_name="Supplier A")
        self.sp_first = _make_supplier_product_for(self.supplier, "ананасы кусочки", "100.00")
        self.sp_second = _make_supplier_product_for(self.supplier, "абрикос половинки", "200.00")
        self.sp_third = _make_supplier_product_for(self.supplier, "сливки 33%", "300.00")
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _add(self, supplier_product, qty="25.0"):
        return self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(supplier_product.id), "requested_qty": qty},
            format="json",
        ).json()["id"]

    def _item_ids(self):
        return [item["id"] for item in self.client.get(self.cart_url).json()["items"]]

    def test_item_order_is_stable_after_updating_a_qty(self):
        """Ровно сценарий видео 1: правим qty первой позиции — порядок всего
        списка обязан остаться прежним."""
        first = self._add(self.sp_first)
        second = self._add(self.sp_second)
        third = self._add(self.sp_third)
        self.assertEqual(self._item_ids(), [first, second, third])

        response = self.client.patch(f"{self.cart_url}/items/{first}", {"requested_qty": "1.0"}, format="json")
        self.assertEqual(response.status_code, 200)

        self.assertEqual(self._item_ids(), [first, second, third])

    def test_item_order_is_stable_after_many_rapid_updates(self):
        """Сценарий видео 3: зажатая "+" — несколько апдейтов подряд по одной
        и той же позиции, порядок не должен разъезжаться ни на одном шаге.

        Здесь апдейтов намеренно много: пока в heap-странице есть свободное
        место, Postgres делает HOT-update и строка физически остаётся на
        месте — баг не виден. Когда страница заполняется, HOT-цепочка
        рвётся, новая версия строки уезжает в другую страницу, и порядок в
        выдаче без ORDER BY меняется. На пустой таблице это происходит
        примерно на 76-м апдейте, в проде с реальным churn — заметно
        раньше и непредсказуемо. Без Meta.ordering этот тест падает:
        отредактированная позиция уезжает в КОНЕЦ списка (а не в начало —
        гипотеза "последняя изменённая наверх" из отчёта не подтвердилась)."""
        first = self._add(self.sp_first)
        second = self._add(self.sp_second)
        third = self._add(self.sp_third)

        for n in range(150):
            qty = f"{10 + n % 50}.0"
            response = self.client.patch(f"{self.cart_url}/items/{first}", {"requested_qty": qty}, format="json")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self._item_ids(), [first, second, third], f"порядок разъехался на {n}-м апдейте")

    def test_supplier_group_item_order_is_stable_after_update(self):
        """Тот же инвариант, но для supplier_groups — на экране Витя видит
        именно сгруппированный по поставщику список."""
        first = self._add(self.sp_first)
        second = self._add(self.sp_second)
        self.client.patch(f"{self.cart_url}/items/{first}", {"requested_qty": "1.0"}, format="json")

        groups = self.client.get(self.cart_url).json()["supplier_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual([item["id"] for item in groups[0]["items"]], [first, second])

    def test_item_total_and_group_subtotal_are_consistent_within_one_response(self):
        """Пункт 4 из отчёта Вити — проверка, что рассинхрон item_total и
        subtotal возникает только на клиенте (от применения разных ответов),
        а не внутри одного ответа бэкенда."""
        self._add(self.sp_first, qty="2.0")
        self._add(self.sp_second, qty="3.0")

        data = self.client.get(self.cart_url).json()
        group = data["supplier_groups"][0]
        self.assertEqual(
            float(group["subtotal"]),
            sum(float(item["item_total"]) for item in group["items"]),
        )
        self.assertEqual(float(group["subtotal"]), 2 * 100.0 + 3 * 200.0)

    def test_patch_response_item_total_matches_qty_from_the_same_request(self):
        item_id = self._add(self.sp_first, qty="25.0")
        response = self.client.patch(f"{self.cart_url}/items/{item_id}", {"requested_qty": "1.0"}, format="json")
        data = response.json()
        self.assertEqual(float(data["requested_qty"]), 1.0)
        self.assertEqual(float(data["item_total"]), 100.0)

    def test_cart_item_exposes_updated_at_that_advances_on_change(self):
        """Токен свежести для фронта: позволяет отбросить ответ, пришедший
        позже, но относящийся к более раннему запросу."""
        item_id = self._add(self.sp_first, qty="25.0")
        before = self.client.get(self.cart_url).json()["items"][0]["updated_at"]

        after = self.client.patch(f"{self.cart_url}/items/{item_id}", {"requested_qty": "26.0"}, format="json").json()[
            "updated_at"
        ]

        self.assertIsNotNone(before)
        self.assertGreater(after, before)


@override_settings(SERVICE_API_TOKEN="test-token")
class OrderItemOrderStabilityAPITests(APITestCase):
    """Та же проблема на стороне уже отправленного заказа: приём/частичный
    приём делает UPDATE позиций, из-за чего история заказа перетасовывалась
    после каждого действия поставщика (Витя этот экран ещё не смотрел, но
    баг тот же и чинится тем же ordering)."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = _make_venue_owner(self.venue)
        self.supplier = Supplier.objects.create(legal_name="A LLC", display_name="Supplier A")
        self.sp_a = _make_supplier_product_for(self.supplier, "product-a", "100.00")
        self.sp_b = _make_supplier_product_for(self.supplier, "product-b", "200.00")
        self.sp_c = _make_supplier_product_for(self.supplier, "product-c", "300.00")
        self.orders_url = f"/api/v1/catalog/venues/{self.venue.id}/orders"

    def test_order_item_order_is_stable_after_partial_accept(self):
        cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        for sp in (self.sp_a, self.sp_b, self.sp_c):
            self.client.post(
                f"{cart_url}/items", {"supplier_product_id": str(sp.id), "requested_qty": "10.0"}, format="json"
            )
        order_id = self.client.post(
            f"{cart_url}/submit", {"command_id": str(uuid4()), "expected_settings_version": 1}, format="json"
        ).json()["id"]

        before = [item["id"] for item in _grouped_order_items(self.client.get(f"{self.orders_url}/{order_id}").json())]
        self.assertEqual(len(before), 3)

        # Поставщик частично принимает СРЕДНЮЮ позицию — она не должна никуда переехать.
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.post(
            f"/api/v1/catalog/suppliers/{self.supplier.id}/orders/{order_id}/accept",
            {
                "confirmed_delivery_date": _TEST_DELIVERY_DATE,
                "items": [{"item_id": before[1], "confirmed_qty": "4.0"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        after = [item["id"] for item in _grouped_order_items(self.client.get(f"{self.orders_url}/{order_id}").json())]
        self.assertEqual(after, before)


class SubmitVenueCartAppliedPriceTests(APITestCase):
    """2026-09-07 (Павел's "Поставщик Изменения цен - ТЗ", §38-39): price is
    snapshotted onto OrderItem at submit_venue_cart() time — the ядро of the
    "История изменения цены" task (applied_individual_price_id is what
    supplier_product_venue_price_confirmed_qty aggregates by)."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = User.objects.create(
            email="cart-price-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.sp = _make_supplier_product(price="100.00")
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def test_submit_snapshots_base_price_when_no_individual_price(self):
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "3.0"}, format="json"
        )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        item = Order.objects.get(pk=response.json()["id"]).items.get()
        self.assertEqual(item.applied_price, Decimal("100.00"))
        self.assertEqual(item.applied_price_type, "base")
        self.assertIsNone(item.applied_individual_price_id)

    def test_submit_snapshots_individual_price_and_its_id(self):
        (price,) = set_supplier_product_venue_price(
            supplier_product=self.sp, venues=[self.venue], price=Decimal("85.00"), created_by=self.owner
        )
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "3.0"}, format="json"
        )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        item = Order.objects.get(pk=response.json()["id"]).items.get()
        self.assertEqual(item.applied_price, Decimal("85.00"))
        self.assertEqual(item.applied_price_type, "individual")
        self.assertEqual(item.applied_individual_price_id, price.id)

    def test_submit_snapshots_nothing_for_off_system_supplier_item(self):
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Микрозелень", "requested_qty": "1.0", "off_system_supplier_id": str(off_system.id)},
            format="json",
        )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        item = Order.objects.get(pk=response.json()["id"]).items.get()
        self.assertIsNone(item.applied_price)
        self.assertIsNone(item.applied_price_type)
        self.assertIsNone(item.applied_individual_price_id)

    def test_later_price_change_does_not_affect_already_submitted_item(self):
        (price,) = set_supplier_product_venue_price(
            supplier_product=self.sp, venues=[self.venue], price=Decimal("85.00"), created_by=self.owner
        )
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": "3.0"}, format="json"
        )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        item_id = Order.objects.get(pk=response.json()["id"]).items.get().id

        set_supplier_product_venue_price(
            supplier_product=self.sp,
            venues=[self.venue],
            price=Decimal("95.00"),
            created_by=self.owner,
            confirm_replace=True,
        )
        item = OrderItem.objects.get(pk=item_id)
        self.assertEqual(item.applied_price, Decimal("85.00"))
        self.assertEqual(item.applied_individual_price_id, price.id)


@override_settings(SERVICE_API_TOKEN="test-token")
class OrderItemTotalSerializerTests(APITestCase):
    """2026-09-08: Витя прислал макет "История заказов" с колонкой "Сумма
    (Р)" — отсутствовала на OrderItemSerializer (была только на
    CartItemSerializer). item_total = applied_price * qty, где qty —
    подтверждённое количество, если позиция уже обработана, иначе
    запрошенное (до обработки подтверждённого количества ещё нет)."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = User.objects.create(
            email="item-total-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.sp = _make_supplier_product(price="152.00")
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _submit_order(self, qty="200"):
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items", {"supplier_product_id": str(self.sp.id), "requested_qty": qty}, format="json"
        )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        return response.json()["id"]

    def test_item_total_uses_requested_qty_before_acceptance(self):
        order_id = self._submit_order(qty="200")
        response = self.client.get(f"/api/v1/catalog/venues/{self.venue.id}/orders/{order_id}")
        item = _grouped_order_items(response.json())[0]
        self.assertEqual(item["applied_price"], "152.00")
        self.assertEqual(float(item["item_total"]), 30400.0)

    def test_item_total_switches_to_confirmed_qty_after_partial_acceptance(self):
        order_id = self._submit_order(qty="200")
        order = Order.objects.get(pk=order_id)
        item_id = str(order.items.get().id)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.post(
            f"/api/v1/catalog/suppliers/{self.sp.supplier_id}/orders/{order_id}/accept",
            {"confirmed_delivery_date": _TEST_DELIVERY_DATE, "items": [{"item_id": item_id, "confirmed_qty": "150"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.authenticate()
        response = self.client.get(f"/api/v1/catalog/venues/{self.venue.id}/orders/{order_id}")
        item = _grouped_order_items(response.json())[0]
        self.assertEqual(float(item["item_total"]), 22800.0)

    def test_item_total_null_for_off_system_supplier_item(self):
        off_system = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Микрозелень", "requested_qty": "1.0", "off_system_supplier_id": str(off_system.id)},
            format="json",
        )
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        )
        order_id = response.json()["id"]
        response = self.client.get(f"/api/v1/catalog/venues/{self.venue.id}/orders/{order_id}")
        self.assertIsNone(_grouped_order_items(response.json())[0]["item_total"])


class SupplierProductVenuePriceConfirmedQtyTests(TestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = User.objects.create(
            email="confirmed-qty-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.sp = _make_supplier_product(price="100.00")
        (self.price,) = set_supplier_product_venue_price(
            supplier_product=self.sp, venues=[self.venue], price=Decimal("85.00"), created_by=self.owner
        )
        self.order = Order.objects.create(venue=self.venue, order_number="ORD-CQ1", status=OrderStatus.SUBMITTED)

    def _item(self, requested_qty, confirmed_qty, status):
        return OrderItem.objects.create(
            order=self.order,
            supplier_product=self.sp,
            requested_name=self.sp.normalized_name,
            requested_qty=Decimal(requested_qty),
            confirmed_qty=Decimal(confirmed_qty) if confirmed_qty is not None else None,
            status=status,
            applied_price=Decimal("85.00"),
            applied_price_type="individual",
            applied_individual_price=self.price,
        )

    def test_zero_when_no_orders_reference_the_price(self):
        self.assertEqual(supplier_product_venue_price_confirmed_qty(self.price.id), Decimal("0"))

    def test_sums_confirmed_qty_across_matching_items_only(self):
        self._item("100", "100", OrderItemStatus.CONFIRMED)
        self._item("80", "50", OrderItemStatus.CONFIRMED)
        self._item("40", None, OrderItemStatus.CANCELLED)
        self._item("60", None, OrderItemStatus.SUBMITTED)
        other_price_item = OrderItem.objects.create(
            order=self.order,
            supplier_product=self.sp,
            requested_name=self.sp.normalized_name,
            requested_qty=Decimal("999"),
            confirmed_qty=Decimal("999"),
            status=OrderItemStatus.CONFIRMED,
            applied_price=Decimal("100.00"),
            applied_price_type="base",
        )
        self.assertIsNone(other_price_item.applied_individual_price_id)
        self.assertEqual(supplier_product_venue_price_confirmed_qty(self.price.id), Decimal("150"))


class IndividualPriceOrderHistorySelectorTests(TestCase):
    """§26-27 ТЗ «Карточка товара поставщика»: построчная история заявок по
    одной индивидуальной цене — тот же принцип строгой привязки по
    applied_individual_price_id, что уже использует
    supplier_product_venue_price_confirmed_qty (см.
    SupplierProductVenuePriceConfirmedQtyTests выше), плюс фильтры по
    заведению/периоду/номеру заявки (§27)."""

    def setUp(self):
        self.supplier = Supplier.objects.create(display_name="Поставщик А")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сливки 33%", normalized_name="Сливки 33%"
        )
        self.venue = Venue.objects.create(display_name="Качели")
        self.other_venue = Venue.objects.create(display_name="Причал")
        self.owner = User.objects.create(email="hist_owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("305.00"), created_by=self.owner
        )

    def test_filters_by_applied_price_only(self):
        order = Order.objects.create(venue=self.venue, order_number="ORD-HIST-1")
        matching_item = OrderItem.objects.create(
            order=order,
            supplier_product=self.supplier_product,
            requested_qty=Decimal("100"),
            confirmed_qty=Decimal("60"),
            applied_individual_price=self.price,
        )
        other_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.other_venue,
            price=Decimal("310.00"),
            created_by=self.owner,
        )
        OrderItem.objects.create(
            order=order,
            supplier_product=self.supplier_product,
            requested_qty=Decimal("50"),
            confirmed_qty=Decimal("50"),
            applied_individual_price=other_price,
        )

        rows = list(individual_price_order_history_queryset(price_id=self.price.id))
        self.assertEqual([r.id for r in rows], [matching_item.id])

    def test_filters_by_venue_and_order_number(self):
        order = Order.objects.create(venue=self.venue, order_number="1534")
        item = OrderItem.objects.create(
            order=order,
            supplier_product=self.supplier_product,
            requested_qty=Decimal("100"),
            confirmed_qty=Decimal("100"),
            applied_individual_price=self.price,
        )
        other_venue_order = Order.objects.create(venue=self.other_venue, order_number="1535")
        OrderItem.objects.create(
            order=other_venue_order,
            supplier_product=self.supplier_product,
            requested_qty=Decimal("10"),
            confirmed_qty=Decimal("10"),
            applied_individual_price=self.price,
        )

        self.assertEqual(
            [r.id for r in individual_price_order_history_queryset(price_id=self.price.id, order_number="1534")],
            [item.id],
        )
        self.assertEqual(list(individual_price_order_history_queryset(price_id=self.price.id, order_number="9999")), [])
        self.assertEqual(
            [r.id for r in individual_price_order_history_queryset(price_id=self.price.id, venue_id=self.venue.id)],
            [item.id],
        )

    def test_filters_by_date_range(self):
        order = Order.objects.create(venue=self.venue, order_number="ORD-HIST-2")
        item = OrderItem.objects.create(
            order=order,
            supplier_product=self.supplier_product,
            requested_qty=Decimal("20"),
            confirmed_qty=Decimal("20"),
            applied_individual_price=self.price,
        )
        in_range_from = item.order.created_at - timedelta(days=1)
        in_range_to = item.order.created_at + timedelta(days=1)
        out_of_range_from = item.order.created_at + timedelta(days=1)

        self.assertEqual(
            [
                r.id
                for r in individual_price_order_history_queryset(
                    price_id=self.price.id, date_from=in_range_from, date_to=in_range_to
                )
            ],
            [item.id],
        )
        self.assertEqual(
            list(individual_price_order_history_queryset(price_id=self.price.id, date_from=out_of_range_from)), []
        )

    def test_orders_newest_first(self):
        older_order = Order.objects.create(venue=self.venue, order_number="ORD-HIST-OLD")
        older_item = OrderItem.objects.create(
            order=older_order,
            supplier_product=self.supplier_product,
            requested_qty=Decimal("5"),
            confirmed_qty=Decimal("5"),
            applied_individual_price=self.price,
        )
        newer_order = Order.objects.create(venue=self.venue, order_number="ORD-HIST-NEW")
        newer_item = OrderItem.objects.create(
            order=newer_order,
            supplier_product=self.supplier_product,
            requested_qty=Decimal("7"),
            confirmed_qty=Decimal("7"),
            applied_individual_price=self.price,
        )
        Order.objects.filter(pk=older_order.pk).update(created_at=timezone.now() - timedelta(days=2))

        rows = list(individual_price_order_history_queryset(price_id=self.price.id))
        self.assertEqual([r.id for r in rows], [newer_item.id, older_item.id])


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueCartRecipientAddressContactsAPITests(APITestCase):
    """2026-08-30, Vitya: "в свагере нет — получатель, адрес доставки, имя и
    фамилия, эл. почта, номер телефона" — макет заявки, доставка была
    отложена на v2 на созвоне 21.08, пользователь явно решил реализовать
    сейчас. Получатель = VenueSubdivision, адрес = VenueAddress (оба —
    уже существующие в заведении записи, не свободный текст), контакты —
    OrderContact (их может быть несколько на заявку).

    2026-09-09 (Витя: "можешь плиз это вынести вообще из корзины и
    принимать параметрами в /api/v1/catalog/cart/submit"): всё три поля
    больше не состояние корзины — переехали в тело POST /cart/submit,
    ровно там же, где и venue_id. PATCH .../cart и POST/DELETE
    .../cart/contacts под них больше не существуют."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.other_venue = Venue.objects.create(display_name="Other Cafe")
        self.owner = _make_venue_owner(self.venue)
        self.sp = _make_supplier_product(price="100.00")
        self.subdivision = VenueSubdivision.objects.create(venue=self.venue, name="Кухня")
        self.address = VenueAddress.objects.create(
            venue=self.venue, address_type="actual", address="г. Москва, ул. Тестовая, д. 1"
        )
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _add_item(self):
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        )

    def test_submit_sets_subdivision_and_delivery_address_on_order(self):
        self.authenticate()
        self._add_item()
        response = self.client.post(
            f"{self.cart_url}/submit",
            {
                "command_id": str(uuid4()),
                "expected_settings_version": 1,
                **(
                    {
                        "subdivision_id": str(self.subdivision.id),
                        "delivery_address_id": str(self.address.id),
                    }
                ),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        order_id = response.json()["id"]

        detail = self.client.get(f"/api/v1/catalog/venues/{self.venue.id}/orders/{order_id}").json()
        self.assertEqual(detail["subdivision_id"], str(self.subdivision.id))
        self.assertEqual(detail["subdivision_name"], "Кухня")
        self.assertEqual(detail["delivery_address_id"], str(self.address.id))
        self.assertEqual(detail["delivery_address"]["address"], "г. Москва, ул. Тестовая, д. 1")
        self.assertEqual(detail["delivery_address"]["address_type"], "actual")

    def test_submit_rejects_subdivision_from_another_venue(self):
        self.authenticate()
        self._add_item()
        foreign_subdivision = VenueSubdivision.objects.create(venue=self.other_venue, name="Бар")
        response = self.client.post(
            f"{self.cart_url}/submit",
            {
                "command_id": str(uuid4()),
                "expected_settings_version": 1,
                **({"subdivision_id": str(foreign_subdivision.id)}),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_submit_rejects_delivery_address_from_another_venue(self):
        self.authenticate()
        self._add_item()
        foreign_address = VenueAddress.objects.create(venue=self.other_venue, address_type="legal")
        response = self.client.post(
            f"{self.cart_url}/submit",
            {
                "command_id": str(uuid4()),
                "expected_settings_version": 1,
                **({"delivery_address_id": str(foreign_address.id)}),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_submit_without_subdivision_and_delivery_address_leaves_them_null(self):
        self.authenticate()
        self._add_item()
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        detail = self.client.get(f"/api/v1/catalog/venues/{self.venue.id}/orders/{order_id}").json()
        self.assertIsNone(detail["subdivision_id"])
        self.assertIsNone(detail["delivery_address_id"])
        self.assertIsNone(detail["delivery_address"])
        self.assertEqual(detail["contacts"], [])

    def test_cart_no_longer_exposes_subdivision_delivery_address_or_contacts(self):
        """2026-09-09: убедиться, что поля реально ушли с GET /cart, а не
        просто перестали быть settable."""
        self.authenticate()
        data = self.client.get(self.cart_url).json()
        for field in (
            "venue_id",
            "subdivision_id",
            "subdivision_name",
            "delivery_address_id",
            "delivery_address",
            "contacts",
        ):
            self.assertNotIn(field, data)

    def test_submit_creates_multiple_contacts(self):
        """Контактов для связи может быть несколько на одну заявку."""
        self.authenticate()
        self._add_item()
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {
                "command_id": str(uuid4()),
                "expected_settings_version": 1,
                **(
                    {
                        "contacts": [{"full_name": "Иван Иванов"}, {"full_name": "Пётр Петров"}],
                    }
                ),
            },
            format="json",
        ).json()["id"]

        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.contacts.count(), 2)
        names = set(order.contacts.values_list("full_name", flat=True))
        self.assertEqual(names, {"Иван Иванов", "Пётр Петров"})

    def test_submit_contact_requires_at_least_one_field(self):
        self.authenticate()
        self._add_item()
        response = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1, **({"contacts": [{}]})},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_submit_copies_recipient_address_and_contacts_onto_order(self):
        self.authenticate()
        self._add_item()

        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {
                "command_id": str(uuid4()),
                "expected_settings_version": 1,
                **(
                    {
                        "subdivision_id": str(self.subdivision.id),
                        "delivery_address_id": str(self.address.id),
                        "contacts": [
                            {"full_name": "Иван Иванов", "email": "ivan@example.com", "phone": "+79990001122"},
                            {"full_name": "Пётр Петров"},
                        ],
                    }
                ),
            },
            format="json",
        ).json()["id"]
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.subdivision_id, self.subdivision.id)
        self.assertEqual(order.delivery_address_id, self.address.id)
        self.assertEqual(order.contacts.count(), 2)
        names = set(order.contacts.values_list("full_name", flat=True))
        self.assertEqual(names, {"Иван Иванов", "Пётр Петров"})
        contact = order.contacts.get(full_name="Иван Иванов")
        self.assertEqual(contact.email, "ivan@example.com")
        self.assertEqual(contact.phone, "+79990001122")

        detail = self.client.get(f"/api/v1/catalog/venues/{self.venue.id}/orders/{order_id}").json()
        self.assertEqual(detail["subdivision_id"], str(self.subdivision.id))
        self.assertEqual(detail["subdivision_name"], "Кухня")
        self.assertEqual(detail["delivery_address_id"], str(self.address.id))
        self.assertEqual(len(detail["contacts"]), 2)


@override_settings(SERVICE_API_TOKEN="test-token")
class OffSystemOrderItemAndCompleteAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = _make_venue_owner(self.venue)
        self.sp = _make_supplier_product(price="100.00")
        self.off_system_supplier = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")
        self.subdivision = VenueSubdivision.objects.create(venue=self.venue, name="Кухня")
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"
        self.orders_url = f"/api/v1/catalog/venues/{self.venue.id}/orders"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def test_off_system_item_manual_status_flow_and_order_complete(self):
        self.authenticate()
        item_resp = self.client.post(
            f"{self.cart_url}/items",
            {
                "requested_name": "Микрозелень",
                "requested_qty": "2.0",
                "off_system_supplier_id": str(self.off_system_supplier.id),
                "subdivision_id": str(self.subdivision.id),
            },
            format="json",
        )
        self.assertEqual(item_resp.status_code, 201)
        item_data = item_resp.json()
        self.assertEqual(item_data["off_system_supplier_name"], "Рынок")
        self.assertEqual(item_data["subdivision_name"], "Кухня")

        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        item_id = str(Order.objects.get(pk=order_id).items.get().id)

        # Manual accept for the off-system item.
        response = self.client.post(
            f"{self.orders_url}/{order_id}/items/{item_id}/set-status",
            {"status": "confirmed", "confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "confirmed")
        # 2026-09-21: no real supplier account for off-system items — the
        # manual status change itself stands in for the "response".
        self.assertIsNotNone(response.json()["supplier_responded_at"])
        # 2026-09-14: этот эндпоинт не идёт через venue_order_detail_queryset —
        # select_related("venue") выставляется вручную в view, проверяем, что
        # не забыт.
        self.assertEqual(response.json()["venue_name"], self.venue.display_name)

        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.status, OrderStatus.CONFIRMED)

        # завершена — только вручную, только из confirmed.
        response = self.client.post(f"{self.orders_url}/{order_id}/complete")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(response.json()["venue_name"], self.venue.display_name)

        response = self.client.post(f"{self.orders_url}/{order_id}/complete")
        self.assertEqual(response.status_code, 409)

    def test_set_status_rejected_for_regular_supplier_item(self):
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        item_id = str(Order.objects.get(pk=order_id).items.get().id)

        response = self.client.post(
            f"{self.orders_url}/{order_id}/items/{item_id}/set-status", {"status": "confirmed"}, format="json"
        )
        self.assertEqual(response.status_code, 409)

    def test_set_status_optional_comment_echoed_as_supplier_comment(self):
        """2026-09-25, задача #Лк1: "Отказать в поставке?" на макете несёт
        необязательный комментарий — тот же выходной field supplier_comment,
        что у реального поставщика (2026-09-22), другой источник."""
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {
                "requested_name": "Микрозелень",
                "requested_qty": "2.0",
                "off_system_supplier_id": str(self.off_system_supplier.id),
            },
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        item_id = str(Order.objects.get(pk=order_id).items.get().id)

        response = self.client.post(
            f"{self.orders_url}/{order_id}/items/{item_id}/set-status",
            {"status": "cancelled", "comment": "не привезли вовремя"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["supplier_comment"], "не привезли вовремя")

    def test_set_status_without_comment_leaves_supplier_comment_null(self):
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {
                "requested_name": "Микрозелень",
                "requested_qty": "2.0",
                "off_system_supplier_id": str(self.off_system_supplier.id),
            },
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        item_id = str(Order.objects.get(pk=order_id).items.get().id)

        response = self.client.post(
            f"{self.orders_url}/{order_id}/items/{item_id}/set-status",
            {"status": "confirmed", "confirmed_delivery_date": _TEST_DELIVERY_DATE},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["supplier_comment"])

    def _submit_off_system_item(self):
        self.client.post(
            f"{self.cart_url}/items",
            {
                "requested_name": "Микрозелень",
                "requested_qty": "2.0",
                "off_system_supplier_id": str(self.off_system_supplier.id),
            },
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        return order_id, Order.objects.get(pk=order_id).items.get()

    def test_set_status_confirm_requires_delivery_date(self):
        """TZ «Поставщики "Не в системе"» §8: без даты подтверждение невозможно."""
        self.authenticate()
        order_id, item = self._submit_off_system_item()

        response = self.client.post(
            f"{self.orders_url}/{order_id}/items/{item.id}/set-status", {"status": "confirmed"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["confirmed_delivery_date"], "Укажите дату поставки, чтобы принять заявку.")
        item.refresh_from_db()
        self.assertEqual(item.status, OrderItemStatus.SUBMITTED)

    def test_set_status_decline_does_not_require_delivery_date(self):
        self.authenticate()
        order_id, item = self._submit_off_system_item()

        response = self.client.post(
            f"{self.orders_url}/{order_id}/items/{item.id}/set-status", {"status": "cancelled"}, format="json"
        )

        self.assertEqual(response.status_code, 200)

    def test_set_status_rejected_for_closed_item(self):
        """TZ §16: отменённую заведением или заменённую позицию повторно обработать нельзя."""
        self.authenticate()
        for closed_status in (OrderItemStatus.CANCELLED_BY_VENUE, OrderItemStatus.REPLACED):
            with self.subTest(status=closed_status):
                order_id, item = self._submit_off_system_item()
                OrderItem.objects.filter(pk=item.pk).update(status=closed_status)

                response = self.client.post(
                    f"{self.orders_url}/{order_id}/items/{item.id}/set-status",
                    {"status": "confirmed", "confirmed_delivery_date": _TEST_DELIVERY_DATE},
                    format="json",
                )

                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["detail"],
                    "Позиция отменена заведением или заменена, её статус больше нельзя менять.",
                )
                item.refresh_from_db()
                self.assertEqual(item.status, closed_status)

    def test_set_status_rejected_for_completed_order(self):
        self.authenticate()
        order_id, item = self._submit_off_system_item()
        url = f"{self.orders_url}/{order_id}/items/{item.id}/set-status"
        self.client.post(url, {"status": "confirmed", "confirmed_delivery_date": _TEST_DELIVERY_DATE}, format="json")
        self.assertEqual(self.client.post(f"{self.orders_url}/{order_id}/complete").status_code, 200)

        response = self.client.post(url, {"status": "cancelled"}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "Заказ завершён, статусы его позиций больше нельзя менять.")

    def test_set_status_can_return_processed_item_to_pending(self):
        """Возврат confirmed -> submitted остаётся разрешён: так исправляют ошибочно проставленный ответ."""
        self.authenticate()
        order_id, item = self._submit_off_system_item()
        url = f"{self.orders_url}/{order_id}/items/{item.id}/set-status"
        self.client.post(url, {"status": "confirmed", "confirmed_delivery_date": _TEST_DELIVERY_DATE}, format="json")

        response = self.client.post(url, {"status": "submitted"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "submitted")

    def test_regular_supplier_item_supplier_comment_unaffected_by_off_system_field(self):
        """supplier_comment реального поставщика по-прежнему читается из
        SupplierOrderRequest, а не off_system_status_comment — оба источника
        не должны пересекаться."""
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        item = Order.objects.get(pk=order_id).items.get()
        self.assertIsNone(item.off_system_supplier_id)
        detail = self.client.get(f"{self.orders_url}/{order_id}").json()
        self.assertIsNone(_grouped_order_items(detail)[0]["supplier_comment"])

    def test_complete_rejected_when_not_confirmed(self):
        self.authenticate()
        self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": "1.0"},
            format="json",
        )
        order_id = self.client.post(
            f"{self.cart_url}/submit",
            {"command_id": str(uuid4()), "expected_settings_version": 1},
            format="json",
        ).json()["id"]
        response = self.client.post(f"{self.orders_url}/{order_id}/complete")
        self.assertEqual(response.status_code, 409)


class MasterProductModelTests(TestCase):
    def test_create_and_str(self):
        owner = User.objects.create(email="owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        mp = MasterProduct.objects.create(owner=owner, canonical_name="Мука пшеничная в/с")
        self.assertEqual(mp.status, EntityStatus.ACTIVE)
        self.assertIsNone(mp.unit)
        self.assertIsNone(mp.net_value)
        self.assertIn("Мука пшеничная в/с", str(mp))

    def test_owner_delete_is_restricted(self):
        owner = User.objects.create(email="owner2@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        MasterProduct.objects.create(owner=owner, canonical_name="Соль")
        with self.assertRaises(IntegrityError):
            owner.delete()


class VenueProductMatrixItemMasterProductFieldsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner3@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.mp = MasterProduct.objects.create(owner=self.owner, canonical_name="Сахар")
        self.venue = Venue.objects.create(display_name="Cafe A")

    def test_defaults(self):
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сахар песок")
        self.assertIsNone(item.master_product)
        self.assertEqual(item.source, VenueProductMatrixItemSource.MANUAL)
        self.assertFalse(item.needs_review)

    def test_unique_master_product_per_venue(self):
        VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сахар песок", master_product=self.mp)
        with self.assertRaises(IntegrityError):
            VenueProductMatrixItem.objects.create(
                venue=self.venue, requested_name="Сахар рафинад", master_product=self.mp
            )

    def test_master_product_delete_sets_null(self):
        item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Сахар песок", master_product=self.mp
        )
        self.mp.delete()
        item.refresh_from_db()
        self.assertIsNone(item.master_product)


class MasterProductSelectorsAndServicesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner4@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe B")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )

    def test_create_and_queryset_companies_count(self):
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        qs = master_products_queryset(self.owner.id)
        self.assertEqual(list(qs), [mp])
        self.assertEqual(qs.first().companies_count, 0)

        link_master_product_to_venue(master_product=mp, venue=self.venue)
        self.assertEqual(master_products_queryset(self.owner.id).first().companies_count, 1)

    def test_update_and_delete(self):
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        update_master_product(mp, canonical_name="Рис круглозёрный")
        mp.refresh_from_db()
        self.assertEqual(mp.canonical_name, "Рис круглозёрный")

        delete_master_product(mp)
        self.assertFalse(MasterProduct.objects.filter(pk=mp.pk).exists())

    def test_link_rejects_venue_not_owned_by_owner(self):
        other_venue = Venue.objects.create(display_name="Cafe C")
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        with self.assertRaises(ValidationError):
            link_master_product_to_venue(master_product=mp, venue=other_venue)

    def test_link_rejects_duplicate(self):
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        link_master_product_to_venue(master_product=mp, venue=self.venue)
        with self.assertRaises(ValidationError):
            link_master_product_to_venue(master_product=mp, venue=self.venue)

    def test_link_rejects_duplicate_even_when_existing_link_is_archived(self):
        # TASK-46: vpmi_master_product_venue_uniq has no status filter, so an
        # archived row still occupies the (master_product, venue) slot at the
        # DB level — the service-level occupancy check must catch this too,
        # instead of letting .create() below hit an unhandled IntegrityError.
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        item = link_master_product_to_venue(master_product=mp, venue=self.venue)
        delete_product_matrix_item(item)
        with self.assertRaises(ValidationError):
            link_master_product_to_venue(master_product=mp, venue=self.venue, force_create=True)

    def test_link_raises_conflict_when_similar_unlinked_item_exists(self):
        # §16 ТЗ / TASK-33: must not silently create a duplicate when the
        # venue's own catalog already has an unlinked item with the same
        # name (case-insensitive).
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        candidate = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="рис")
        with self.assertRaises(MasterProductVenueLinkConflict) as ctx:
            link_master_product_to_venue(master_product=mp, venue=self.venue)
        self.assertEqual(ctx.exception.candidate.id, candidate.id)
        self.assertEqual(VenueProductMatrixItem.objects.filter(venue=self.venue).count(), 1)

    def test_link_force_create_ignores_similar_unlinked_item(self):
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="рис")
        item = link_master_product_to_venue(master_product=mp, venue=self.venue, force_create=True)
        self.assertEqual(item.master_product_id, mp.id)
        self.assertEqual(VenueProductMatrixItem.objects.filter(venue=self.venue).count(), 2)

    def test_link_ignores_candidate_already_linked_to_another_master_product(self):
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        other_mp = create_master_product(owner=self.owner, canonical_name="Гречка")
        link_master_product_to_venue(master_product=other_mp, venue=self.venue)
        VenueProductMatrixItem.objects.filter(venue=self.venue).update(requested_name="рис")
        item = link_master_product_to_venue(master_product=mp, venue=self.venue)
        self.assertEqual(item.master_product_id, mp.id)
        self.assertEqual(VenueProductMatrixItem.objects.filter(venue=self.venue).count(), 2)

    def test_link_existing_item_id_links_without_creating_new_row(self):
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        candidate = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Рис басмати")
        item = link_master_product_to_venue(master_product=mp, venue=self.venue, link_existing_item_id=candidate.id)
        self.assertEqual(item.id, candidate.id)
        self.assertEqual(item.master_product_id, mp.id)
        self.assertEqual(VenueProductMatrixItem.objects.filter(venue=self.venue).count(), 1)

    def test_link_existing_item_id_rejects_row_from_other_venue(self):
        other_venue = Venue.objects.create(display_name="Cafe E")
        VenueUser.objects.create(
            venue=other_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        foreign_item = VenueProductMatrixItem.objects.create(venue=other_venue, requested_name="Рис")
        with self.assertRaises(ValidationError):
            link_master_product_to_venue(master_product=mp, venue=self.venue, link_existing_item_id=foreign_item.id)

    def test_link_existing_item_id_rejects_row_already_linked(self):
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        other_mp = create_master_product(owner=self.owner, canonical_name="Гречка")
        already_linked = link_master_product_to_venue(master_product=other_mp, venue=self.venue)
        with self.assertRaises(ValidationError):
            link_master_product_to_venue(master_product=mp, venue=self.venue, link_existing_item_id=already_linked.id)

    def test_unlink_clears_master_product_without_deleting_row(self):
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        item = link_master_product_to_venue(master_product=mp, venue=self.venue)
        unlink_master_product_from_venue(master_product=mp, venue_id=self.venue.id)
        item.refresh_from_db()
        self.assertIsNone(item.master_product)
        self.assertTrue(VenueProductMatrixItem.objects.filter(pk=item.pk).exists())

    def test_venue_breakdown_mixed_linked_and_unlinked(self):
        other_venue = Venue.objects.create(display_name="Cafe D")
        VenueUser.objects.create(
            venue=other_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        link_master_product_to_venue(master_product=mp, venue=self.venue)

        rows = {row.venue_id: row for row in master_product_venue_breakdown(mp)}
        self.assertTrue(rows[self.venue.id].linked)
        self.assertEqual(rows[self.venue.id].requested_name, "Рис")
        self.assertFalse(rows[other_venue.id].linked)
        self.assertIsNone(rows[other_venue.id].matrix_item_id)

    def test_companies_count_excludes_archived_venue(self):
        # 2026-09-01 fix wave (Fix 2): companies_count must stay in lockstep
        # with master_product_venue_breakdown's scoping (active matrix item,
        # active venue, venue currently owned by the owner) — an archived
        # venue (delete_venue_cabinet's soft-delete path: Venue.status set to
        # ARCHIVED, VenueUser/matrix-item rows left in place) must drop out
        # of both, not just the breakdown.
        mp = create_master_product(owner=self.owner, canonical_name="Рис")
        link_master_product_to_venue(master_product=mp, venue=self.venue)
        self.assertEqual(master_products_queryset(self.owner.id).first().companies_count, 1)
        self.assertEqual(master_product_detail_queryset().get(pk=mp.pk).companies_count, 1)

        self.venue.status = EntityStatus.ARCHIVED
        self.venue.save(update_fields=["status", "updated_at"])

        self.assertEqual(master_products_queryset(self.owner.id).first().companies_count, 0)
        self.assertEqual(master_product_detail_queryset().get(pk=mp.pk).companies_count, 0)
        breakdown_linked_count = sum(1 for row in master_product_venue_breakdown(mp) if row.linked)
        self.assertEqual(breakdown_linked_count, 0)


class MasterCatalogPermissionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.owner = User.objects.create(email="owner5@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.manager_user = User.objects.create(email="mgr@example.com", full_name="Manager", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(
            email="stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe E")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.venue, user=self.manager_user, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.mp = MasterProduct.objects.create(owner=self.owner, canonical_name="Молоко")

    def _request(self, user, query_params=None):
        request = self.factory.get("/", query_params or {})
        request.user = user
        request.query_params = request.GET
        return request

    def test_owner_can_access_own(self):
        request = self._request(self.owner, {"owner_id": str(self.owner.id)})
        self.assertTrue(HasMasterCatalogAccessToOwner().has_permission(request, None))

    def test_manager_can_access_owners_catalog(self):
        request = self._request(self.manager_user, {"owner_id": str(self.owner.id)})
        self.assertTrue(HasMasterCatalogAccessToOwner().has_permission(request, None))

    def test_stranger_is_denied(self):
        request = self._request(self.stranger, {"owner_id": str(self.owner.id)})
        self.assertFalse(HasMasterCatalogAccessToOwner().has_permission(request, None))

    def test_missing_owner_id_defers_to_view_validation(self):
        # 2026-09-01 fix wave (Fix 1): a missing owner_id is a request-shape
        # problem, not an authorization one — MasterProductListQuerySerializer
        # in the view is what now rejects it with 400, so the permission
        # defers (True) rather than reporting a misleading 403.
        request = self._request(self.owner)
        self.assertTrue(HasMasterCatalogAccessToOwner().has_permission(request, None))

    def test_malformed_owner_id_defers_to_view_validation(self):
        request = self._request(self.owner, {"owner_id": "not-a-uuid"})
        self.assertTrue(HasMasterCatalogAccessToOwner().has_permission(request, None))

    def test_manager_with_access_to_only_one_of_owners_two_venues_still_has_access(self):
        # 2026-08-31 spec rule 14 differentiator: owner has 2 venues, manager
        # has staff (MANAGER) access to only ONE of them — access to the
        # owner's master catalog must still be granted via that one shared
        # venue.
        second_venue = Venue.objects.create(display_name="Cafe E2")
        VenueUser.objects.create(
            venue=second_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        # manager_user has no VenueUser row on second_venue at all.
        request = self._request(self.manager_user, {"owner_id": str(self.owner.id)})
        self.assertTrue(HasMasterCatalogAccessToOwner().has_permission(request, None))

    def test_detail_permission_resolves_owner_from_object(self):
        view = SimpleNamespace(kwargs={"master_product_id": str(self.mp.id)})
        request = self._request(self.manager_user)
        self.assertTrue(HasMasterCatalogAccess().has_permission(request, view))

        request = self._request(self.stranger)
        self.assertFalse(HasMasterCatalogAccess().has_permission(request, view))

    def test_detail_permission_denied_for_nonexistent_master_product(self):
        view = SimpleNamespace(kwargs={"master_product_id": str(uuid4())})
        request = self._request(self.owner)
        self.assertFalse(HasMasterCatalogAccess().has_permission(request, view))


class MasterProductListCreateAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner6@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.manager_user = User.objects.create(email="mgr2@example.com", full_name="Manager", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(
            email="stranger2@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe F")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.venue, user=self.manager_user, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.url = "/api/v1/catalog/master-products"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_create_by_owner(self):
        self.auth(self.owner)
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "canonical_name": "Мука"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["canonical_name"], "Мука")
        self.assertEqual(data["companies_count"], 0)
        self.assertIsNone(data["unit_id"])

    def test_create_by_manager_for_owner(self):
        self.auth(self.manager_user)
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "canonical_name": "Мука"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)

    def test_create_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "canonical_name": "Мука"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_create_requires_authentication(self):
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "canonical_name": "Мука"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_list_scoped_to_owner_id(self):
        self.auth(self.owner)
        self.client.post(self.url, {"owner_id": str(self.owner.id), "canonical_name": "Мука"}, format="json")
        response = self.client.get(self.url, {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 1)

    def test_list_missing_owner_id_returns_400(self):
        # 2026-09-01 fix wave (Fix 1): missing owner_id is now a 400
        # (MasterProductListQuerySerializer), not a 403 — it's a malformed
        # request, not an authorization failure.
        self.auth(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)

    def test_list_malformed_owner_id_returns_400(self):
        self.auth(self.owner)
        response = self.client.get(self.url, {"owner_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_list_missing_owner_id_returns_400_for_admin(self):
        # Closes the admin bypass (Fix 1): previously ADMIN's unconditional
        # permission short-circuit let a missing owner_id reach
        # master_products_queryset(None), silently producing 200 [].
        admin = User.objects.create(email="admin1@example.com", full_name="Admin", role=UserRole.ADMIN)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(admin))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)

    def test_list_malformed_owner_id_returns_400_for_admin(self):
        # Closes the admin bypass (Fix 1): previously ADMIN's unconditional
        # permission short-circuit let a malformed owner_id reach
        # master_products_queryset(), raising an uncaught 500.
        admin = User.objects.create(email="admin2@example.com", full_name="Admin", role=UserRole.ADMIN)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(admin))
        response = self.client.get(self.url, {"owner_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_create_non_dict_body_returns_403(self):
        # Fix 6: request.data can be a non-dict (JSON array) — must not
        # AttributeError -> 500 when the permission calls .get() on it.
        self.auth(self.owner)
        response = self.client.post(self.url, ["not", "a", "dict"], format="json")
        self.assertEqual(response.status_code, 403, response.content)

    def test_search_by_q_filters_by_canonical_name_substring(self):
        self.auth(self.owner)
        flour = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука пшеничная")
        MasterProduct.objects.create(owner=self.owner, canonical_name="Сахар")
        response = self.client.get(self.url, {"owner_id": str(self.owner.id), "q": "мука"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(flour.id)])

    def test_search_by_q_is_case_insensitive(self):
        self.auth(self.owner)
        flour = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука пшеничная")
        response = self.client.get(self.url, {"owner_id": str(self.owner.id), "q": "МУКА"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(flour.id)])

    def test_search_by_q_no_match_returns_empty_list(self):
        self.auth(self.owner)
        MasterProduct.objects.create(owner=self.owner, canonical_name="Мука пшеничная")
        response = self.client.get(self.url, {"owner_id": str(self.owner.id), "q": "Несуществующий товар"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_search_by_q_matches_words_in_any_order(self):
        self.auth(self.owner)
        flour = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука пшеничная высший сорт")
        MasterProduct.objects.create(owner=self.owner, canonical_name="Мука рисовая")

        response = self.client.get(self.url, {"owner_id": str(self.owner.id), "q": "пшеничная мука"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(flour.id)])

    def test_search_by_q_corrects_english_keyboard_layout(self):
        self.auth(self.owner)
        flour = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука пшеничная")

        response = self.client.get(self.url, {"owner_id": str(self.owner.id), "q": "verf"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(flour.id)])

    @skipUnless(connection.vendor == "postgresql", "pg_trgm is PostgreSQL-only")
    def test_search_by_q_tolerates_typo(self):
        self.auth(self.owner)
        flour = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука пшеничная")

        response = self.client.get(self.url, {"owner_id": str(self.owner.id), "q": "пшенична"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(flour.id)])

    def test_blank_q_returns_all(self):
        self.auth(self.owner)
        MasterProduct.objects.create(owner=self.owner, canonical_name="Мука пшеничная")
        MasterProduct.objects.create(owner=self.owner, canonical_name="Сахар")
        response = self.client.get(self.url, {"owner_id": str(self.owner.id), "q": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 2)

    def test_list_response_is_paginated_envelope(self):
        self.auth(self.owner)
        MasterProduct.objects.create(owner=self.owner, canonical_name="Мука")
        response = self.client.get(self.url, {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data.keys()), {"count", "next", "previous", "results"})
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["canonical_name"], "Мука")

    def test_list_default_page_size_is_20(self):
        self.auth(self.owner)
        for i in range(25):
            MasterProduct.objects.create(owner=self.owner, canonical_name=f"Товар {i:02d}")
        response = self.client.get(self.url, {"owner_id": str(self.owner.id)})
        data = response.json()
        self.assertEqual(data["count"], 25)
        self.assertEqual(len(data["results"]), 20)
        self.assertIsNotNone(data["next"])

    def test_list_second_page_returns_remaining_items(self):
        self.auth(self.owner)
        for i in range(25):
            MasterProduct.objects.create(owner=self.owner, canonical_name=f"Товар {i:02d}")
        response = self.client.get(self.url, {"owner_id": str(self.owner.id), "page": "2"})
        data = response.json()
        self.assertEqual(len(data["results"]), 5)
        self.assertIsNone(data["next"])

    def test_list_page_size_query_param_overrides_default(self):
        self.auth(self.owner)
        for i in range(5):
            MasterProduct.objects.create(owner=self.owner, canonical_name=f"Товар {i}")
        response = self.client.get(self.url, {"owner_id": str(self.owner.id), "page_size": "2"})
        data = response.json()
        self.assertEqual(data["count"], 5)
        self.assertEqual(len(data["results"]), 2)


class TenderListCreateAPITests(APITestCase):
    """2026-09-14: приоритетные ручки по просьбе Дмитрия Подкопаева
    (docs/wiki/logs/2026-09-14-sync-call-podkopaev-onboarding.md) — только
    сам тендер (create + list), без позиций/предложений/распределений
    (raw ТЗ §3.2-3.4/§9 — отдельные, пока не реализованные ручки).
    Owner-scoping/permission-тесты зеркалят MasterProductListCreateAPITests
    (тот же HasTenderAccessToOwner, та же _user_has_master_catalog_access)."""

    def setUp(self):
        self.owner = User.objects.create(email="tender-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.manager_user = User.objects.create(
            email="tender-mgr@example.com", full_name="Manager", role=UserRole.VENUE_USER
        )
        self.stranger = User.objects.create(
            email="tender-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Tender")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.venue, user=self.manager_user, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.supplier = Supplier.objects.create(legal_name="Tender Supplier LLC", display_name="Tender Supplier")
        self.url = "/api/v1/catalog/tenders"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_create_open_tender_by_owner(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "name": "Осеннее меню 2026", "access": "open"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["name"], "Осеннее меню 2026")
        self.assertEqual(data["access"], "open")
        self.assertEqual(data["status"], "draft")
        self.assertIsNone(data["accepts_offers_until"])
        self.assertEqual(data["invited_supplier_ids"], [])

    def test_create_by_manager_for_owner(self):
        self.auth(self.manager_user)
        response = self.client.post(
            self.url, {"owner_id": str(self.owner.id), "name": "Мука 2026", "access": "open"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_create_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.post(
            self.url, {"owner_id": str(self.owner.id), "name": "Мука 2026", "access": "open"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_create_requires_authentication(self):
        response = self.client.post(
            self.url, {"owner_id": str(self.owner.id), "name": "Мука 2026", "access": "open"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_create_invite_only_with_suppliers(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "name": "Закрытый тендер",
                "access": "invite_only",
                "invited_suppliers": [str(self.supplier.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["invited_supplier_ids"], [str(self.supplier.id)])

    def test_create_invite_only_without_suppliers_rejected(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "name": "Закрытый тендер", "access": "invite_only"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("invited_suppliers", response.json())

    def test_create_open_with_suppliers_rejected(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "name": "Открытый тендер",
                "access": "open",
                "invited_suppliers": [str(self.supplier.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("invited_suppliers", response.json())

    def test_create_with_deadline(self):
        # Relative, not a fixed date: the deadline must be in the future (§7), a fixed one expires.
        deadline = (timezone.now() + timedelta(days=30)).astimezone(dt_timezone.utc).replace(microsecond=0)
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "name": "Тендер со сроком",
                "access": "open",
                "accepts_offers_until": deadline.isoformat().replace("+00:00", "Z"),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        returned = dt.fromisoformat(response.json()["accepts_offers_until"])
        self.assertEqual(returned.astimezone(dt_timezone.utc), deadline)

    def test_create_with_items_creates_tender_and_positions(self):
        """TASK-66 (raw ТЗ §5.2-5.3, кнопка «Собрать предложения»): тендер
        и позиции создаются одним вызовом POST /tenders."""
        unit = Unit.objects.create(code="pcs-t66", name="шт")
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Соль", unit=unit)
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "name": "Собранный тендер",
                "access": "open",
                "items": [{"master_product_id": str(master_product.id), "venue_ids": [str(self.venue.id)]}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        tender_id = response.json()["id"]
        items = TenderItem.objects.filter(tender_id=tender_id)
        self.assertEqual(items.count(), 1)
        self.assertEqual(list(items[0].venues.values_list("id", flat=True)), [self.venue.id])

    def test_create_with_empty_items_list_creates_tender_without_positions(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "name": "Пустой сбор", "access": "open", "items": []},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(TenderItem.objects.filter(tender_id=response.json()["id"]).count(), 0)

    def test_create_with_invalid_item_creates_nothing(self):
        """Атомарность: невалидная позиция откатывает и создание самого
        тендера, а не только позиций."""
        unit = Unit.objects.create(code="pcs-t66b", name="шт")
        other_owner = User.objects.create(
            email="t66-other-owner@example.com", full_name="Other", role=UserRole.VENUE_USER
        )
        foreign_master_product = MasterProduct.objects.create(owner=other_owner, canonical_name="Не моё", unit=unit)
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "name": "Не должен создаться",
                "access": "open",
                "items": [{"master_product_id": str(foreign_master_product.id), "venue_ids": [str(self.venue.id)]}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Tender.objects.filter(name="Не должен создаться").exists())

    def test_list_scoped_to_owner_id(self):
        self.auth(self.owner)
        self.client.post(
            self.url, {"owner_id": str(self.owner.id), "name": "Тендер 1", "access": "open"}, format="json"
        )
        response = self.client.get(self.url, {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 1)

    def test_list_does_not_include_other_owners_tenders(self):
        other_owner = User.objects.create(email="other-owner@example.com", full_name="Other", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other_owner))
        self.client.post(
            self.url, {"owner_id": str(other_owner.id), "name": "Чужой тендер", "access": "open"}, format="json"
        )
        self.auth(self.owner)
        response = self.client.get(self.url, {"owner_id": str(self.owner.id)})
        self.assertEqual(response.json()["results"], [])

    def test_list_missing_owner_id_returns_400(self):
        self.auth(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)

    def test_list_malformed_owner_id_returns_400(self):
        self.auth(self.owner)
        response = self.client.get(self.url, {"owner_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_list_response_is_paginated_envelope(self):
        self.auth(self.owner)
        self.client.post(
            self.url, {"owner_id": str(self.owner.id), "name": "Тендер 1", "access": "open"}, format="json"
        )
        response = self.client.get(self.url, {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data.keys()), {"count", "next", "previous", "results"})
        self.assertEqual(data["count"], 1)

    def test_list_query_count_does_not_grow_with_invite_only_tender_count(self):
        """Страхует prefetch_related("invited_suppliers") в tenders_queryset —
        без него каждый invite_only-тендер добавлял бы свой запрос ради
        invited_supplier_ids."""
        self.auth(self.owner)
        for i in range(3):
            self.client.post(
                self.url,
                {
                    "owner_id": str(self.owner.id),
                    "name": f"Тендер {i}",
                    "access": "invite_only",
                    "invited_suppliers": [str(self.supplier.id)],
                },
                format="json",
            )
        with self.assertNumQueries(5):
            response = self.client.get(self.url, {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)


class TenderDetailAPITests(APITestCase):
    """TASK-83 (raw ТЗ §17): the buyer's tender card, GET /tenders/{id}, and
    the items_count/offers_count counters every Tender response carries
    (docs/adr/0002). Counters cover all positions, unpublished included,
    and every current offer on them — same as the §41 completion summary."""

    def setUp(self):
        self.owner = User.objects.create(email="card-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.manager_user = User.objects.create(
            email="card-mgr@example.com", full_name="Manager", role=UserRole.VENUE_USER
        )
        self.stranger = User.objects.create(
            email="card-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Card")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.venue, user=self.manager_user, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.unit = Unit.objects.create(code="kg-card", name="кг")
        self.supplier_a = Supplier.objects.create(legal_name="Card A LLC", display_name="Card A")
        self.supplier_b = Supplier.objects.create(legal_name="Card B LLC", display_name="Card B")
        self.tender = Tender.objects.create(created_by=self.owner, name="Карточка", access=TenderAccess.OPEN)
        self.url = f"/api/v1/catalog/tenders/{self.tender.id}"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def add_item(self, tender, name, published=True):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name=name, unit=self.unit)
        item = TenderItem.objects.create(tender=tender, master_product=master_product, published=published)
        item.venues.set([self.venue])
        return item

    def add_offer(self, item, supplier):
        product = SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name=f"{item.master_product.canonical_name} от {supplier.display_name}",
            normalized_name=item.master_product.canonical_name,
            unit=self.unit,
        )
        offer = TenderOffer.objects.create(
            tender_item=item, supplier=supplier, supplier_product=product, price=Decimal("100.00")
        )
        offer.venues.set([self.venue])
        return offer

    def get_card(self):
        self.auth(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_requires_authentication(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_other_owner_gets_403(self):
        self.auth(self.stranger)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_manager_of_owner_venue_can_read(self):
        self.auth(self.manager_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)

    def test_card_has_tender_fields(self):
        data = self.get_card()
        self.assertEqual(data["id"], str(self.tender.id))
        self.assertEqual(data["owner_id"], str(self.owner.id))
        self.assertEqual(data["name"], "Карточка")
        self.assertEqual(data["status"], "draft")
        self.assertEqual(data["access"], "open")
        self.assertIsNone(data["accepts_offers_until"])
        self.assertEqual(data["invited_supplier_ids"], [])

    def test_counters_empty_tender(self):
        data = self.get_card()
        self.assertEqual((data["items_count"], data["offers_count"]), (0, 0))

    def test_counters_one_item_one_offer(self):
        self.add_offer(self.add_item(self.tender, "Сливки"), self.supplier_a)
        data = self.get_card()
        self.assertEqual((data["items_count"], data["offers_count"]), (1, 1))

    def test_counters_many_items_and_offers(self):
        cream = self.add_item(self.tender, "Сливки")
        cheese = self.add_item(self.tender, "Камамбер")
        self.add_item(self.tender, "Мука")
        self.add_offer(cream, self.supplier_a)
        self.add_offer(cream, self.supplier_b)
        self.add_offer(cheese, self.supplier_a)
        other = Tender.objects.create(created_by=self.owner, name="Другой", access=TenderAccess.OPEN)
        self.add_offer(self.add_item(other, "Соль"), self.supplier_a)

        data = self.get_card()
        self.assertEqual((data["items_count"], data["offers_count"]), (3, 3))

    def test_counters_include_unpublished_item_and_its_offers(self):
        hidden = self.add_item(self.tender, "Снятая", published=False)
        self.add_offer(hidden, self.supplier_a)
        self.add_item(self.tender, "Видимая")
        data = self.get_card()
        self.assertEqual((data["items_count"], data["offers_count"]), (2, 1))

    def assert_status_after_read(self, deadline_shift, expected_status):
        now = timezone.now()
        self.tender.status = TenderStatus.ACTIVE
        self.tender.accepts_offers_until = now + deadline_shift
        self.tender.save()
        with patch("django.utils.timezone.now", return_value=now):
            data = self.get_card()
        self.assertEqual(data["status"], expected_status)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, expected_status)

    def test_deadline_minute_ago_completes_on_read(self):
        self.assert_status_after_read(-timedelta(minutes=1), "completed")

    def test_deadline_exactly_now_completes_on_read(self):
        self.assert_status_after_read(timedelta(0), "completed")

    def test_deadline_in_a_minute_stays_active(self):
        self.assert_status_after_read(timedelta(minutes=1), "active")

    def test_list_rows_carry_counters(self):
        self.add_offer(self.add_item(self.tender, "Сливки"), self.supplier_a)
        self.add_item(self.tender, "Мука")
        empty = Tender.objects.create(created_by=self.owner, name="Пустой", access=TenderAccess.OPEN)
        self.auth(self.owner)
        response = self.client.get("/api/v1/catalog/tenders", {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200, response.content)
        counters = {row["id"]: (row["items_count"], row["offers_count"]) for row in response.json()["results"]}
        self.assertEqual(counters, {str(self.tender.id): (2, 1), str(empty.id): (0, 0)})

    def test_list_query_count_does_not_grow_with_items_and_offers(self):
        for i in range(3):
            tender = Tender.objects.create(created_by=self.owner, name=f"Тендер {i}", access=TenderAccess.OPEN)
            for name in ("Сливки", "Мука"):
                item = self.add_item(tender, f"{name} {i}")
                self.add_offer(item, self.supplier_a)
                self.add_offer(item, self.supplier_b)
        self.auth(self.owner)
        with self.assertNumQueries(5):
            response = self.client.get("/api/v1/catalog/tenders", {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200)
        rows = [r for r in response.json()["results"] if r["id"] != str(self.tender.id)]
        self.assertEqual([(r["items_count"], r["offers_count"]) for r in rows], [(2, 4)] * 3)

    def test_create_response_carries_counters(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Соль", unit=self.unit)
        self.auth(self.owner)
        response = self.client.post(
            "/api/v1/catalog/tenders",
            {
                "owner_id": str(self.owner.id),
                "name": "Собранный",
                "access": "open",
                "items": [{"master_product_id": str(master_product.id), "venue_ids": [str(self.venue.id)]}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((response.json()["items_count"], response.json()["offers_count"]), (1, 0))

    def test_publish_response_carries_counters(self):
        self.add_item(self.tender, "Сливки")
        self.auth(self.owner)
        response = self.client.post(f"{self.url}/publish")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["items_count"], response.json()["offers_count"]), (1, 0))

    def test_complete_response_carries_counters(self):
        self.tender.status = TenderStatus.ACTIVE
        self.tender.save()
        self.add_offer(self.add_item(self.tender, "Сливки"), self.supplier_a)
        self.auth(self.owner)
        response = self.client.post(f"{self.url}/complete")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual((response.json()["items_count"], response.json()["offers_count"]), (1, 1))


class TenderItemListCreateAPITests(APITestCase):
    """Raw ТЗ §9-11: adding positions to a tender. Owner-scoping mirrors
    TenderListCreateAPITests (same HasTenderAccess-family reasoning)."""

    def setUp(self):
        self.owner = User.objects.create(email="ti-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(email="ti-stranger@example.com", full_name="X", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Item")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.other_venue = Venue.objects.create(display_name="Not Mine")
        self.unit = Unit.objects.create(code="l", name="литр")
        self.master_product = MasterProduct.objects.create(
            owner=self.owner, canonical_name="Сливки 33%", unit=self.unit
        )
        self.tender = Tender.objects.create(created_by=self.owner, name="Осеннее меню", access=TenderAccess.OPEN)
        self.url = f"/api/v1/catalog/tenders/{self.tender.id}/items"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_add_item_by_owner(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {
                "items": [
                    {
                        "master_product_id": str(self.master_product.id),
                        "product_kind_label": "Сливки",
                        "comment": "33-35%",
                        "venue_ids": [str(self.venue.id)],
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()[0]
        self.assertEqual(data["master_product_name"], "Сливки 33%")
        self.assertEqual(data["unit_code"], "l")
        self.assertEqual(data["product_kind_label"], "Сливки")
        self.assertEqual(data["venue_ids"], [str(self.venue.id)])

    def test_add_item_rejects_venue_not_owned(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"items": [{"master_product_id": str(self.master_product.id), "venue_ids": [str(self.other_venue.id)]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("venue_ids", response.json())

    def test_add_item_rejects_nonexistent_venue_id(self):
        """Final-review Finding 1: a venue_id that doesn't resolve to any
        Venue row must 400, not silently vanish from the set and leave the
        item created with zero venues (permanently undistributable, no
        error surfaced anywhere)."""
        self.auth(self.owner)
        bogus_venue_id = str(uuid4())
        response = self.client.post(
            self.url,
            {"items": [{"master_product_id": str(self.master_product.id), "venue_ids": [bogus_venue_id]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("venue_ids", response.json())
        self.assertFalse(TenderItem.objects.filter(tender=self.tender, master_product=self.master_product).exists())

    def test_add_duplicate_master_product_rejected(self):
        self.auth(self.owner)
        TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        response = self.client.post(
            self.url,
            {"items": [{"master_product_id": str(self.master_product.id), "venue_ids": [str(self.venue.id)]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_add_item_without_venue_ids_auto_derives_from_existing_links(self):
        """TASK-68 (raw ТЗ §10, Павел подтвердил в Telegram 2026-09-17):
        если venue_ids не передан вообще, заведения подтягиваются сами —
        из уже существующих VenueProductMatrixItem-связей мастер-товара с
        заведениями владельца тендера."""
        VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Сливки", master_product=self.master_product
        )
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"items": [{"master_product_id": str(self.master_product.id)}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()[0]["venue_ids"], [str(self.venue.id)])

    def test_add_item_without_venue_ids_and_no_links_creates_with_empty_venues(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"items": [{"master_product_id": str(self.master_product.id)}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()[0]["venue_ids"], [])

    def test_add_item_auto_derive_ignores_links_on_venues_not_owned(self):
        VenueProductMatrixItem.objects.create(
            venue=self.other_venue, requested_name="Сливки", master_product=self.master_product
        )
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"items": [{"master_product_id": str(self.master_product.id)}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()[0]["venue_ids"], [])

    def test_add_item_explicit_empty_venue_ids_means_no_venues(self):
        """TASK-106 (ТЗ «Мастер-справочник — создание тендера» §12-14): the buyer
        may uncheck every suggested venue; [] keeps the position without venues
        instead of auto-selecting (publishing still refuses it, §16). Until
        2026-09-28 an explicit [] was rejected with 400 (TASK-68)."""
        VenueProductMatrixItem.objects.create(
            venue=self.venue, master_product=self.master_product, requested_name="Связан"
        )
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"items": [{"master_product_id": str(self.master_product.id), "venue_ids": []}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()[0]["venue_ids"], [])

    def test_add_item_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.post(
            self.url,
            {"items": [{"master_product_id": str(self.master_product.id), "venue_ids": [str(self.venue.id)]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_list_items(self):
        TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.auth(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)


class TenderItemPublishAPITests(APITestCase):
    """TASK-59 (raw ТЗ §18, §75): публикация/снятие с публикации отдельной
    позиции тендера — колонка "Публикация" в таблице товаров карточки
    тендера. Позиция создаётся уже опубликованной (§18 пример — обе
    строки "Опубликован")."""

    def setUp(self):
        self.owner = User.objects.create(email="ip-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(email="ip-stranger@example.com", full_name="X", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe ItemPublish")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="kg-ip", name="кг")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Бри", unit=unit)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Сыры-2", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])
        self.publish_url = f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/publish"
        self.unpublish_url = f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/unpublish"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_item_created_published_by_default(self):
        self.auth(self.owner)
        response = self.client.get(f"/api/v1/catalog/tenders/{self.tender.id}/items")
        self.assertTrue(response.json()[0]["published"])

    def test_unpublish_item(self):
        self.auth(self.owner)
        response = self.client.post(self.unpublish_url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["published"])
        self.item.refresh_from_db()
        self.assertFalse(self.item.published)

    def test_publish_after_unpublish(self):
        self.auth(self.owner)
        self.client.post(self.unpublish_url)
        response = self.client.post(self.publish_url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["published"])

    def test_unpublish_is_idempotent(self):
        self.auth(self.owner)
        self.client.post(self.unpublish_url)
        response = self.client.post(self.unpublish_url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["published"])

    def test_publish_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.post(self.unpublish_url)
        self.assertEqual(response.status_code, 403)

    def test_publish_rejected_after_tender_completed(self):
        self.tender.status = TenderStatus.COMPLETED
        self.tender.save(update_fields=["status"])
        self.auth(self.owner)
        response = self.client.post(self.unpublish_url)
        self.assertEqual(response.status_code, 400, response.content)


class TenderItemCountersAPITests(APITestCase):
    """TASK-85 (raw ТЗ §18-19): offers_count and distributed_venues_count on
    every TenderItem response. Distributed = venues whose directory row now
    points at a product offered on THIS position (docs/adr/0004); the §41
    completion summary is the sum of the same numbers."""

    def setUp(self):
        self.owner = User.objects.create(email="ic-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.cafe1, self.cafe2, self.cafe3 = (self.venue(name) for name in ("Кафе 1", "Кафе 2", "Кафе 3"))
        self.unit = Unit.objects.create(code="l-ic", name="л")
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Счётчики", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.dairy = Supplier.objects.create(legal_name="Dairy LLC", display_name="Dairy")
        self.farm = Supplier.objects.create(legal_name="Farm LLC", display_name="Farm")
        self.items_url = f"/api/v1/catalog/tenders/{self.tender.id}/items"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def venue(self, name):
        venue = Venue.objects.create(display_name=name)
        VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        return venue

    def add_item(self, name, venues):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name=name, unit=self.unit)
        item = TenderItem.objects.create(tender=self.tender, master_product=master_product)
        item.venues.set(venues)
        return item

    def product(self, supplier, name):
        return SupplierProduct.objects.create(
            supplier=supplier, supplier_name=name, normalized_name=name, unit=self.unit
        )

    def add_offer(self, item, supplier, venues, product=None):
        offer = TenderOffer.objects.create(
            tender_item=item,
            supplier=supplier,
            supplier_product=product or self.product(supplier, f"{item.id}-{supplier.id}"),
            price=Decimal("100.00"),
        )
        offer.venues.set(venues)
        return offer

    def distribute(self, offer, venues):
        item = offer.tender_item
        url = f"/api/v1/catalog/tenders/{item.tender_id}/items/{item.id}/offers/{offer.id}/distribution"
        response = self.client.post(url, {"venue_ids": [str(v.id) for v in venues]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

    def counters(self, tender=None):
        response = self.client.get(f"/api/v1/catalog/tenders/{(tender or self.tender).id}/items")
        self.assertEqual(response.status_code, 200, response.content)
        return {
            row["master_product_name"]: (row["offers_count"], row["distributed_venues_count"])
            for row in response.json()
        }

    def distributed_venue_ids(self, offer):
        response = self.client.get(f"{self.items_url}/{offer.tender_item_id}/offers")
        self.assertEqual(response.status_code, 200, response.content)
        return next(row["distributed_venue_ids"] for row in response.json() if row["id"] == str(offer.id))

    def test_item_without_offers(self):
        self.add_item("Мука", [self.cafe1, self.cafe2])
        self.assertEqual(self.counters(), {"Мука": (0, 0)})

    def test_one_offer_distributed_to_one_venue(self):
        cream = self.add_item("Сливки", [self.cafe1, self.cafe2])
        self.distribute(self.add_offer(cream, self.dairy, [self.cafe1, self.cafe2]), [self.cafe1])
        self.assertEqual(self.counters(), {"Сливки": (1, 1)})

    def test_many_offers_and_items(self):
        cream = self.add_item("Сливки", [self.cafe1, self.cafe2, self.cafe3])
        self.add_item("Мука", [self.cafe1])
        everywhere = [self.cafe1, self.cafe2, self.cafe3]
        self.distribute(self.add_offer(cream, self.dairy, everywhere), [self.cafe1, self.cafe2])
        self.distribute(self.add_offer(cream, self.farm, everywhere), [self.cafe3])
        self.assertEqual(self.counters(), {"Сливки": (2, 3), "Мука": (0, 0)})

    def test_product_assigned_outside_this_position_is_not_distributed(self):
        cream = self.add_item("Сливки", [self.cafe1])
        self.add_offer(cream, self.dairy, [self.cafe1])
        VenueProductMatrixItem.objects.create(
            venue=self.cafe1,
            master_product=cream.master_product,
            requested_name="Сливки",
            supplier_product=self.product(self.dairy, "Сливки из прайса"),
        )
        self.assertEqual(self.counters(), {"Сливки": (1, 0)})

    def test_same_product_distributed_by_another_tender_is_not_distributed(self):
        """Stand, tender №24: the supplier offers the product an earlier tender already put into the venue's
        directory. Nothing is distributed here until the buyer distributes this offer — then the earlier one is
        replaced (§77)."""
        cream = self.add_item("Сливки", [self.cafe1])
        earlier = Tender.objects.create(
            created_by=self.owner, name="Раньше", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        earlier_item = TenderItem.objects.create(tender=earlier, master_product=cream.master_product)
        earlier_item.venues.set([self.cafe1])
        product = self.product(self.dairy, "Сливки 33% 1л")
        self.distribute(self.add_offer(earlier_item, self.dairy, [self.cafe1], product=product), [self.cafe1])
        Tender.objects.filter(pk=earlier.pk).update(status=TenderStatus.COMPLETED)

        offer = self.add_offer(cream, self.dairy, [self.cafe1], product=product)
        self.assertEqual(self.counters(), {"Сливки": (1, 0)})
        self.assertEqual(self.distributed_venue_ids(offer), [])

        self.distribute(offer, [self.cafe1])
        self.assertEqual(self.counters(), {"Сливки": (1, 1)})
        self.assertEqual(self.distributed_venue_ids(offer), [str(self.cafe1.id)])
        self.assertEqual(self.counters(earlier), {"Сливки": (1, 0)})

    def test_offer_price_changed_after_distribution_stays_distributed(self):
        """§36: the supplier may change the offer afterwards; the fixed price and the assignment stay."""
        cream = self.add_item("Сливки", [self.cafe1])
        offer = self.add_offer(cream, self.dairy, [self.cafe1])
        self.distribute(offer, [self.cafe1])
        TenderOffer.objects.filter(pk=offer.pk).update(price=Decimal("90.00"))
        self.assertEqual(self.counters(), {"Сливки": (1, 1)})
        self.assertEqual(self.distributed_venue_ids(offer), [str(self.cafe1.id)])

    def test_suppliers_own_price_for_the_venue_ends_the_distribution(self):
        """docs/adr/0004: once the supplier replaces the tender price for the venue with its own, the venue no
        longer buys on the offer's terms."""
        cream = self.add_item("Сливки", [self.cafe1])
        offer = self.add_offer(cream, self.dairy, [self.cafe1])
        self.distribute(offer, [self.cafe1])
        set_supplier_product_venue_price(
            supplier_product=offer.supplier_product,
            venues=[self.cafe1],
            price=Decimal("95.00"),
            created_by=self.owner,
            confirm_replace=True,
        )
        self.assertEqual(self.counters(), {"Сливки": (1, 0)})
        self.assertEqual(self.distributed_venue_ids(offer), [])

    def test_publish_unpublish_and_add_responses_carry_counters(self):
        cream = self.add_item("Сливки", [self.cafe1])
        self.distribute(self.add_offer(cream, self.dairy, [self.cafe1]), [self.cafe1])
        for action in ("unpublish", "publish"):
            with self.subTest(action=action):
                response = self.client.post(f"{self.items_url}/{cream.id}/{action}")
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual((response.json()["offers_count"], response.json()["distributed_venues_count"]), (1, 1))

        flour = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука", unit=self.unit)
        response = self.client.post(
            self.items_url,
            {"items": [{"master_product_id": str(flour.id), "venue_ids": [str(self.cafe2.id)]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((response.json()[0]["offers_count"], response.json()[0]["distributed_venues_count"]), (0, 0))

    def test_query_count_does_not_grow_with_items(self):
        first = self.add_item("Позиция 0", [self.cafe1, self.cafe2])
        self.distribute(self.add_offer(first, self.dairy, [self.cafe1, self.cafe2]), [self.cafe1])
        with CaptureQueriesContext(connection) as one_item:
            self.counters()
        for i in range(1, 4):
            item = self.add_item(f"Позиция {i}", [self.cafe1, self.cafe2])
            self.distribute(self.add_offer(item, self.farm, [self.cafe1, self.cafe2]), [self.cafe2])
        with CaptureQueriesContext(connection) as four_items:
            self.counters()
        self.assertEqual(len(four_items), len(one_item))

    def test_completion_summary_is_the_sum_over_items(self):
        cream = self.add_item("Сливки", [self.cafe1, self.cafe2])
        flour = self.add_item("Мука", [self.cafe3])
        self.distribute(self.add_offer(cream, self.dairy, [self.cafe1, self.cafe2]), [self.cafe1, self.cafe2])
        self.distribute(self.add_offer(flour, self.farm, [self.cafe3]), [self.cafe3])
        response = self.client.get(f"/api/v1/catalog/tenders/{self.tender.id}/complete")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["distributed"], response.json()["undistributed"]), (3, 0))
        self.assertEqual(sum(d for _, d in self.counters().values()), 3)


class TenderFromMasterDirectoryAPITests(APITestCase):
    """TASK-93 (ТЗ «Мастер-справочник — создание тендера» §6-18, docs/adr/0007):
    venue availability per master product, auto-selection that skips venues
    already in another active tender, and no publishing a position without
    venues. Only ACTIVE tenders hold a venue; drafts and completed ones do not."""

    def setUp(self):
        self.owner = User.objects.create(email="md-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(email="md-stranger@example.com", full_name="X", role=UserRole.VENUE_USER)
        self.cafe1, self.cafe2, self.cafe3, self.center = (
            self.venue(name) for name in ("Кафе 1", "Кафе 2", "Кафе 3", "Ресторан Центр")
        )
        self.archived = self.venue("Закрытое кафе", status=EntityStatus.ARCHIVED)
        self.cream = self.master_product("Сливки 33%", [self.cafe1, self.cafe2, self.cafe3, self.archived])
        self.salmon = self.master_product("Лосось", [self.cafe1, self.center])
        self.dessert = self.master_product("Новый десерт", [])
        self.busy = self.tender("Тендер 15", TenderStatus.ACTIVE, {self.cream: [self.cafe2]})
        self.tender("Черновик", TenderStatus.DRAFT, {self.cream: [self.cafe3]})
        self.tender("Прошлый", TenderStatus.COMPLETED, {self.cream: [self.cafe1]})
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def venue(self, name, status=EntityStatus.ACTIVE):
        venue = Venue.objects.create(display_name=name, status=status)
        VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        return venue

    def master_product(self, name, linked_venues):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name=name)
        for venue in linked_venues:
            VenueProductMatrixItem.objects.create(venue=venue, master_product=master_product, requested_name=name)
        return master_product

    def tender(self, name, status, items):
        tender = Tender.objects.create(created_by=self.owner, name=name, access=TenderAccess.OPEN, status=status)
        for master_product, venues in items.items():
            TenderItem.objects.create(tender=tender, master_product=master_product).venues.set(venues)
        return tender

    def availability(self, master_products, owner=None):
        return self.client.post(
            "/api/v1/catalog/tenders/venue-availability",
            {"owner_id": str((owner or self.owner).id), "master_product_ids": [str(mp.id) for mp in master_products]},
            format="json",
        )

    def test_availability_requires_authentication(self):
        self.client.credentials()
        self.assertEqual(self.availability([self.cream]).status_code, 401)

    def test_availability_denied_for_other_account(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.stranger))
        self.assertEqual(self.availability([self.cream]).status_code, 403)

    def test_availability_rejects_foreign_master_product(self):
        foreign = MasterProduct.objects.create(owner=self.stranger, canonical_name="Чужое")
        self.assertEqual(self.availability([foreign]).status_code, 400)

    def test_availability_rejects_empty_selection(self):
        self.assertEqual(self.availability([]).status_code, 400)

    def test_availability_limit_is_200_master_products(self):
        many = MasterProduct.objects.bulk_create(
            MasterProduct(owner=self.owner, canonical_name=f"Товар {i}") for i in range(201)
        )
        self.assertEqual(self.availability(many[:200]).status_code, 200)
        self.assertEqual(self.availability(many).status_code, 400)

    def test_availability_per_master_product(self):
        response = self.availability([self.cream, self.salmon, self.dessert])
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()
        self.assertEqual(
            [row["master_product_id"] for row in rows], [str(self.cream.id), str(self.salmon.id), str(self.dessert.id)]
        )

        def summary(row):
            return [(v["venue_name"], v["linked"], v["busy_tender_name"]) for v in row["venues"]]

        self.assertEqual(
            summary(rows[0]),
            [
                ("Кафе 1", True, None),
                ("Кафе 2", True, "Тендер 15"),
                ("Кафе 3", True, None),
                ("Ресторан Центр", False, None),
            ],
        )
        self.assertEqual(rows[0]["venues"][1]["busy_tender_id"], str(self.busy.id))
        self.assertEqual(
            summary(rows[1]),
            [("Кафе 1", True, None), ("Кафе 2", False, None), ("Кафе 3", False, None), ("Ресторан Центр", True, None)],
        )
        self.assertEqual([v["linked"] for v in rows[2]["venues"]], [False, False, False, False])

    def test_create_with_items_auto_selects_free_linked_venues(self):
        response = self.client.post(
            "/api/v1/catalog/tenders",
            {
                "owner_id": str(self.owner.id),
                "name": "Из мастер-справочника",
                "access": "open",
                "items": [{"master_product_id": str(self.cream.id)}, {"master_product_id": str(self.dessert.id)}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        tender = Tender.objects.get(pk=response.json()["id"])
        venues = {item.master_product_id: set(item.venues.values_list("id", flat=True)) for item in tender.items.all()}
        self.assertEqual(venues, {self.cream.id: {self.cafe1.id, self.cafe3.id}, self.dessert.id: set()})

    def test_create_with_explicit_empty_venues_keeps_position_empty(self):
        response = self.client.post(
            "/api/v1/catalog/tenders",
            {
                "owner_id": str(self.owner.id),
                "name": "Без заведений",
                "access": "open",
                "items": [{"master_product_id": str(self.cream.id), "venue_ids": []}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        item = Tender.objects.get(pk=response.json()["id"]).items.get()
        self.assertEqual(list(item.venues.all()), [])

    def test_add_items_auto_selection_skips_busy_venues(self):
        draft = self.tender("Новый", TenderStatus.DRAFT, {})
        response = self.client.post(
            f"/api/v1/catalog/tenders/{draft.id}/items",
            {"items": [{"master_product_id": str(self.cream.id)}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(set(response.json()[0]["venue_ids"]), {str(self.cafe1.id), str(self.cafe3.id)})

    def test_publish_rejects_position_without_venues(self):
        draft = self.tender("Новый", TenderStatus.DRAFT, {self.salmon: [self.cafe1], self.dessert: []})
        response = self.client.post(f"/api/v1/catalog/tenders/{draft.id}/publish")
        self.assertEqual(response.status_code, 400, response.content)
        dessert_item = draft.items.get(master_product=self.dessert)
        self.assertEqual(
            response.json(),
            {
                "detail": "Выберите хотя бы одно заведение для товара «Новый десерт».",
                "tender_item_ids": [str(dessert_item.id)],
            },
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, TenderStatus.DRAFT)

    def test_publish_lists_every_position_without_venues(self):
        draft = self.tender("Новый", TenderStatus.DRAFT, {self.salmon: [], self.dessert: []})
        response = self.client.post(f"/api/v1/catalog/tenders/{draft.id}/publish")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()["detail"], "Выберите хотя бы одно заведение для товаров «Лосось», «Новый десерт»."
        )
        self.assertEqual(len(response.json()["tender_item_ids"]), 2)

    def test_unpublished_position_without_venues_does_not_block(self):
        draft = self.tender("Новый", TenderStatus.DRAFT, {self.salmon: [self.cafe1], self.dessert: []})
        draft.items.filter(master_product=self.dessert).update(published=False)
        response = self.client.post(f"/api/v1/catalog/tenders/{draft.id}/publish")
        self.assertEqual(response.status_code, 200, response.content)


class TenderPublishAPITests(APITestCase):
    """Raw ТЗ §15: publish flips draft -> active, and is the only way a
    tender ever becomes visible to suppliers (§14). Requiring at least one
    item before publish isn't spelled out verbatim in the raw ТЗ, but
    publishing an empty tender has no useful supplier-facing meaning —
    documented here as a deliberate judgment call, easy to relax later if
    it turns out to be wrong."""

    def setUp(self):
        self.owner = User.objects.create(email="pub-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(email="pub-stranger@example.com", full_name="X", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Pub")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="kg", name="кг")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Камамбер", unit=unit)
        self.tender = Tender.objects.create(created_by=self.owner, name="Сыры", access=TenderAccess.OPEN)
        self.url = f"/api/v1/catalog/tenders/{self.tender.id}/publish"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_publish_rejects_empty_tender(self):
        self.auth(self.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 400)

    def test_publish_with_items_succeeds(self):
        item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        item.venues.set([self.venue])
        self.auth(self.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "active")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, TenderStatus.ACTIVE)

    def test_publish_twice_rejected(self):
        item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        item.venues.set([self.venue])
        self.tender.status = TenderStatus.ACTIVE
        self.tender.save(update_fields=["status"])
        self.auth(self.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 400)

    def test_publish_denied_for_stranger(self):
        item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        item.venues.set([self.venue])
        self.auth(self.stranger)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 403)


class TenderActiveConflictAPITests(APITestCase):
    """Raw ТЗ §12: Мастер-товар + Заведение can't be active in two tenders
    at once — "Связка ... не может одновременно участвовать в двух активных
    тендерах". Checked server-side at the two points a TenderItem can
    become active: publish (draft -> active) and adding an item directly
    to a tender that's already active."""

    def setUp(self):
        self.owner = User.objects.create(email="conf-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe 1")
        self.other_venue = Venue.objects.create(display_name="Cafe 2")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.other_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="l", name="литр")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%", unit=unit)
        self.other_tender = Tender.objects.create(
            created_by=self.owner, name="Тендер №15", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        other_item = TenderItem.objects.create(tender=self.other_tender, master_product=self.master_product)
        other_item.venues.set([self.venue])

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_publish_blocked_by_conflicting_active_tender(self):
        tender = Tender.objects.create(created_by=self.owner, name="Дубль", access=TenderAccess.OPEN)
        item = TenderItem.objects.create(tender=tender, master_product=self.master_product)
        item.venues.set([self.venue])
        self.auth(self.owner)
        response = self.client.post(f"/api/v1/catalog/tenders/{tender.id}/publish")
        self.assertEqual(response.status_code, 409, response.content)
        data = response.json()
        self.assertEqual(len(data["conflicts"]), 1)
        self.assertEqual(data["conflicts"][0]["tender_id"], str(self.other_tender.id))
        self.assertEqual(data["conflicts"][0]["venue_id"], str(self.venue.id))
        tender.refresh_from_db()
        self.assertEqual(tender.status, TenderStatus.DRAFT)

    def test_publish_succeeds_when_venues_dont_overlap(self):
        tender = Tender.objects.create(created_by=self.owner, name="Другое заведение", access=TenderAccess.OPEN)
        item = TenderItem.objects.create(tender=tender, master_product=self.master_product)
        item.venues.set([self.other_venue])
        self.auth(self.owner)
        response = self.client.post(f"/api/v1/catalog/tenders/{tender.id}/publish")
        self.assertEqual(response.status_code, 200, response.content)

    def test_add_item_to_active_tender_blocked_by_conflict(self):
        tender = Tender.objects.create(
            created_by=self.owner, name="Уже активен", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.auth(self.owner)
        response = self.client.post(
            f"/api/v1/catalog/tenders/{tender.id}/items",
            {"items": [{"master_product_id": str(self.master_product.id), "venue_ids": [str(self.venue.id)]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(TenderItem.objects.filter(tender=tender, master_product=self.master_product).exists())

    def test_add_item_to_draft_tender_unaffected_by_conflict(self):
        tender = Tender.objects.create(created_by=self.owner, name="Ещё черновик", access=TenderAccess.OPEN)
        self.auth(self.owner)
        response = self.client.post(
            f"/api/v1/catalog/tenders/{tender.id}/items",
            {"items": [{"master_product_id": str(self.master_product.id), "venue_ids": [str(self.venue.id)]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)


class SupplierTenderOfferAPITests(APITestCase):
    """Raw ТЗ §35/§55-57: supplier submits/updates their one current offer
    per position. Auth mirrors SupplierOrderDetailView (OptionalCoreUserJWT
    + CanInitiateAccountTransfer, supplier_id in the URL)."""

    def setUp(self):
        self.owner = User.objects.create(email="off-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Offer")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.other_venue = Venue.objects.create(display_name="Not In Position")
        unit = Unit.objects.create(code="kg", name="кг")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Лосось", unit=unit)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Рыба", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])

        self.supplier = Supplier.objects.create(legal_name="Fish LLC", display_name="Fish Co")
        self.supplier_user = User.objects.create(email="fish@example.com", full_name="Fish", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Лосось охл.", normalized_name="лосось охл", unit=unit
        )
        self.other_supplier = Supplier.objects.create(legal_name="Other LLC", display_name="Other Co")
        self.other_supplier_product = SupplierProduct.objects.create(
            supplier=self.other_supplier, supplier_name="Чужой товар", normalized_name="чужой товар"
        )

        self.url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/items/{self.item.id}/offer"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_submit_offer(self):
        self.auth(self.supplier_user)
        response = self.client.put(
            self.url,
            {
                "supplier_product_id": str(self.supplier_product.id),
                "price": "320.00",
                "venue_ids": [str(self.venue.id)],
                "comment": "Свежий улов",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["price"], "320.00")
        self.assertEqual(data["venue_ids"], [str(self.venue.id)])
        self.assertEqual(TenderOffer.objects.filter(tender_item=self.item, supplier=self.supplier).count(), 1)

    def test_resubmit_updates_same_offer_not_creates_new(self):
        self.auth(self.supplier_user)
        self.client.put(
            self.url,
            {
                "supplier_product_id": str(self.supplier_product.id),
                "price": "320.00",
                "venue_ids": [str(self.venue.id)],
            },
            format="json",
        )
        response = self.client.put(
            self.url,
            {
                "supplier_product_id": str(self.supplier_product.id),
                "price": "340.00",
                "venue_ids": [str(self.venue.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["price"], "340.00")
        self.assertEqual(TenderOffer.objects.filter(tender_item=self.item, supplier=self.supplier).count(), 1)

    def test_rejects_venue_outside_position(self):
        self.auth(self.supplier_user)
        response = self.client.put(
            self.url,
            {
                "supplier_product_id": str(self.supplier_product.id),
                "price": "320.00",
                "venue_ids": [str(self.other_venue.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("venue_ids", response.json())

    def test_rejects_other_suppliers_product(self):
        self.auth(self.supplier_user)
        response = self.client.put(
            self.url,
            {
                "supplier_product_id": str(self.other_supplier_product.id),
                "price": "320.00",
                "venue_ids": [str(self.venue.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("supplier_product_id", response.json())

    def test_rejects_non_member(self):
        stranger = User.objects.create(email="stranger-fish@example.com", full_name="X", role=UserRole.SUPPLIER)
        self.auth(stranger)
        response = self.client.put(
            self.url,
            {
                "supplier_product_id": str(self.supplier_product.id),
                "price": "320.00",
                "venue_ids": [str(self.venue.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_rejects_draft_tender(self):
        self.tender.status = TenderStatus.DRAFT
        self.tender.save(update_fields=["status"])
        self.auth(self.supplier_user)
        response = self.client.put(
            self.url,
            {
                "supplier_product_id": str(self.supplier_product.id),
                "price": "320.00",
                "venue_ids": [str(self.venue.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class SupplierTenderItemListAPITests(APITestCase):
    """Raw ТЗ §52-54: supplier sees every position of an eligible tender on
    one page, each row showing their own current offer if they have one."""

    def setUp(self):
        self.owner = User.objects.create(email="sl-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe List")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="kg", name="кг")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Треска", unit=unit)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Рыба2", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])

        self.supplier = Supplier.objects.create(legal_name="Fish2 LLC", display_name="Fish2 Co")
        self.supplier_user = User.objects.create(email="fish2@example.com", full_name="Fish2", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Треска мор.", normalized_name="треска мор", unit=unit
        )

        self.closed_tender = Tender.objects.create(
            created_by=self.owner, name="Закрытый", access=TenderAccess.INVITE_ONLY, status=TenderStatus.ACTIVE
        )
        TenderItem.objects.create(tender=self.closed_tender, master_product=self.master_product).venues.set(
            [self.venue]
        )

        self.url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/items"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_list_without_offer(self):
        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertIsNone(data[0]["my_offer"])
        self.assertEqual(data[0]["master_product_name"], "Треска")

    def test_unpublished_item_hidden_from_supplier(self):
        """TASK-59 (raw ТЗ §18/§75): снятие с публикации скрывает позицию
        от поставщика — единственный видимый эффект флага published."""
        hake = MasterProduct.objects.create(owner=self.owner, canonical_name="Хек")
        visible = TenderItem.objects.create(tender=self.tender, master_product=hake)
        visible.venues.set([self.venue])
        self.item.published = False
        self.item.save(update_fields=["published"])
        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row["id"] for row in response.json()], [str(visible.id)])

    def test_list_with_offer(self):
        offer = TenderOffer.objects.create(
            tender_item=self.item, supplier=self.supplier, supplier_product=self.supplier_product, price="150.00"
        )
        offer.venues.set([self.venue])
        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        data = response.json()
        self.assertIsNotNone(data[0]["my_offer"])
        self.assertEqual(data[0]["my_offer"]["price"], "150.00")

    def test_denied_for_closed_tender_not_invited(self):
        url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.closed_tender.id}/items"
        self.auth(self.supplier_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_invited_supplier_can_access_invite_only_tender_but_others_cannot(self):
        """Final-review Finding 3b: the invite_only branch of
        supplier_can_access_tender (selectors.py) had zero test coverage
        anywhere in this plan — every other test only exercises
        TenderAccess.OPEN. Confirms an invited supplier gets 200, and a
        different, non-invited supplier still gets 404 for the same
        invite_only tender."""
        other_supplier = Supplier.objects.create(legal_name="Other Fish LLC", display_name="Other Fish Co")
        other_supplier_user = User.objects.create(
            email="other-fish-sl@example.com", full_name="OtherFish", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=other_supplier,
            user=other_supplier_user,
            role=SupplierUserRole.MANAGER,
            status=EntityStatus.ACTIVE,
        )
        self.closed_tender.invited_suppliers.add(self.supplier)

        invited_url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.closed_tender.id}/items"
        self.auth(self.supplier_user)
        response = self.client.get(invited_url)
        self.assertEqual(response.status_code, 200, response.content)

        not_invited_url = f"/api/v1/catalog/suppliers/{other_supplier.id}/tenders/{self.closed_tender.id}/items"
        self.auth(other_supplier_user)
        response = self.client.get(not_invited_url)
        self.assertEqual(response.status_code, 404)

    def test_denied_for_draft_tender(self):
        self.tender.status = TenderStatus.DRAFT
        self.tender.save(update_fields=["status"])
        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)


class SupplierTenderDetailAPITests(APITestCase):
    """TASK-84 (raw ТЗ §53): the tender header on the supplier's response
    page, opened by direct link. Same access rules as .../items (ADR-0001);
    counters and customer venues cover published positions only, and
    nothing about other suppliers leaks (§51)."""

    def setUp(self):
        self.owner = User.objects.create(email="sh-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.unit = Unit.objects.create(code="kg-sh", name="кг")
        self.supplier = Supplier.objects.create(legal_name="Head LLC", display_name="Head Co")
        self.supplier_user = User.objects.create(email="head@example.com", full_name="Head", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.rival = Supplier.objects.create(legal_name="Rival LLC", display_name="Rival Co")
        self.tender = Tender.objects.create(
            created_by=self.owner,
            name="Осеннее меню",
            description="Доставка до 10:00",
            access=TenderAccess.OPEN,
            status=TenderStatus.ACTIVE,
        )
        self.url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def venue(self, name):
        venue = Venue.objects.create(display_name=name)
        VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        return venue

    def add_item(self, name, venues, published=True):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name=name, unit=self.unit)
        item = TenderItem.objects.create(tender=self.tender, master_product=master_product, published=published)
        item.venues.set(venues)
        return item

    def add_offer(self, item, supplier):
        product = SupplierProduct.objects.create(
            supplier=supplier, supplier_name=item.master_product.canonical_name, normalized_name="x", unit=self.unit
        )
        return TenderOffer.objects.create(
            tender_item=item, supplier=supplier, supplier_product=product, price=Decimal("10.00")
        )

    def get_header(self):
        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_requires_authentication(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_user_outside_supplier_gets_403(self):
        stranger = User.objects.create(email="sh-stranger@example.com", full_name="S", role=UserRole.SUPPLIER)
        self.auth(stranger)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_draft_tender_is_404(self):
        self.tender.status = TenderStatus.DRAFT
        self.tender.save()
        self.auth(self.supplier_user)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_invite_only_tender_visible_only_to_invited(self):
        self.add_item("Сливки", [self.venue("Кафе 1")])
        self.tender.access = TenderAccess.INVITE_ONLY
        self.tender.save()
        self.auth(self.supplier_user)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.tender.invited_suppliers.add(self.supplier)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_header_fields_and_nothing_about_other_suppliers(self):
        self.add_item("Сливки", [self.venue("Кафе 1")])
        self.tender.access = TenderAccess.INVITE_ONLY
        self.tender.save()
        self.tender.invited_suppliers.add(self.supplier, self.rival)
        data = self.get_header()
        self.assertEqual(
            set(data),
            {
                "id",
                "number",
                "name",
                "description",
                "accepts_offers_until",
                "access",
                "status",
                "customer_venues",
                "published_items_count",
                "my_offers_count",
                "created_at",
                "published_at",
                "files",
                "companies",
            },
        )
        self.assertIsNone(data["companies"])
        self.assertEqual(data["id"], str(self.tender.id))
        self.assertEqual(data["name"], "Осеннее меню")
        self.assertEqual(data["description"], "Доставка до 10:00")
        self.assertIsNone(data["accepts_offers_until"])
        self.assertEqual((data["access"], data["status"]), ("invite_only", "active"))

    def test_expired_tender_reads_as_completed(self):
        self.add_item("Сливки", [self.venue("Кафе 1")])
        self.tender.accepts_offers_until = timezone.now() - timedelta(minutes=1)
        self.tender.save()
        data = self.get_header()
        self.assertEqual(data["status"], "completed")

    def test_nothing_published_is_404(self):
        hidden = self.add_item("Снятая", [self.venue("Кафе 1")], published=False)
        self.add_offer(hidden, self.supplier)
        self.auth(self.supplier_user)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_counters_one_item_with_my_offer(self):
        cafe = self.venue("Кафе 1")
        self.add_offer(self.add_item("Сливки", [cafe]), self.supplier)
        data = self.get_header()
        self.assertEqual((data["published_items_count"], data["my_offers_count"]), (1, 1))
        self.assertEqual(data["customer_venues"], [{"id": str(cafe.id), "display_name": "Кафе 1"}])

    def test_counters_and_venues_many_items(self):
        bakery, cafe, center, hidden_venue = (
            self.venue("Пекарня"),
            self.venue("Кафе 1"),
            self.venue("Ресторан Центр"),
            self.venue("Скрытое"),
        )
        cream = self.add_item("Сливки", [cafe, bakery])
        cheese = self.add_item("Камамбер", [bakery])
        flour = self.add_item("Мука", [center])
        hidden = self.add_item("Снятая", [hidden_venue], published=False)
        self.add_offer(cream, self.supplier)
        self.add_offer(cheese, self.supplier)
        self.add_offer(hidden, self.supplier)
        self.add_offer(flour, self.rival)

        data = self.get_header()
        self.assertEqual((data["published_items_count"], data["my_offers_count"]), (3, 2))
        self.assertEqual([v["display_name"] for v in data["customer_venues"]], ["Кафе 1", "Пекарня", "Ресторан Центр"])


class SupplierTenderListAPITests(APITestCase):
    """TASK-60 (raw ТЗ §44-51): tenders available to a supplier — open ones
    plus invite_only ones this supplier is invited to; never drafts. Tabs
    filter by the effective status, so a tender past its deadline is already
    "completed" before anyone opens it. Rows are the same SupplierTender as
    the response-page header."""

    def setUp(self):
        self.owner = User.objects.create(email="stl-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.unit = Unit.objects.create(code="kg-stl", name="кг")
        self.supplier = Supplier.objects.create(legal_name="List LLC", display_name="List Co")
        self.supplier_user = User.objects.create(email="stl@example.com", full_name="Stl", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders"
        self.minutes = 0

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def tender(self, name, status=TenderStatus.ACTIVE, access=TenderAccess.OPEN, deadline=None, with_item=True):
        tender = Tender.objects.create(
            created_by=self.owner, name=name, status=status, access=access, accepts_offers_until=deadline
        )
        if with_item:
            self.add_item(tender, f"Позиция {name}", f"Заведение {name}")
        self.minutes += 1
        Tender.objects.filter(pk=tender.pk).update(
            created_at=dt(2026, 9, 1, tzinfo=dt_timezone.utc) + timedelta(minutes=self.minutes)
        )
        return tender

    def add_item(self, tender, name, venue_name, published=True):
        venue = Venue.objects.create(display_name=venue_name)
        VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name=name, unit=self.unit)
        item = TenderItem.objects.create(tender=tender, master_product=master_product, published=published)
        item.venues.set([venue])
        return item

    def names(self, **params):
        self.auth(self.supplier_user)
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["name"] for row in response.json()["results"]]

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_user_outside_supplier_gets_403(self):
        stranger = User.objects.create(email="stl-stranger@example.com", full_name="S", role=UserRole.SUPPLIER)
        self.auth(stranger)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_active_tab_is_default_newest_first_without_drafts(self):
        self.tender("Старый")
        self.tender("Новый")
        self.tender("Черновик", status=TenderStatus.DRAFT)
        self.tender("Закрыт", status=TenderStatus.COMPLETED)
        self.assertEqual(self.names(), ["Новый", "Старый"])

    def test_completed_tab(self):
        self.tender("Идёт")
        self.tender("Закрыт", status=TenderStatus.COMPLETED)
        self.assertEqual(self.names(status="completed"), ["Закрыт"])

    def test_unknown_status_is_400(self):
        self.auth(self.supplier_user)
        self.assertEqual(self.client.get(self.url, {"status": "draft"}).status_code, 400)

    def test_invite_only_listed_only_for_invited(self):
        closed = self.tender("Закрытый", access=TenderAccess.INVITE_ONLY)
        rival = Supplier.objects.create(legal_name="Rival LLC", display_name="Rival")
        closed.invited_suppliers.add(rival)
        self.assertEqual(self.names(), [])
        closed.invited_suppliers.add(self.supplier)
        self.assertEqual(self.names(), ["Закрытый"])

    def test_deadline_boundary_splits_tabs(self):
        now = timezone.now()
        self.tender("Минутой раньше", deadline=now - timedelta(minutes=1))
        self.tender("Ровно сейчас", deadline=now)
        self.tender("Минутой позже", deadline=now + timedelta(minutes=1))
        with patch("django.utils.timezone.now", return_value=now):
            active = self.names()
            completed_response = self.client.get(self.url, {"status": "completed"})
        self.assertEqual(active, ["Минутой позже"])
        rows = completed_response.json()["results"]
        self.assertEqual(
            [(r["name"], r["status"]) for r in rows], [("Ровно сейчас", "completed"), ("Минутой раньше", "completed")]
        )

    def test_search_by_tender_customer_venue_and_visible_product(self):
        cream = self.tender("Молочка", with_item=False)
        self.add_item(cream, "Сливки 33%", "Кафе Центр")
        bread = self.tender("Выпечка", with_item=False)
        self.add_item(bread, "Мука", "Пекарня")
        self.add_item(bread, "Дрожжи", "Склад", published=False)
        cases = {"молоч": ["Молочка"], "центр": ["Молочка"], "СЛИВКИ": ["Молочка"], "дрожжи": [], "пекар": ["Выпечка"]}
        for q, expected in cases.items():
            with self.subTest(q=q):
                self.assertEqual(self.names(q=q), expected)

    def test_search_ignores_case_whatever_the_database_locale(self):
        """TASK-92: the stand's Postgres folds only ASCII in UPPER()/ILIKE
        (LC_CTYPE=C), so icontains missed «проверка» in «Проверка»."""
        self.tender("Проверка связи")
        self.tender("Ёлочные игрушки")
        self.tender("Lira Market")
        cases = {
            "проверка": ["Проверка связи"],
            "ПРОВЕРКА": ["Проверка связи"],
            "ёлоч": ["Ёлочные игрушки"],
            "lira": ["Lira Market"],
        }
        for q, expected in cases.items():
            with self.subTest(q=q):
                self.assertEqual(self.names(q=q), expected)

    def test_rows_carry_header_fields(self):
        tender = self.tender("Молочка", with_item=False)
        cream = self.add_item(tender, "Сливки", "Кафе 1")
        self.add_item(tender, "Масло", "Кафе 2")
        product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сливки", normalized_name="сливки", unit=self.unit
        )
        TenderOffer.objects.create(
            tender_item=cream, supplier=self.supplier, supplier_product=product, price=Decimal("1.00")
        )
        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        self.assertEqual(set(response.json()), {"count", "next", "previous", "results"})
        row = response.json()["results"][0]
        self.assertEqual((row["published_items_count"], row["my_offers_count"]), (2, 1))
        self.assertEqual([v["display_name"] for v in row["customer_venues"]], ["Кафе 1", "Кафе 2"])
        self.assertNotIn("invited_supplier_ids", row)
        self.assertEqual(dt.fromisoformat(row["created_at"]), dt(2026, 9, 1, 0, 1, tzinfo=dt_timezone.utc))

    def test_query_count_does_not_grow_with_rows(self):
        self.add_item(self.tender("Тендер 0", access=TenderAccess.INVITE_ONLY, with_item=False), "Товар 0", "Кафе 0")
        Tender.objects.get(name="Тендер 0").invited_suppliers.add(self.supplier)
        self.auth(self.supplier_user)
        with CaptureQueriesContext(connection) as one_row:
            self.client.get(self.url)
        for i in range(1, 4):
            self.add_item(self.tender(f"Тендер {i}", with_item=False), f"Товар {i}", f"Кафе {i}")
        with CaptureQueriesContext(connection) as four_rows:
            response = self.client.get(self.url)
        self.assertEqual(response.json()["count"], 4)
        self.assertEqual(len(four_rows), len(one_row))


class SupplierTenderOfferAutofillAPITests(APITestCase):
    """TASK-67 (raw ТЗ §62-64, минимальный v1-срез, согласован 2026-09-17 —
    см. backlog#task-67): подсказка для кнопки "Заполнить автоматически".
    Matching — только через существующие VenueProductMatrixItem-связи
    (никакого fuzzy-поиска по названию, это отдельный будущий гэп).
    Заведения — всегда полный список позиции (Павел подтвердил в Telegram
    2026-09-17: система выбирает все, поставщик убирает лишние руками)."""

    def setUp(self):
        self.owner = User.objects.create(email="af-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Autofill 1")
        self.venue2 = Venue.objects.create(display_name="Cafe Autofill 2")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.venue2, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="kg-af", name="кг")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука", unit=unit)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Мучной", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue, self.venue2])

        self.supplier = Supplier.objects.create(legal_name="Flour LLC", display_name="Flour Co")
        self.supplier_user = User.objects.create(email="flour@example.com", full_name="Flour", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

        self.url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/offer-autofill"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_direct_link_suggests_product_min_price_and_all_venues(self):
        supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Мука в/с", normalized_name="мука в/с", current_price="100.00"
        )
        VenueProductMatrixItem.objects.create(
            venue=self.venue,
            requested_name="Мука",
            master_product=self.master_product,
            supplier_product=supplier_product,
        )
        VenueProductMatrixItem.objects.create(
            venue=self.venue2,
            requested_name="Мука",
            master_product=self.master_product,
            supplier_product=supplier_product,
        )
        SupplierProductVenuePrice.objects.create(supplier_product=supplier_product, venue=self.venue2, price="80.00")

        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(len(data), 1)
        row = data[0]
        self.assertEqual(row["match_status"], "found")
        self.assertEqual(row["supplier_product_id"], str(supplier_product.id))
        self.assertEqual(row["price"], "80.00")
        self.assertCountEqual(row["venue_ids"], [str(self.venue.id), str(self.venue2.id)])

    def test_multiple_linked_products_picks_most_frequent(self):
        frequent = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Мука популярная",
            normalized_name="мука популярная",
            current_price="90.00",
        )
        rare = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Мука редкая", normalized_name="мука редкая", current_price="70.00"
        )
        third_venue = Venue.objects.create(display_name="Cafe Autofill 3")
        self.item.venues.add(third_venue)
        VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Мука", master_product=self.master_product, supplier_product=frequent
        )
        VenueProductMatrixItem.objects.create(
            venue=third_venue, requested_name="Мука", master_product=self.master_product, supplier_product=frequent
        )
        VenueProductMatrixItem.objects.create(
            venue=self.venue2, requested_name="Мука", master_product=self.master_product, supplier_product=rare
        )

        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()[0]["supplier_product_id"], str(frequent.id))

    def test_no_link_is_not_found(self):
        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()[0]
        self.assertEqual(row["match_status"], "not_found")
        self.assertIsNone(row["supplier_product_id"])
        self.assertIsNone(row["price"])
        self.assertEqual(row["venue_ids"], [])

    def test_item_without_venues_is_not_found(self):
        self.item.venues.clear()
        supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Мука вс2", normalized_name="мука вс2", current_price="100.00"
        )
        VenueProductMatrixItem.objects.create(
            venue=self.venue,
            requested_name="Мука",
            master_product=self.master_product,
            supplier_product=supplier_product,
        )
        self.auth(self.supplier_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()[0]["match_status"], "not_found")

    def test_denied_for_supplier_without_tender_access(self):
        closed_tender = Tender.objects.create(
            created_by=self.owner, name="Закрытый", access=TenderAccess.INVITE_ONLY, status=TenderStatus.ACTIVE
        )
        url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{closed_tender.id}/offer-autofill"
        self.auth(self.supplier_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


class TenderOfferAutofillQueriesAPITests(APITestCase):
    """BE-61: offer-autofill used to spend a product lookup, a product fetch
    and a price query per venue on every position (~150 queries for 25
    positions × 4 venues). The suggestion itself is covered by
    SupplierTenderOfferAutofillAPITests; this pins the query count."""

    def setUp(self):
        self.owner = User.objects.create(email="aq-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venues = [Venue.objects.create(display_name=f"Кафе {i}") for i in range(3)]
        for venue in self.venues:
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Большой", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.supplier = Supplier.objects.create(legal_name="Big LLC", display_name="Big")
        self.supplier_user = User.objects.create(email="aq-big@example.com", full_name="Big", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/offer-autofill"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.supplier_user))

    def add_linked_item(self, name, venues):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name=name)
        TenderItem.objects.create(tender=self.tender, master_product=master_product).venues.set(venues)
        product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name=name, normalized_name=name, current_price="100.00"
        )
        for venue in venues:
            VenueProductMatrixItem.objects.create(
                venue=venue, master_product=master_product, supplier_product=product, requested_name=name
            )
        SupplierProductVenuePrice.objects.create(supplier_product=product, venue=venues[0], price="90.00")

    def test_query_count_does_not_grow_with_positions_and_venues(self):
        self.add_linked_item("Позиция 0", self.venues[:1])
        with CaptureQueriesContext(connection) as one:
            first = self.client.get(self.url)
        for i in range(1, 5):
            self.add_linked_item(f"Позиция {i}", self.venues)
        with CaptureQueriesContext(connection) as five:
            response = self.client.get(self.url)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual([row["price"] for row in response.json()], ["90.00"] * 5)
        self.assertEqual(len(five), len(one))


class TenderOfferRawNameAPITests(APITestCase):
    """TASK-89: packagings of one product share a normalized name ("pomi
    мякоть помидора" is both 0.5 kg and 1 kg), while a tender price is per
    packaging — every response carrying an offered product also returns its
    price-list name as supplier_product_raw_name."""

    HALF_KILO = "POMI Мякоть помидора 0,5 кг 1/16, шт"
    ONE_KILO = "POMI Мякоть помидора 1 кг 1/12, шт"

    def setUp(self):
        self.owner = User.objects.create(email="rn-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Raw")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Мякоть помидора")
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Томаты", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])
        self.supplier, self.supplier_user = self.supplier_with_user("rn-a")
        self.half_kilo = self.product(self.supplier, self.HALF_KILO)

    def supplier_with_user(self, code):
        supplier = Supplier.objects.create(legal_name=f"{code} LLC", display_name=code)
        user = User.objects.create(email=f"{code}@example.com", full_name=code, role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=supplier, user=user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        return supplier, user

    def product(self, supplier, raw_name):
        return SupplierProduct.objects.create(
            supplier=supplier, supplier_name=raw_name, normalized_name="pomi мякоть помидора", current_price="150.00"
        )

    def submit(self, supplier, user, product):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.put(
            f"/api/v1/catalog/suppliers/{supplier.id}/tenders/{self.tender.id}/items/{self.item.id}/offer",
            {"supplier_product_id": str(product.id), "price": "140.00", "venue_ids": [str(self.venue.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def buyer_offers(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get(f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/offers")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_submit_response_carries_price_list_name(self):
        data = self.submit(self.supplier, self.supplier_user, self.half_kilo)
        self.assertEqual(data["supplier_product_raw_name"], self.HALF_KILO)
        self.assertEqual(data["supplier_product_name"], "pomi мякоть помидора")

    def test_buyer_tells_packagings_apart(self):
        other, other_user = self.supplier_with_user("rn-b")
        self.submit(self.supplier, self.supplier_user, self.half_kilo)
        self.submit(other, other_user, self.product(other, self.ONE_KILO))
        names = {row["supplier_id"]: row["supplier_product_raw_name"] for row in self.buyer_offers()}
        self.assertEqual(names, {str(self.supplier.id): self.HALF_KILO, str(other.id): self.ONE_KILO})

    def test_supplier_sees_own_offer_name_after_reload(self):
        self.submit(self.supplier, self.supplier_user, self.half_kilo)
        response = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/items")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()[0]["my_offer"]["supplier_product_raw_name"], self.HALF_KILO)

    def test_deleted_product_gives_null(self):
        self.submit(self.supplier, self.supplier_user, self.half_kilo)
        TenderOffer.objects.filter(tender_item=self.item).update(supplier_product=None)
        row = self.buyer_offers()[0]
        self.assertIsNone(row["supplier_product_raw_name"])

    def test_autofill_suggestion_carries_price_list_name(self):
        url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/offer-autofill"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.supplier_user))
        self.assertIsNone(self.client.get(url).json()[0]["supplier_product_raw_name"])

        VenueProductMatrixItem.objects.create(
            venue=self.venue,
            requested_name="Томаты",
            master_product=self.master_product,
            supplier_product=self.half_kilo,
        )
        row = self.client.get(url).json()[0]
        self.assertEqual((row["match_status"], row["supplier_product_raw_name"]), ("found", self.HALF_KILO))


class TenderOfferWithdrawAPITests(APITestCase):
    """TASK-90 (raw ТЗ §67, docs/adr/0006): DELETE .../offer withdraws a sent
    offer without deleting it — the supplier still sees it (muted, values kept),
    the buyer no longer does, counters drop it, a later PUT revives the same
    row (§69). A venue already given the offer's product stays distributed."""

    def setUp(self):
        self.owner = User.objects.create(email="wd-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Withdraw")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Молочка", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])
        self.supplier = Supplier.objects.create(legal_name="Cream LLC", display_name="Cream")
        self.supplier_user = User.objects.create(
            email="wd-cream@example.com", full_name="Cream", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сливки 33% 1 л", normalized_name="сливки 33"
        )
        self.offer_url = (
            f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/items/{self.item.id}/offer"
        )
        self.as_supplier()

    def as_supplier(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.supplier_user))

    def as_owner(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def submit(self, price="100.00"):
        self.as_supplier()
        response = self.client.put(
            self.offer_url,
            {"supplier_product_id": str(self.product.id), "price": price, "venue_ids": [str(self.venue.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def withdraw(self):
        self.as_supplier()
        return self.client.delete(self.offer_url)

    def buyer_view(self):
        self.as_owner()
        base = f"/api/v1/catalog/tenders/{self.tender.id}"
        return {
            "offers": self.client.get(f"{base}/items/{self.item.id}/offers").json(),
            "tender_offers_count": self.client.get(base).json()["offers_count"],
            "item": self.client.get(f"{base}/items").json()[0],
            "summary_offers_count": self.client.get(f"{base}/complete").json()["offers_count"],
        }

    def supplier_view(self):
        self.as_supplier()
        base = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}"
        return {
            "my_offer": self.client.get(f"{base}/items").json()[0]["my_offer"],
            "header": self.client.get(base).json(),
        }

    def test_requires_authentication(self):
        self.client.credentials()
        self.assertEqual(self.client.delete(self.offer_url).status_code, 401)

    def test_user_outside_supplier_gets_403(self):
        self.submit()
        stranger = User.objects.create(email="wd-stranger@example.com", full_name="S", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(stranger))
        self.assertEqual(self.client.delete(self.offer_url).status_code, 403)

    def test_nothing_sent_is_404(self):
        self.assertEqual(self.withdraw().status_code, 404)

    def test_withdrawn_offer_leaves_buyer_view_and_counters(self):
        self.submit()
        response = self.withdraw()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNotNone(response.json()["withdrawn_at"])

        buyer = self.buyer_view()
        self.assertEqual(buyer["offers"], [])
        self.assertEqual(
            (buyer["tender_offers_count"], buyer["item"]["offers_count"], buyer["summary_offers_count"]), (0, 0, 0)
        )

        supplier = self.supplier_view()
        self.assertIsNotNone(supplier["my_offer"]["withdrawn_at"])
        self.assertEqual(supplier["my_offer"]["price"], "100.00")
        self.assertEqual(supplier["header"]["my_offers_count"], 0)
        self.assertTrue(TenderOffer.objects.filter(tender_item=self.item, supplier=self.supplier).exists())

    def test_repeat_withdraw_keeps_first_moment(self):
        self.submit()
        first = self.withdraw().json()["withdrawn_at"]
        second = self.withdraw()
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()["withdrawn_at"], first)

    def test_resubmit_revives_the_same_offer(self):
        offer_id = self.submit()["id"]
        self.withdraw()
        revived = self.submit(price="95.00")
        self.assertEqual((revived["id"], revived["withdrawn_at"], revived["price"]), (offer_id, None, "95.00"))
        self.assertEqual(len(self.buyer_view()["offers"]), 1)
        self.assertEqual(self.supplier_view()["header"]["my_offers_count"], 1)

    def test_completed_tender_rejects_withdraw(self):
        self.submit()
        self.tender.status = TenderStatus.COMPLETED
        self.tender.save()
        self.assertEqual(self.withdraw().status_code, 400)

    def test_withdrawn_offer_cannot_be_distributed(self):
        offer_id = self.submit()["id"]
        self.withdraw()
        self.as_owner()
        url = f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/offers/{offer_id}/distribution"
        self.assertEqual(self.client.get(url, {"venue_id": str(self.venue.id)}).status_code, 400)
        self.assertEqual(self.client.post(url, {"venue_ids": [str(self.venue.id)]}, format="json").status_code, 400)

    def test_distributed_venue_stays_distributed_after_withdraw(self):
        offer_id = self.submit()["id"]
        self.as_owner()
        url = f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/offers/{offer_id}/distribution"
        response = self.client.post(url, {"venue_ids": [str(self.venue.id)]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.withdraw()
        item = self.buyer_view()["item"]
        self.assertEqual((item["offers_count"], item["distributed_venues_count"]), (0, 1))


class TenderOfferTableAPITests(APITestCase):
    """TASK-91 (raw ТЗ §21-25): the buyer's offers table needs the supplier's
    name, the offered product's terms (net, multiple, minimum) and where this
    offer is distributed now (§23) — one TenderOffer shape in every response."""

    def setUp(self):
        self.owner = User.objects.create(email="ot-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.cafe1, self.cafe2 = (self.venue(name) for name in ("Кафе 1", "Кафе 2"))
        self.kg = Unit.objects.create(code="kg-ot", name="кг")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Молочка", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.cafe1, self.cafe2])
        self.base = f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}"

    def venue(self, name):
        venue = Venue.objects.create(display_name=name)
        VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        return venue

    def supplier(self, name, **product_terms):
        supplier = Supplier.objects.create(legal_name=f"{name} LLC", display_name=name)
        user = User.objects.create(email=f"ot-{supplier.id}@example.com", full_name=name, role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=supplier, user=user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        product = SupplierProduct.objects.create(
            supplier=supplier, supplier_name=f"Сливки {name}", normalized_name="сливки", **product_terms
        )
        return supplier, user, product

    def offer_url(self, supplier):
        return f"/api/v1/catalog/suppliers/{supplier.id}/tenders/{self.tender.id}/items/{self.item.id}/offer"

    def submit(self, supplier, user, product):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.put(
            self.offer_url(supplier),
            {
                "supplier_product_id": str(product.id),
                "price": "300.00",
                "venue_ids": [str(self.cafe1.id), str(self.cafe2.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def distribute(self, offer_id, venues):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.post(
            f"{self.base}/offers/{offer_id}/distribution", {"venue_ids": [str(v.id) for v in venues]}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)

    def buyer_rows(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get(f"{self.base}/offers")
        self.assertEqual(response.status_code, 200, response.content)
        return {row["supplier_name"]: row for row in response.json()}

    def test_buyer_row_has_supplier_and_product_terms(self):
        supplier, user, product = self.supplier(
            "Молочный двор",
            net_value=Decimal("0.5"),
            net_unit=self.kg,
            min_order_qty=Decimal("2"),
            delivery_multiple=Decimal("1"),
        )
        self.submit(supplier, user, product)
        row = self.buyer_rows()["Молочный двор"]
        self.assertEqual(
            (
                row["supplier_product_net_value"],
                row["supplier_product_net_unit"],
                row["supplier_product_min_order_qty"],
                row["supplier_product_delivery_multiple"],
                row["distributed_venue_ids"],
            ),
            ("0.5000", "kg-ot", "2.0000", "1.0000", []),
        )

    def test_distributed_venues_belong_to_each_offer(self):
        parmalat, parmalat_user, parmalat_product = self.supplier("Parmalat")
        petmol, petmol_user, petmol_product = self.supplier("Petmol")
        self.distribute(self.submit(parmalat, parmalat_user, parmalat_product)["id"], [self.cafe1])
        self.distribute(self.submit(petmol, petmol_user, petmol_product)["id"], [self.cafe2])
        rows = self.buyer_rows()
        self.assertEqual(rows["Parmalat"]["distributed_venue_ids"], [str(self.cafe1.id)])
        self.assertEqual(rows["Petmol"]["distributed_venue_ids"], [str(self.cafe2.id)])

    def test_deleted_product_gives_nulls(self):
        supplier, user, product = self.supplier("Удалённый", net_value=Decimal("1"), net_unit=self.kg)
        self.submit(supplier, user, product)
        TenderOffer.objects.filter(tender_item=self.item).update(supplier_product=None)
        row = self.buyer_rows()["Удалённый"]
        self.assertEqual(
            (row["supplier_product_net_value"], row["supplier_product_net_unit"], row["distributed_venue_ids"]),
            (None, None, []),
        )

    def test_supplier_sees_own_distribution_everywhere(self):
        supplier, user, product = self.supplier("Parmalat")
        offer_id = self.submit(supplier, user, product)["id"]
        self.distribute(offer_id, [self.cafe1])
        expected = [str(self.cafe1.id)]

        self.assertEqual(self.submit(supplier, user, product)["distributed_venue_ids"], expected)
        items = self.client.get(f"/api/v1/catalog/suppliers/{supplier.id}/tenders/{self.tender.id}/items").json()
        self.assertEqual(items[0]["my_offer"]["distributed_venue_ids"], expected)
        self.assertEqual(self.client.delete(self.offer_url(supplier)).json()["distributed_venue_ids"], expected)

    def test_query_count_does_not_grow_with_offers(self):
        first, first_user, first_product = self.supplier("Первый", net_unit=self.kg)
        self.distribute(self.submit(first, first_user, first_product)["id"], [self.cafe1])
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        with CaptureQueriesContext(connection) as one_offer:
            self.client.get(f"{self.base}/offers")
        for name in ("Второй", "Третий"):
            supplier, user, product = self.supplier(name, net_unit=self.kg)
            self.distribute(self.submit(supplier, user, product)["id"], [self.cafe2])
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        with CaptureQueriesContext(connection) as three_offers:
            response = self.client.get(f"{self.base}/offers")
        self.assertEqual(len(response.json()), 3)
        self.assertEqual(len(three_offers), len(one_offer))


class TenderDistributionArchivedRowAPITests(APITestCase):
    """TASK-95 (docs/adr/0008): a venue whose directory row for the master
    product is archived used to fail distribution with 400 «уже привязан»
    (the unique index ignores status). Distribution restores that same row;
    an archived row is not a current assignment for «Распределено»."""

    def setUp(self):
        self.owner = User.objects.create(email="ar-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Кафе Архив")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%")
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Молочка", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])
        supplier = Supplier.objects.create(legal_name="Cream LLC", display_name="Cream")
        self.product = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Сливки", normalized_name="сливки"
        )
        self.offer = TenderOffer.objects.create(
            tender_item=self.item, supplier=supplier, supplier_product=self.product, price="320.00"
        )
        self.offer.venues.set([self.venue])
        self.row = VenueProductMatrixItem.objects.create(
            venue=self.venue,
            master_product=self.master_product,
            requested_name="Сливки 33% (старая)",
            supplier_product=self.product,
            status=EntityStatus.ARCHIVED,
        )
        self.url = f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/offers/{self.offer.id}/distribution"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def distributed_count(self):
        response = self.client.get(f"/api/v1/catalog/tenders/{self.tender.id}/items")
        return response.json()[0]["distributed_venues_count"]

    def test_archived_row_is_not_a_current_assignment(self):
        self.assertEqual(self.distributed_count(), 0)

    def test_preview_works_and_writes_nothing(self):
        response = self.client.get(self.url, {"venue_id": str(self.venue.id)})
        self.assertEqual(response.status_code, 200, response.content)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, EntityStatus.ARCHIVED)
        self.assertFalse(AuditLog.objects.filter(entity_id=self.row.id).exists())

    def test_apply_restores_the_same_row_and_records_it(self):
        response = self.client.post(self.url, {"venue_ids": [str(self.venue.id)]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        rows = VenueProductMatrixItem.objects.filter(venue=self.venue, master_product=self.master_product)
        self.assertEqual(
            list(rows.values_list("id", "status", "supplier_product_id")),
            [(self.row.id, EntityStatus.ACTIVE, self.product.id)],
        )
        log = AuditLog.objects.get(entity_id=self.row.id)
        self.assertEqual(log.action, AuditLogAction.UPDATED)
        self.assertEqual(log.summary, "Восстановлено из архива при распределении тендера «Молочка»")
        self.assertEqual(self.distributed_count(), 1)


class TenderDistributionAPITests(APITestCase):
    """Raw ТЗ §30-31: distributing an offer to venues assigns the supplier
    product AND fixes the individual price, atomically, with a "Сейчас/
    Будет" diff shown before commit — mirrors MasterProductTenderOfferView's
    already-shipped GET (preview) / POST (apply) pair."""

    def setUp(self):
        self.owner = User.objects.create(email="dist-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Dist")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="l", name="литр")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки Dist", unit=unit)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Дистрибуция", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])

        self.supplier = Supplier.objects.create(legal_name="Cream LLC", display_name="Cream Co")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Parmalat 35%", normalized_name="parmalat 35", unit=unit
        )
        self.offer = TenderOffer.objects.create(
            tender_item=self.item, supplier=self.supplier, supplier_product=self.supplier_product, price="320.00"
        )
        self.offer.venues.set([self.venue])

        # Distribution requires the venue to already have the master product
        # linked (see the task's documented scope boundary).
        self.matrix_item = VenueProductMatrixItem.objects.create(
            venue=self.venue, master_product=self.master_product, requested_name="Сливки 33%"
        )

        self.preview_url = (
            f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/offers/{self.offer.id}/distribution"
        )

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_preview_does_not_write(self):
        self.auth(self.owner)
        response = self.client.get(self.preview_url, {"venue_id": str(self.venue.id)})
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()[0]
        self.assertIsNone(row["current_supplier_product_id"])
        self.assertEqual(row["new_supplier_product_id"], str(self.supplier_product.id))
        self.matrix_item.refresh_from_db()
        self.assertIsNone(self.matrix_item.supplier_product_id)

    def test_apply_assigns_product_and_fixes_price(self):
        self.auth(self.owner)
        response = self.client.post(self.preview_url, {"venue_ids": [str(self.venue.id)]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.matrix_item.refresh_from_db()
        self.assertEqual(self.matrix_item.supplier_product_id, self.supplier_product.id)
        price = SupplierProductVenuePrice.objects.get(supplier_product=self.supplier_product, venue=self.venue)
        self.assertEqual(str(price.price), "320.00")
        self.assertEqual(price.source_type, SupplierProductVenuePriceSourceType.TENDER)
        self.assertEqual(price.source_tender_offer_id, self.offer.id)

    def test_rejects_venue_not_ready_to_deliver(self):
        other_venue = Venue.objects.create(display_name="Not Ready")
        VenueUser.objects.create(
            venue=other_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.item.venues.add(other_venue)
        self.auth(self.owner)
        response = self.client.post(self.preview_url, {"venue_ids": [str(other_venue.id)]}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_apply_auto_links_venue_without_existing_link(self):
        unlinked_venue = Venue.objects.create(display_name="Unlinked")
        VenueUser.objects.create(
            venue=unlinked_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.item.venues.add(unlinked_venue)
        self.offer.venues.add(unlinked_venue)
        self.auth(self.owner)
        response = self.client.post(self.preview_url, {"venue_ids": [str(unlinked_venue.id)]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        matrix_item = VenueProductMatrixItem.objects.get(venue=unlinked_venue, master_product=self.master_product)
        self.assertEqual(matrix_item.supplier_product_id, self.supplier_product.id)
        self.assertTrue(
            SupplierProductVenuePrice.objects.filter(
                supplier_product=self.supplier_product, venue=unlinked_venue
            ).exists()
        )

    def test_preview_unlinked_venue_does_not_create_link(self):
        unlinked_venue = Venue.objects.create(display_name="Unlinked Preview")
        VenueUser.objects.create(
            venue=unlinked_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.item.venues.add(unlinked_venue)
        self.offer.venues.add(unlinked_venue)
        self.auth(self.owner)
        response = self.client.get(self.preview_url, {"venue_id": str(unlinked_venue.id)})
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()[0]
        self.assertIsNone(row["current_supplier_product_id"])
        self.assertEqual(row["new_supplier_product_id"], str(self.supplier_product.id))
        self.assertFalse(
            VenueProductMatrixItem.objects.filter(venue=unlinked_venue, master_product=self.master_product).exists()
        )

    def test_apply_force_creates_despite_name_conflict(self):
        unlinked_venue = Venue.objects.create(display_name="Name Conflict")
        VenueUser.objects.create(
            venue=unlinked_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.item.venues.add(unlinked_venue)
        self.offer.venues.add(unlinked_venue)
        candidate = VenueProductMatrixItem.objects.create(
            venue=unlinked_venue, requested_name=self.master_product.canonical_name
        )
        self.auth(self.owner)
        response = self.client.post(self.preview_url, {"venue_ids": [str(unlinked_venue.id)]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        candidate.refresh_from_db()
        self.assertIsNone(candidate.master_product_id)
        new_item = VenueProductMatrixItem.objects.get(venue=unlinked_venue, master_product=self.master_product)
        self.assertNotEqual(new_item.id, candidate.id)
        self.assertEqual(new_item.supplier_product_id, self.supplier_product.id)

    def test_rejects_offer_with_deleted_supplier_product(self):
        self.offer.supplier_product = None
        self.offer.save(update_fields=["supplier_product", "updated_at"])
        self.auth(self.owner)

        get_response = self.client.get(self.preview_url, {"venue_id": str(self.venue.id)})
        self.assertEqual(get_response.status_code, 400)

        post_response = self.client.post(self.preview_url, {"venue_ids": [str(self.venue.id)]}, format="json")
        self.assertEqual(post_response.status_code, 400)

    def test_denied_for_stranger(self):
        stranger = User.objects.create(email="dist-stranger@example.com", full_name="X", role=UserRole.VENUE_USER)
        self.auth(stranger)
        response = self.client.post(self.preview_url, {"venue_ids": [str(self.venue.id)]}, format="json")
        self.assertEqual(response.status_code, 403)


class TenderAutoCompletionAPITests(APITestCase):
    """Raw ТЗ §7/§40: "После наступления срока тендер автоматически
    переходит в статус: Завершен". No background job exists anywhere in
    this project (no celery beat schedule) — completion is checked lazily,
    every time a Tender is loaded for a tender-scoped request, via
    sync_tender_completion. §42: after completion suppliers keep read
    access (history) but lose write access (submit/change offer)."""

    def setUp(self):
        self.owner = User.objects.create(email="auto-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Auto")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="kg-auto", name="кг")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Масло", unit=unit)
        self.tender = Tender.objects.create(
            created_by=self.owner,
            name="Истёкший",
            access=TenderAccess.OPEN,
            status=TenderStatus.ACTIVE,
            accepts_offers_until=timezone.now() - timedelta(hours=1),
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])

        self.supplier = Supplier.objects.create(legal_name="Butter LLC", display_name="Butter Co")
        self.supplier_user = User.objects.create(email="butter@example.com", full_name="Butter", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Масло 82.5%", normalized_name="масло 82.5", unit=unit
        )

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_list_shows_completed_status_for_expired_tender(self):
        self.auth(self.owner)
        response = self.client.get("/api/v1/catalog/tenders", {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200, response.content)
        row = next(r for r in response.json()["results"] if r["id"] == str(self.tender.id))
        self.assertEqual(row["status"], "completed")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, TenderStatus.COMPLETED)

    def test_expired_tender_rejects_new_offer(self):
        url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/items/{self.item.id}/offer"
        self.auth(self.supplier_user)
        response = self.client.put(
            url,
            {
                "supplier_product_id": str(self.supplier_product.id),
                "price": "500.00",
                "venue_ids": [str(self.venue.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, TenderStatus.COMPLETED)

    def test_expired_tender_still_viewable_by_supplier(self):
        url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/items"
        self.auth(self.supplier_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content)

    def test_add_item_rejected_after_expiry(self):
        url = f"/api/v1/catalog/tenders/{self.tender.id}/items"
        self.auth(self.owner)
        response = self.client.post(
            url,
            {"items": [{"master_product_id": str(uuid4()), "venue_ids": [str(self.venue.id)]}]},
            format="json",
        )
        # Any 400 here (including the unrelated bogus master_product_id)
        # would pass, so assert on the tender's own completion instead —
        # the important behavior is that add_tender_items is not reachable
        # via a stale ACTIVE status, proven directly.
        self.assertEqual(response.status_code, 400)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, TenderStatus.COMPLETED)


class TenderCompleteAPITests(APITestCase):
    """Raw ТЗ §40-41: manual completion — buyer clicks "Завершить тендер".
    Mirrors TenderPublishAPITests' shape (same HasTenderAccess gate, same
    "reject transition from the wrong status" style)."""

    def setUp(self):
        self.owner = User.objects.create(
            email="complete-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.stranger = User.objects.create(
            email="complete-stranger@example.com", full_name="X", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Complete")
        self.other_venue = Venue.objects.create(display_name="Cafe Complete 2")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.other_venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="kg-cmp", name="кг")
        self.mp1 = MasterProduct.objects.create(owner=self.owner, canonical_name="Сыр Complete", unit=unit)
        self.mp2 = MasterProduct.objects.create(owner=self.owner, canonical_name="Йогурт Complete", unit=unit)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Молочка", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item1 = TenderItem.objects.create(tender=self.tender, master_product=self.mp1)
        self.item1.venues.set([self.venue, self.other_venue])
        self.item2 = TenderItem.objects.create(tender=self.tender, master_product=self.mp2)
        self.item2.venues.set([self.venue])

        self.supplier = Supplier.objects.create(legal_name="Dairy LLC", display_name="Dairy Co")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сыр 45%", normalized_name="сыр 45", unit=unit
        )
        self.offer1 = TenderOffer.objects.create(
            tender_item=self.item1, supplier=self.supplier, supplier_product=self.supplier_product, price="700.00"
        )
        self.offer1.venues.set([self.venue, self.other_venue])

        # venue is distributed for item1 (matrix row points at the offered
        # product, its price there was fixed from the offer — docs/adr/0004);
        # other_venue and item2's venue are not.
        VenueProductMatrixItem.objects.create(
            venue=self.venue,
            master_product=self.mp1,
            requested_name="Сыр",
            supplier_product=self.supplier_product,
        )
        SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price="700.00",
            source_type=SupplierProductVenuePriceSourceType.TENDER,
            source_tender_offer=self.offer1,
        )
        VenueProductMatrixItem.objects.create(venue=self.other_venue, master_product=self.mp1, requested_name="Сыр")
        VenueProductMatrixItem.objects.create(venue=self.venue, master_product=self.mp2, requested_name="Йогурт")

        self.url = f"/api/v1/catalog/tenders/{self.tender.id}/complete"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_summary_counts(self):
        self.auth(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["items_count"], 2)
        self.assertEqual(data["offers_count"], 1)
        self.assertEqual(data["distributed"], 1)
        self.assertEqual(data["undistributed"], 2)

    def test_complete_active_tender(self):
        self.auth(self.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "completed")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, TenderStatus.COMPLETED)

    def test_complete_draft_rejected(self):
        draft_tender = Tender.objects.create(created_by=self.owner, name="Черновик", access=TenderAccess.OPEN)
        self.auth(self.owner)
        response = self.client.post(f"/api/v1/catalog/tenders/{draft_tender.id}/complete")
        self.assertEqual(response.status_code, 400)

    def test_complete_twice_rejected(self):
        self.auth(self.owner)
        self.client.post(self.url)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 400)

    def test_complete_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 403)


class TenderPostCompletionEnforcementAPITests(APITestCase):
    """Raw ТЗ §42-43: after completion, distribution (a buyer write action)
    stops; before completion, a fully-distributed position keeps accepting
    new/changed offers regardless ("это обязательное правило", §43)."""

    def setUp(self):
        self.owner = User.objects.create(email="pce-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe PCE")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="l-pce", name="литр")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки PCE", unit=unit)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="ПослеЗавершения", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])

        self.supplier = Supplier.objects.create(legal_name="Cream PCE LLC", display_name="Cream PCE Co")
        self.supplier_user = User.objects.create(
            email="cream-pce@example.com", full_name="CreamPCE", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сливки 35% PCE", normalized_name="сливки 35 pce", unit=unit
        )
        self.offer = TenderOffer.objects.create(
            tender_item=self.item, supplier=self.supplier, supplier_product=self.supplier_product, price="310.00"
        )
        self.offer.venues.set([self.venue])
        self.matrix_item = VenueProductMatrixItem.objects.create(
            venue=self.venue, master_product=self.master_product, requested_name="Сливки"
        )
        self.distribution_url = (
            f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/offers/{self.offer.id}/distribution"
        )

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_distribution_preview_blocked_after_completion(self):
        self.tender.status = TenderStatus.COMPLETED
        self.tender.save(update_fields=["status"])
        self.auth(self.owner)
        response = self.client.get(self.distribution_url, {"venue_id": str(self.venue.id)})
        self.assertEqual(response.status_code, 400)

    def test_distribution_apply_blocked_after_completion(self):
        self.tender.status = TenderStatus.COMPLETED
        self.tender.save(update_fields=["status"])
        self.auth(self.owner)
        response = self.client.post(self.distribution_url, {"venue_ids": [str(self.venue.id)]}, format="json")
        self.assertEqual(response.status_code, 400)
        self.matrix_item.refresh_from_db()
        self.assertIsNone(self.matrix_item.supplier_product_id)

    def test_fully_distributed_position_still_accepts_new_offer(self):
        self.auth(self.owner)
        apply_response = self.client.post(self.distribution_url, {"venue_ids": [str(self.venue.id)]}, format="json")
        self.assertEqual(apply_response.status_code, 200, apply_response.content)

        offer_url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/items/{self.item.id}/offer"
        self.auth(self.supplier_user)
        response = self.client.put(
            offer_url,
            {
                "supplier_product_id": str(self.supplier_product.id),
                "price": "330.00",
                "venue_ids": [str(self.venue.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["price"], "330.00")


class MasterCatalogUnitListAPITests(APITestCase):
    """TASK-49: Мастер-справочник (CoreUser JWT) нужен свой список единиц
    измерения для дропдауна "Ед.Изм. в УС" — /suppliers/units существует,
    но сидит на токене кабинета поставщика и недоступен venue-стороне."""

    url = "/api/v1/catalog/units"

    def setUp(self):
        self.user = User.objects.create(email="units-user@example.com", full_name="U", role=UserRole.VENUE_USER)

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))

    def test_rejects_request_without_auth(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_returns_active_units_for_core_user(self):
        Unit.objects.create(code="кг-cu", name="Килограмм", kind=UnitKind.WEIGHT)
        Unit.objects.create(code="шт-cu", name="Штука", kind=UnitKind.PIECE)
        self.auth()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        codes = {u["code"] for u in response.json()}
        self.assertTrue({"кг-cu", "шт-cu"}.issubset(codes))

    def test_filters_by_kind(self):
        Unit.objects.create(code="кг-cu2", name="Килограмм", kind=UnitKind.WEIGHT)
        Unit.objects.create(code="шт-cu2", name="Штука", kind=UnitKind.PIECE)
        self.auth()

        response = self.client.get(self.url, {"kind": "weight"})

        codes = {u["code"] for u in response.json()}
        self.assertIn("кг-cu2", codes)
        self.assertNotIn("шт-cu2", codes)

    def test_excludes_inactive_units(self):
        Unit.objects.create(code="архив-cu", name="Архивная", kind=UnitKind.PIECE, status=EntityStatus.INACTIVE)
        self.auth()

        response = self.client.get(self.url)

        codes = {u["code"] for u in response.json()}
        self.assertNotIn("архив-cu", codes)


class MasterProductDetailAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner7@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(
            email="stranger3@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe G")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.mp = create_master_product(owner=self.owner, canonical_name="Гречка")
        self.url = f"/api/v1/catalog/master-products/{self.mp.id}"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_get(self):
        self.auth(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["canonical_name"], "Гречка")

    def test_get_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_get_missing_returns_403(self):
        # Matches HasMasterCatalogAccess's documented behavior (Task 4): an
        # unknown master_product_id fails the permission check itself (403),
        # same as this codebase's other venue_id/supplier_id-based permission
        # classes — it never reaches the view to 404.
        self.auth(self.owner)
        response = self.client.get(f"/api/v1/catalog/master-products/{uuid4()}")
        self.assertEqual(response.status_code, 403)

    def test_patch_updates_field(self):
        self.auth(self.owner)
        response = self.client.patch(self.url, {"canonical_name": "Гречка ядрица"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["canonical_name"], "Гречка ядрица")

    def test_patch_empty_body_rejected(self):
        self.auth(self.owner)
        response = self.client.patch(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_delete_is_hard_delete_and_clears_links(self):
        item = link_master_product_to_venue(master_product=self.mp, venue=self.venue)
        self.auth(self.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(MasterProduct.objects.filter(pk=self.mp.pk).exists())
        item.refresh_from_db()
        self.assertIsNone(item.master_product)
        self.assertTrue(VenueProductMatrixItem.objects.filter(pk=item.pk).exists())

    def test_companies_count_matches_breakdown_after_venue_archived(self):
        # 2026-09-01 fix wave (Fix 2), API-level regression: archiving a
        # linked venue (delete_venue_cabinet's path) must drop it from
        # companies_count on both the list and detail responses, matching
        # what the "Компаний: N" breakdown endpoint already shows.
        link_master_product_to_venue(master_product=self.mp, venue=self.venue)
        self.venue.status = EntityStatus.ARCHIVED
        self.venue.save(update_fields=["status", "updated_at"])
        self.auth(self.owner)

        list_response = self.client.get("/api/v1/catalog/master-products", {"owner_id": str(self.owner.id)})
        detail_response = self.client.get(self.url)
        breakdown_response = self.client.get(f"{self.url}/venues")

        self.assertEqual(list_response.json()["results"][0]["companies_count"], 0)
        self.assertEqual(detail_response.json()["companies_count"], 0)
        self.assertEqual(len(breakdown_response.json()), 0)

    def test_patch_returns_404_for_non_active_master_product(self):
        # 2026-09-01 fix wave (Fix 5): patch/delete must agree with get() on
        # what "exists" means for this endpoint — both go through
        # master_product_detail_queryset() now, not an unfiltered
        # get_object_or_404(MasterProduct, ...). Unreachable via the real
        # API today (hard-delete-only, no archive path for MasterProduct) —
        # status is flipped directly via the ORM to prove consistency.
        self.mp.status = EntityStatus.ARCHIVED
        self.mp.save(update_fields=["status", "updated_at"])
        self.auth(self.owner)
        response = self.client.patch(self.url, {"canonical_name": "x"}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_delete_returns_404_for_non_active_master_product(self):
        self.mp.status = EntityStatus.ARCHIVED
        self.mp.save(update_fields=["status", "updated_at"])
        self.auth(self.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 404)


class MasterProductVenueBreakdownAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner8@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue_linked = Venue.objects.create(display_name="Cafe H")
        self.venue_unlinked = Venue.objects.create(display_name="Cafe I")
        VenueUser.objects.create(
            venue=self.venue_linked, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.venue_unlinked, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.mp = create_master_product(owner=self.owner, canonical_name="Овсянка")
        link_master_product_to_venue(master_product=self.mp, venue=self.venue_linked)
        self.url = f"/api/v1/catalog/master-products/{self.mp.id}/venues"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_breakdown_lists_all_owner_venues(self):
        self.auth(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        rows = {row["venue_id"]: row for row in response.json()}
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[str(self.venue_linked.id)]["linked"])
        self.assertEqual(rows[str(self.venue_linked.id)]["requested_name"], "Овсянка")
        self.assertFalse(rows[str(self.venue_unlinked.id)]["linked"])
        self.assertIsNone(rows[str(self.venue_unlinked.id)]["matrix_item_id"])

    def test_breakdown_denied_for_stranger(self):
        stranger = User.objects.create(email="stranger4@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
        self.auth(stranger)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_breakdown_shows_individual_price_where_active_and_base_price_elsewhere(self):
        # 2026-09-01 final-review fix wave, I2: the headline integration —
        # the "Компаний: N" modal must actually show the individual
        # (SupplierProductVenuePrice) price where one is active, and fall
        # back to SupplierProduct.current_price for the other venues.
        supplier = Supplier.objects.create(display_name="Поставщик Ж")
        supplier_product = SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Овсянка 5кг",
            normalized_name="Овсянка 5кг",
            current_price=Decimal("500.00"),
        )
        link_master_product_to_venue(master_product=self.mp, venue=self.venue_unlinked)
        VenueProductMatrixItem.objects.filter(master_product=self.mp).update(supplier_product=supplier_product)
        (individual,) = set_supplier_product_venue_price(
            supplier_product=supplier_product,
            venues=[self.venue_linked],
            price=Decimal("450.00"),
            created_by=self.owner,
        )
        self.assertEqual(individual.status, SupplierProductVenuePriceStatus.ACTIVE)

        rows = {row.venue_id: row for row in master_product_venue_breakdown(self.mp)}
        self.assertEqual(rows[self.venue_linked.id].price, Decimal("450.00"))
        self.assertEqual(rows[self.venue_unlinked.id].price, Decimal("500.00"))

        self.auth(self.owner)
        api_rows = {row["venue_id"]: row for row in self.client.get(self.url).json()}
        self.assertEqual(api_rows[str(self.venue_linked.id)]["price"], "450.00")
        self.assertEqual(api_rows[str(self.venue_unlinked.id)]["price"], "500.00")


class MasterProductVenueLinkAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner9@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe J")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.mp = create_master_product(owner=self.owner, canonical_name="Перловка")
        self.url = f"/api/v1/catalog/master-products/{self.mp.id}/venues/{self.venue.id}"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_link_creates_matrix_item(self):
        self.auth(self.owner)
        response = self.client.put(self.url)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["requested_name"], "Перловка")
        # Fix 4 (2026-09-01 fix wave): the link endpoint's own response must
        # already show the link it just created.
        self.assertEqual(data["master_product_id"], str(self.mp.id))
        self.assertEqual(data["source"], VenueProductMatrixItemSource.MASTER_CATALOG)
        self.assertIn("needs_review", data)
        item = VenueProductMatrixItem.objects.get(pk=data["id"])
        self.assertEqual(item.master_product_id, self.mp.id)
        self.assertEqual(item.source, VenueProductMatrixItemSource.MASTER_CATALOG)

    def test_link_returns_409_with_candidate_when_similar_unlinked_item_exists(self):
        candidate = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="перловка")
        self.auth(self.owner)
        response = self.client.put(self.url)
        self.assertEqual(response.status_code, 409, response.content)
        data = response.json()
        self.assertEqual(data["candidate"]["id"], str(candidate.id))
        self.assertFalse(VenueProductMatrixItem.objects.filter(master_product=self.mp).exists())

    def test_link_force_create_query_param_creates_despite_similar_item(self):
        VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="перловка")
        self.auth(self.owner)
        response = self.client.put(f"{self.url}?force_create=true")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(VenueProductMatrixItem.objects.filter(venue=self.venue).count(), 2)

    def test_link_existing_item_id_query_param_links_and_returns_200(self):
        candidate = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Перловая крупа")
        self.auth(self.owner)
        response = self.client.put(f"{self.url}?link_existing_item_id={candidate.id}")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["id"], str(candidate.id))
        self.assertEqual(VenueProductMatrixItem.objects.filter(venue=self.venue).count(), 1)

    def test_link_twice_is_rejected(self):
        self.auth(self.owner)
        self.client.put(self.url)
        response = self.client.put(self.url)
        self.assertEqual(response.status_code, 400)

    def test_link_to_venue_not_owned_by_owner_is_rejected(self):
        other_venue = Venue.objects.create(display_name="Cafe K")
        self.auth(self.owner)
        response = self.client.put(f"/api/v1/catalog/master-products/{self.mp.id}/venues/{other_venue.id}")
        self.assertEqual(response.status_code, 400)

    def test_link_to_archived_venue_is_rejected(self):
        # 2026-09-01 fix wave (Fix 3): _is_owners_active_venue must also
        # reject an archived venue, not just one the owner never owned —
        # same root cause as Fix 2 (delete_venue_cabinet soft-archives a
        # venue but leaves VenueUser rows in place).
        self.venue.status = EntityStatus.ARCHIVED
        self.venue.save(update_fields=["status", "updated_at"])
        self.auth(self.owner)
        response = self.client.put(self.url)
        self.assertEqual(response.status_code, 400, response.content)

    def test_unlink_clears_link_without_deleting_row(self):
        self.auth(self.owner)
        item_id = self.client.put(self.url).json()["id"]
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 204)
        item = VenueProductMatrixItem.objects.get(pk=item_id)
        self.assertIsNone(item.master_product)

    def test_link_denied_for_stranger(self):
        stranger = User.objects.create(email="stranger5@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
        self.auth(stranger)
        response = self.client.put(self.url)
        self.assertEqual(response.status_code, 403)


class VenueProductPickerAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="picker-owner@example.com", full_name="Picker Owner", role=UserRole.VENUE_USER
        )
        self.manager = User.objects.create(
            email="picker-manager@example.com", full_name="Picker Manager", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Picker Cafe")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        VenueUser.objects.create(
            venue=self.venue, user=self.manager, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сливки кокосовые 33%")
        self.master = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33% 1 л")
        self.suggestions_url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/master-product-suggestions"
        self.decision_url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/{self.item.id}/master-product"

    def auth(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.manager))

    def test_master_suggestions_support_initial_recommendations_and_manual_search(self):
        self.auth()
        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products", return_value=[(self.master, 0.94)]
        ):
            response = self.client.get(self.suggestions_url, {"name": "Сливки кокосовые", "q": "Сливки"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["mode"], "search")
        self.assertEqual(data["recommended"][0]["master_product_id"], str(self.master.id))
        self.assertEqual(data["recommended"][0]["score"], 0.94)
        self.assertTrue(data["recommended"][0]["can_link"])
        self.assertEqual(data["search_results"][0]["master_product_id"], str(self.master.id))

    def test_master_suggestions_mark_an_occupied_master_product(self):
        VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Другие сливки", master_product=self.master
        )
        self.auth()
        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products", return_value=[(self.master, 0.94)]
        ):
            response = self.client.get(self.suggestions_url, {"name": "Сливки"})
        self.assertEqual(response.status_code, 200, response.content)
        suggestion = response.json()["recommended"][0]
        self.assertTrue(suggestion["already_linked_to_venue"])
        self.assertFalse(suggestion["can_link"])

    def test_create_company_product_can_apply_picker_choice_atomically(self):
        self.auth()
        url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix"
        response = self.client.post(
            url,
            {
                "requested_name": "Сливки для соуса",
                "requested_unit_label": "л",
                "master_product_decision": {
                    "action": "link_existing",
                    "master_product_id": str(self.master.id),
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["master_product_id"], str(self.master.id))
        self.assertEqual(response.json()["master_match_state"], "matched")

    def test_create_company_product_rolls_back_when_picker_choice_conflicts(self):
        VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Занятая строка", master_product=self.master
        )
        self.auth()
        url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix"
        before = VenueProductMatrixItem.objects.filter(venue=self.venue).count()
        response = self.client.post(
            url,
            {
                "requested_name": "Не должна сохраниться",
                "master_product_decision": {
                    "action": "link_existing",
                    "master_product_id": str(self.master.id),
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(VenueProductMatrixItem.objects.filter(venue=self.venue).count(), before)

    def test_row_first_decision_can_link_create_and_unlink(self):
        self.auth()
        linked = self.client.put(
            self.decision_url,
            {"action": "link_existing", "master_product_id": str(self.master.id)},
            format="json",
        )
        self.assertEqual(linked.status_code, 200, linked.content)
        self.assertEqual(linked.json()["master_product_id"], str(self.master.id))

        unlinked = self.client.put(self.decision_url, {"action": "unlink"}, format="json")
        self.assertEqual(unlinked.status_code, 200, unlinked.content)
        self.assertIsNone(unlinked.json()["master_product_id"])

        created = self.client.put(
            self.decision_url,
            {"action": "create_new", "canonical_name": "Сливки кокосовые 33%", "note": "выбор пользователя"},
            format="json",
        )
        self.assertEqual(created.status_code, 200, created.content)
        new_master = MasterProduct.objects.get(pk=created.json()["master_product_id"])
        self.assertEqual(new_master.canonical_name, "Сливки кокосовые 33%")
        self.assertEqual(new_master.note, "выбор пользователя")

    def test_row_first_decision_returns_409_when_master_is_occupied(self):
        occupied = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Занятая строка", master_product=self.master
        )
        self.auth()
        response = self.client.put(
            self.decision_url,
            {"action": "link_existing", "master_product_id": str(self.master.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["candidate"]["id"], str(occupied.id))

    def test_supplier_suggestions_use_name_when_q_is_absent(self):
        supplier = Supplier.objects.create(display_name="Поставщик Picker", is_published=True)
        product = SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Сливки 33%",
            normalized_name="Сливки 33%",
            current_price=Decimal("250.00"),
        )
        self.auth()
        url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/supplier-product-suggestions"
        with patch(
            "api.v1.catalog.views._shared.catalog_products_queryset",
            return_value=SupplierProduct.objects.filter(pk=product.pk).select_related(
                "supplier", "unit", "brand", "category"
            ),
        ) as mocked:
            response = self.client.get(url, {"name": "Сливки кокосовые", "limit": 5})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["results"][0]["id"], str(product.id))
        self.assertEqual(mocked.call_args.args[0]["q"], "Сливки кокосовые")

    def test_supplier_suggestions_offset_moves_to_second_page(self):
        # TASK-53 — 2026-09-11, Kharitonov: "тут наверное пагинация нужна? в апи только лимит".
        # The view used to hardcode offset=0 when building CatalogProductQuerySerializer, so a
        # client could never reach matches beyond the first `limit` even though `count` in the
        # response said there were more.
        supplier = Supplier.objects.create(display_name="Поставщик Picker 2", is_published=True)
        products = [
            SupplierProduct.objects.create(
                supplier=supplier,
                supplier_name=f"Сливки {i}",
                normalized_name=f"Сливки {i}",
                current_price=Decimal("100.00"),
            )
            for i in range(3)
        ]
        self.auth()
        url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/supplier-product-suggestions"
        queryset = SupplierProduct.objects.filter(pk__in=[p.pk for p in products]).order_by("supplier_name")
        with patch("api.v1.catalog.views._shared.catalog_products_queryset", return_value=queryset):
            first_page = self.client.get(url, {"name": "Сливки", "limit": 1, "offset": 0})
            second_page = self.client.get(url, {"name": "Сливки", "limit": 1, "offset": 1})
        self.assertEqual(first_page.status_code, 200, first_page.content)
        self.assertEqual(second_page.status_code, 200, second_page.content)
        self.assertEqual(first_page.json()["count"], 3)
        self.assertEqual(second_page.json()["count"], 3)
        self.assertNotEqual(first_page.json()["results"][0]["id"], second_page.json()["results"][0]["id"])
        self.assertEqual(second_page.json()["results"][0]["id"], str(products[1].id))

    def test_master_suggestions_search_results_support_offset_and_count(self):
        # TASK-53 — same complaint, master-product-suggestions side: search_results had no
        # count/offset at all, only a `[:limit]` slice from the start every time. Uses "Гречка"
        # (not "Сливки", already taken by setUp's self.master) so the count is exact.
        others = [MasterProduct.objects.create(owner=self.owner, canonical_name=f"Гречка {i}") for i in range(3)]
        self.auth()
        first_page = self.client.get(self.suggestions_url, {"q": "Гречка", "limit": 1, "offset": 0})
        second_page = self.client.get(self.suggestions_url, {"q": "Гречка", "limit": 1, "offset": 1})
        self.assertEqual(first_page.status_code, 200, first_page.content)
        self.assertEqual(second_page.status_code, 200, second_page.content)
        self.assertEqual(first_page.json()["search_results_count"], 3)
        self.assertEqual(second_page.json()["search_results_count"], 3)
        ordered = sorted(others, key=lambda mp: (mp.canonical_name, str(mp.id)))
        self.assertEqual(first_page.json()["search_results"][0]["master_product_id"], str(ordered[0].id))
        self.assertEqual(second_page.json()["search_results"][0]["master_product_id"], str(ordered[1].id))

    def test_master_suggestions_recommended_ignores_offset(self):
        # `recommended` stays top-N by design (best matches first) — offset only moves
        # search_results, never the recommendation ranking.
        self.auth()
        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products", return_value=[(self.master, 0.94)]
        ):
            response = self.client.get(self.suggestions_url, {"name": "Сливки кокосовые", "offset": 1})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["recommended"][0]["master_product_id"], str(self.master.id))


class VenueProductImportAliasModelTests(TestCase):
    def test_unique_constraint_on_venue_and_raw_name(self):
        owner = User.objects.create(email="owner12@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        venue = Venue.objects.create(display_name="Cafe Import")
        item = VenueProductMatrixItem.objects.create(venue=venue, requested_name="Мука")
        VenueProductImportAlias.objects.create(
            venue=venue, raw_name="мука в/с 1кг", matrix_item=item, confirmed_by=owner
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VenueProductImportAlias.objects.create(
                    venue=venue, raw_name="мука в/с 1кг", matrix_item=item, confirmed_by=owner
                )

    def test_deleting_matrix_item_cascades_alias(self):
        owner = User.objects.create(email="owner13@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        venue = Venue.objects.create(display_name="Cafe Import 2")
        item = VenueProductMatrixItem.objects.create(venue=venue, requested_name="Соль")
        alias = VenueProductImportAlias.objects.create(
            venue=venue, raw_name="соль экстра", matrix_item=item, confirmed_by=owner
        )
        item.delete()
        self.assertFalse(VenueProductImportAlias.objects.filter(pk=alias.pk).exists())


class MasterProductBulkSupplierProductTargetingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner10@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue_a = Venue.objects.create(display_name="Cafe L")
        self.venue_b = Venue.objects.create(display_name="Cafe M")
        for venue in (self.venue_a, self.venue_b):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.mp = create_master_product(owner=self.owner, canonical_name="Гречка")
        self.item_a = link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)
        self.item_b = link_master_product_to_venue(master_product=self.mp, venue=self.venue_b)
        self.supplier = Supplier.objects.create(display_name="Поставщик З")
        self.old_sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Гречка 1кг",
            normalized_name="Гречка 1кг",
            current_price=Decimal("100.00"),
        )
        self.new_sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Гречка 5кг",
            normalized_name="Гречка 5кг",
            current_price=Decimal("450.00"),
        )
        VenueProductMatrixItem.objects.filter(pk=self.item_a.pk).update(supplier_product=self.old_sp)

    def test_targets_defaults_to_all_linked_venues(self):
        items = master_product_bulk_supplier_product_targets(master_product=self.mp)
        self.assertEqual({i.id for i in items}, {self.item_a.id, self.item_b.id})

    def test_targets_restricts_to_requested_venue_ids(self):
        items = master_product_bulk_supplier_product_targets(master_product=self.mp, venue_ids=[self.venue_a.id])
        self.assertEqual([i.id for i in items], [self.item_a.id])

    def test_targets_rejects_unlinked_venue_id(self):
        other_venue = Venue.objects.create(display_name="Cafe N")
        with self.assertRaises(ValidationError):
            master_product_bulk_supplier_product_targets(master_product=self.mp, venue_ids=[other_venue.id])

    def test_targets_excludes_matrix_item_of_archived_venue(self):
        self.venue_a.status = EntityStatus.ARCHIVED
        self.venue_a.save(update_fields=["status", "updated_at"])
        items = master_product_bulk_supplier_product_targets(master_product=self.mp)
        self.assertEqual([i.id for i in items], [self.item_b.id])

    def test_targets_rejects_explicit_archived_venue_id_as_unlinked(self):
        self.venue_a.status = EntityStatus.ARCHIVED
        self.venue_a.save(update_fields=["status", "updated_at"])
        with self.assertRaises(ValidationError):
            master_product_bulk_supplier_product_targets(master_product=self.mp, venue_ids=[self.venue_a.id])

    def test_diff_marks_changed_rows(self):
        items = master_product_bulk_supplier_product_targets(master_product=self.mp)
        rows = {
            row.venue_id: row
            for row in master_product_bulk_supplier_product_diff(items=items, new_supplier_product=self.new_sp)
        }
        self.assertTrue(rows[self.venue_a.id].changed)
        self.assertEqual(rows[self.venue_a.id].current_supplier_product_id, self.old_sp.id)
        self.assertEqual(rows[self.venue_a.id].new_supplier_product_id, self.new_sp.id)
        self.assertTrue(rows[self.venue_b.id].changed)
        self.assertIsNone(rows[self.venue_b.id].current_supplier_product_id)

    def test_diff_marks_unchanged_row_when_already_assigned(self):
        items = master_product_bulk_supplier_product_targets(master_product=self.mp)
        rows = {
            row.venue_id: row
            for row in master_product_bulk_supplier_product_diff(items=items, new_supplier_product=self.old_sp)
        }
        self.assertFalse(rows[self.venue_a.id].changed)

    def test_apply_writes_supplier_product_on_all_default_targets(self):
        bulk_assign_supplier_product_to_master_product(master_product=self.mp, supplier_product=self.new_sp)
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_a.supplier_product_id, self.new_sp.id)
        self.assertEqual(self.item_b.supplier_product_id, self.new_sp.id)

    def test_apply_restricts_to_requested_venue_ids(self):
        bulk_assign_supplier_product_to_master_product(
            master_product=self.mp, supplier_product=self.new_sp, venue_ids=[self.venue_a.id]
        )
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_a.supplier_product_id, self.new_sp.id)
        self.assertIsNone(self.item_b.supplier_product_id)

    def test_apply_returns_diff_rows_reflecting_state_before_write(self):
        rows = {
            row.venue_id: row
            for row in bulk_assign_supplier_product_to_master_product(
                master_product=self.mp, supplier_product=self.new_sp
            )
        }
        self.assertEqual(rows[self.venue_a.id].current_supplier_product_id, self.old_sp.id)
        self.assertTrue(rows[self.venue_a.id].changed)

    def test_apply_rejects_unlinked_venue_id_without_writing_anything(self):
        other_venue = Venue.objects.create(display_name="Cafe N2")
        with self.assertRaises(ValidationError):
            bulk_assign_supplier_product_to_master_product(
                master_product=self.mp, supplier_product=self.new_sp, venue_ids=[self.venue_a.id, other_venue.id]
            )
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.supplier_product_id, self.old_sp.id)


class MasterProductBulkSupplierProductAssignmentAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner11@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue_a = Venue.objects.create(display_name="Cafe O")
        self.venue_b = Venue.objects.create(display_name="Cafe P")
        for venue in (self.venue_a, self.venue_b):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.mp = create_master_product(owner=self.owner, canonical_name="Соль")
        self.item_a = link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)
        self.item_b = link_master_product_to_venue(master_product=self.mp, venue=self.venue_b)
        self.supplier = Supplier.objects.create(display_name="Поставщик И")
        self.new_sp = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Соль 1кг", normalized_name="Соль 1кг", current_price=Decimal("50.00")
        )
        self.url = f"/api/v1/catalog/master-products/{self.mp.id}/bulk-supplier-product"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_preview_does_not_write(self):
        self.auth(self.owner)
        response = self.client.get(self.url, {"supplier_product_id": str(self.new_sp.id)})
        self.assertEqual(response.status_code, 200, response.content)
        rows = {row["venue_id"]: row for row in response.json()}
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[str(self.venue_a.id)]["changed"])
        self.item_a.refresh_from_db()
        self.assertIsNone(self.item_a.supplier_product_id)

    def test_preview_restricts_to_requested_venue_ids(self):
        self.auth(self.owner)
        response = self.client.get(
            self.url, {"supplier_product_id": str(self.new_sp.id), "venue_id": str(self.venue_a.id)}
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()
        self.assertEqual([row["venue_id"] for row in rows], [str(self.venue_a.id)])

    def test_preview_rejects_venue_not_linked(self):
        other_venue = Venue.objects.create(display_name="Cafe Q")
        self.auth(self.owner)
        response = self.client.get(
            self.url, {"supplier_product_id": str(self.new_sp.id), "venue_id": str(other_venue.id)}
        )
        self.assertEqual(response.status_code, 400)

    def test_apply_updates_all_linked_venues_by_default(self):
        self.auth(self.owner)
        response = self.client.post(self.url, {"supplier_product_id": str(self.new_sp.id)}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_a.supplier_product_id, self.new_sp.id)
        self.assertEqual(self.item_b.supplier_product_id, self.new_sp.id)

    def test_apply_restricts_to_requested_venue_ids(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"supplier_product_id": str(self.new_sp.id), "venue_ids": [str(self.venue_a.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_a.supplier_product_id, self.new_sp.id)
        self.assertIsNone(self.item_b.supplier_product_id)

    def test_apply_rejects_venue_not_linked(self):
        other_venue = Venue.objects.create(display_name="Cafe R")
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"supplier_product_id": str(self.new_sp.id), "venue_ids": [str(other_venue.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_denied_for_stranger(self):
        stranger = User.objects.create(email="stranger6@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
        self.auth(stranger)
        response = self.client.get(self.url, {"supplier_product_id": str(self.new_sp.id)})
        self.assertEqual(response.status_code, 403)

    def test_rejects_archived_supplier_product(self):
        self.new_sp.status = EntityStatus.ARCHIVED
        self.new_sp.save(update_fields=["status", "updated_at"])
        self.auth(self.owner)
        response = self.client.get(self.url, {"supplier_product_id": str(self.new_sp.id)})
        self.assertEqual(response.status_code, 400)


class TenderItemOffersAPITests(APITestCase):
    """Raw ТЗ §20-22: the buyer's "Предложения по товару" table, one row
    per supplier's current offer on this position."""

    def setUp(self):
        self.owner = User.objects.create(email="lo-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(email="lo-stranger@example.com", full_name="X", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Offers")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        unit = Unit.objects.create(code="kg", name="кг")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Камамбер2", unit=unit)
        self.tender = Tender.objects.create(created_by=self.owner, name="Сыры2", access=TenderAccess.OPEN)
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.venue])

        self.supplier = Supplier.objects.create(legal_name="Cheese LLC", display_name="Cheese Co")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Камамбер фр.", normalized_name="камамбер фр"
        )
        self.offer = TenderOffer.objects.create(
            tender_item=self.item, supplier=self.supplier, supplier_product=self.supplier_product, price="500.00"
        )
        self.offer.venues.set([self.venue])

        self.url = f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/offers"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_list_offers(self):
        self.auth(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["supplier_id"], str(self.supplier.id))
        self.assertEqual(data[0]["price"], "500.00")

    def test_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)


class TenderCrossTenderScopingAPITests(APITestCase):
    """Final-review Finding 3a — the one the reviewer said they'd block
    merge on. HasTenderAccess is keyed on tender_id alone; several nested
    endpoints then resolve item_id/offer_id scoped to that same tender_id
    (TenderItemOffersView.get: get_object_or_404(TenderItem, pk=item_id,
    tender_id=tender_id); TenderDistributionView._get_offer:
    get_object_or_404(TenderOffer, pk=offer_id, tender_item_id=item_id,
    tender_item__tender_id=tender_id)). If that scoping were ever wrong, a
    buyer who owns tender A could read/act on tender B's item/offer just by
    knowing its id and swapping it into tender A's URL. This locks in that
    a real item/offer id belonging to ANOTHER tender the caller does have
    access to (their "mine" tender) 404s rather than leaking the "theirs"
    tender's data."""

    def setUp(self):
        self.mine_owner = User.objects.create(
            email="scope-mine@example.com", full_name="Mine", role=UserRole.VENUE_USER
        )
        self.their_owner = User.objects.create(
            email="scope-theirs@example.com", full_name="Theirs", role=UserRole.VENUE_USER
        )
        self.mine_tender = Tender.objects.create(created_by=self.mine_owner, name="Моё", access=TenderAccess.OPEN)
        self.their_tender = Tender.objects.create(created_by=self.their_owner, name="Чужое", access=TenderAccess.OPEN)
        their_master_product = MasterProduct.objects.create(owner=self.their_owner, canonical_name="Чужой товар")
        self.their_item = TenderItem.objects.create(tender=self.their_tender, master_product=their_master_product)
        their_supplier = Supplier.objects.create(legal_name="Their LLC", display_name="Their Co")
        self.their_offer = TenderOffer.objects.create(
            tender_item=self.their_item, supplier=their_supplier, price="100.00"
        )

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_item_offers_list_404_for_item_from_other_tender(self):
        self.auth(self.mine_owner)
        url = f"/api/v1/catalog/tenders/{self.mine_tender.id}/items/{self.their_item.id}/offers"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404, response.content)

    def test_distribution_preview_404_for_item_offer_from_other_tender(self):
        self.auth(self.mine_owner)
        url = (
            f"/api/v1/catalog/tenders/{self.mine_tender.id}/items/{self.their_item.id}"
            f"/offers/{self.their_offer.id}/distribution"
        )
        response = self.client.get(url, {"venue_id": str(uuid4())})
        self.assertEqual(response.status_code, 404, response.content)


class MasterProductTenderOfferServiceTests(TestCase):
    """Raw ТЗ §45-47 / spec §3.6 п.3: applying a winning tender offer must
    assign supplier_product AND fix an individual price, together, only for
    the explicitly selected venues."""

    def setUp(self):
        self.owner = User.objects.create(email="owner_tender1@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue_a = Venue.objects.create(display_name="Cafe TA")
        self.venue_b = Venue.objects.create(display_name="Cafe TB")
        for venue in (self.venue_a, self.venue_b):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.mp = create_master_product(owner=self.owner, canonical_name="Сливки 33%")
        self.item_a = link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)
        self.item_b = link_master_product_to_venue(master_product=self.mp, venue=self.venue_b)
        self.supplier = Supplier.objects.create(display_name="Цефей")
        self.old_sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Петмол 33%",
            normalized_name="Петмол 33%",
            current_price=Decimal("298.00"),
        )
        self.winning_sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Parmalat 33%",
            normalized_name="Parmalat 33%",
            current_price=Decimal("310.00"),
        )
        VenueProductMatrixItem.objects.filter(pk=self.item_a.pk).update(supplier_product=self.old_sp)

    def test_diff_includes_current_and_new_price(self):
        items = master_product_bulk_supplier_product_targets(master_product=self.mp)
        rows = {
            row.venue_id: row
            for row in master_product_tender_offer_diff(
                items=items, new_supplier_product=self.winning_sp, new_price=Decimal("305.00"), new_currency="RUB"
            )
        }
        self.assertEqual(rows[self.venue_a.id].current_price, Decimal("298.00"))
        self.assertEqual(rows[self.venue_a.id].current_price_currency, "RUB")
        self.assertEqual(rows[self.venue_a.id].new_price, Decimal("305.00"))
        self.assertEqual(rows[self.venue_a.id].new_price_currency, "RUB")

    def test_diff_current_price_is_null_when_no_current_supplier_product(self):
        items = master_product_bulk_supplier_product_targets(master_product=self.mp)
        rows = {
            row.venue_id: row
            for row in master_product_tender_offer_diff(
                items=items, new_supplier_product=self.winning_sp, new_price=Decimal("305.00"), new_currency="RUB"
            )
        }
        self.assertIsNone(rows[self.venue_b.id].current_price)
        self.assertIsNone(rows[self.venue_b.id].current_price_currency)

    def test_apply_assigns_supplier_product_and_creates_individual_price(self):
        apply_tender_offer(
            master_product=self.mp,
            supplier_product=self.winning_sp,
            price=Decimal("305.00"),
            currency="RUB",
            venue_ids=[self.venue_a.id, self.venue_b.id],
            source_reference="Тендер №42",
            created_by=self.owner,
        )
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_a.supplier_product_id, self.winning_sp.id)
        self.assertEqual(self.item_b.supplier_product_id, self.winning_sp.id)

        price_a, currency_a, _price_a_id = resolve_supplier_product_price(
            supplier_product=self.winning_sp, venue_id=self.venue_a.id
        )
        self.assertEqual(price_a, Decimal("305.00"))
        self.assertEqual(currency_a, "RUB")

        rows = list(SupplierProductVenuePrice.objects.filter(supplier_product=self.winning_sp))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.venue_id for row in rows}, {self.venue_a.id, self.venue_b.id})
        self.assertEqual(len({row.group_id for row in rows}), 1)
        for row in rows:
            self.assertEqual(row.source_type, SupplierProductVenuePriceSourceType.TENDER)
            self.assertEqual(row.source_reference, "Тендер №42")

    def test_apply_restricts_to_requested_venue_ids(self):
        apply_tender_offer(
            master_product=self.mp,
            supplier_product=self.winning_sp,
            price=Decimal("305.00"),
            currency="RUB",
            venue_ids=[self.venue_a.id],
            source_reference="Тендер №42",
            created_by=self.owner,
        )
        self.item_b.refresh_from_db()
        self.assertIsNone(self.item_b.supplier_product_id)
        price_b, _, _ = resolve_supplier_product_price(supplier_product=self.winning_sp, venue_id=self.venue_b.id)
        self.assertEqual(price_b, self.winning_sp.current_price)

    def test_apply_requires_at_least_one_venue_id(self):
        with self.assertRaises(ValidationError):
            apply_tender_offer(
                master_product=self.mp,
                supplier_product=self.winning_sp,
                price=Decimal("305.00"),
                currency="RUB",
                venue_ids=[],
                source_reference="Тендер №42",
                created_by=self.owner,
            )

    def test_apply_rejects_unlinked_venue_id_without_writing_anything(self):
        other_venue = Venue.objects.create(display_name="Cafe TC")
        with self.assertRaises(ValidationError):
            apply_tender_offer(
                master_product=self.mp,
                supplier_product=self.winning_sp,
                price=Decimal("305.00"),
                currency="RUB",
                venue_ids=[self.venue_a.id, other_venue.id],
                source_reference="Тендер №42",
                created_by=self.owner,
            )
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.supplier_product_id, self.old_sp.id)
        self.assertFalse(SupplierProductVenuePrice.objects.filter(supplier_product=self.winning_sp).exists())


class MasterProductTenderOfferAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner_tender2@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue_a = Venue.objects.create(display_name="Cafe TD")
        self.venue_b = Venue.objects.create(display_name="Cafe TE")
        for venue in (self.venue_a, self.venue_b):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.mp = create_master_product(owner=self.owner, canonical_name="Лосось")
        self.item_a = link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)
        self.item_b = link_master_product_to_venue(master_product=self.mp, venue=self.venue_b)
        self.supplier = Supplier.objects.create(display_name="Восток-Запад")
        self.winning_sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Лосось охлажденный",
            normalized_name="Лосось охлажденный",
            current_price=Decimal("1200.00"),
        )
        self.url = f"/api/v1/catalog/master-products/{self.mp.id}/tender-offer"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_preview_does_not_write(self):
        self.auth(self.owner)
        response = self.client.get(
            self.url,
            {
                "supplier_product_id": str(self.winning_sp.id),
                "price": "1150.00",
                "venue_id": [str(self.venue_a.id), str(self.venue_b.id)],
                "source_reference": "Тендер №7",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = {row["venue_id"]: row for row in response.json()}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[str(self.venue_a.id)]["new_price"], "1150.00")
        self.item_a.refresh_from_db()
        self.assertIsNone(self.item_a.supplier_product_id)
        self.assertFalse(SupplierProductVenuePrice.objects.filter(supplier_product=self.winning_sp).exists())

    def test_preview_requires_venue_ids(self):
        self.auth(self.owner)
        response = self.client.get(
            self.url,
            {"supplier_product_id": str(self.winning_sp.id), "price": "1150.00", "source_reference": "Тендер №7"},
        )
        self.assertEqual(response.status_code, 400)

    def test_apply_assigns_supplier_product_and_creates_price(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {
                "supplier_product_id": str(self.winning_sp.id),
                "price": "1150.00",
                "venue_ids": [str(self.venue_a.id), str(self.venue_b.id)],
                "source_reference": "Тендер №7",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_a.supplier_product_id, self.winning_sp.id)
        self.assertEqual(self.item_b.supplier_product_id, self.winning_sp.id)
        rows = list(SupplierProductVenuePrice.objects.filter(supplier_product=self.winning_sp))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.source_type, SupplierProductVenuePriceSourceType.TENDER)
            self.assertEqual(row.source_reference, "Тендер №7")

    def test_apply_rejects_venue_not_linked(self):
        other_venue = Venue.objects.create(display_name="Cafe TF")
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {
                "supplier_product_id": str(self.winning_sp.id),
                "price": "1150.00",
                "venue_ids": [str(other_venue.id)],
                "source_reference": "Тендер №7",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_apply_requires_source_reference(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {
                "supplier_product_id": str(self.winning_sp.id),
                "price": "1150.00",
                "venue_ids": [str(self.venue_a.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_denied_for_stranger(self):
        stranger = User.objects.create(
            email="stranger_tender@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.auth(stranger)
        response = self.client.get(
            self.url,
            {
                "supplier_product_id": str(self.winning_sp.id),
                "price": "1150.00",
                "venue_id": [str(self.venue_a.id)],
                "source_reference": "Тендер №7",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_rejects_archived_supplier_product(self):
        self.winning_sp.status = EntityStatus.ARCHIVED
        self.winning_sp.save(update_fields=["status", "updated_at"])
        self.auth(self.owner)
        response = self.client.get(
            self.url,
            {
                "supplier_product_id": str(self.winning_sp.id),
                "price": "1150.00",
                "venue_id": [str(self.venue_a.id)],
                "source_reference": "Тендер №7",
            },
        )
        self.assertEqual(response.status_code, 400)


class SupplierProductVenuePriceModelTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(display_name="Поставщик А")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Мука в/с 25кг", normalized_name="Мука в/с 25кг"
        )
        self.venue = Venue.objects.create(display_name="Cafe H")
        self.owner = User.objects.create(email="spvp_owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)

    def test_create_and_defaults(self):
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("1500.00"), created_by=self.owner
        )
        self.assertEqual(price.currency, "RUB")
        self.assertEqual(price.source_type, SupplierProductVenuePriceSourceType.MANUAL)
        self.assertEqual(price.status, SupplierProductVenuePriceStatus.ACTIVE)
        self.assertIsNone(price.valid_to)
        self.assertIsNotNone(price.group_id)
        self.assertIn(price, self.venue.supplier_product_prices.all())

    def test_supplier_product_delete_restricted_while_venue_price_exists(self):
        """2026-09-23: on_delete вернули RESTRICT (backlog#task-69, отмена
        решения от 2026-08-11) — индивидуальная цена не должна безвозвратно
        пропадать вместе с товаром."""
        SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("1500.00"), created_by=self.owner
        )
        with self.assertRaises(RestrictedError):
            self.supplier_product.delete()

    def test_venue_delete_cascades(self):
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("1500.00"), created_by=self.owner
        )
        self.venue.delete()
        self.assertFalse(SupplierProductVenuePrice.objects.filter(pk=price.pk).exists())

    def test_created_by_delete_sets_null(self):
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("1500.00"), created_by=self.owner
        )
        self.owner.delete()
        price.refresh_from_db()
        self.assertIsNone(price.created_by)

    def test_negative_price_rejected(self):
        with self.assertRaises(IntegrityError):
            SupplierProductVenuePrice.objects.create(
                supplier_product=self.supplier_product, venue=self.venue, price=Decimal("-1.00"), created_by=self.owner
            )

    def test_valid_to_before_valid_from_rejected(self):
        now = timezone.now()
        with self.assertRaises(IntegrityError):
            SupplierProductVenuePrice.objects.create(
                supplier_product=self.supplier_product,
                venue=self.venue,
                price=Decimal("100.00"),
                created_by=self.owner,
                valid_from=now,
                valid_to=now - timedelta(days=1),
            )

    def test_source_tender_offer_nullable_fk(self):
        tender = Tender.objects.create(created_by=self.owner, name="Тендер на молочку", access=TenderAccess.OPEN)
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%")
        tender_item = TenderItem.objects.create(tender=tender, master_product=master_product)
        offer = TenderOffer.objects.create(
            tender_item=tender_item,
            supplier=self.supplier,
            supplier_product=self.supplier_product,
            price=Decimal("305.00"),
        )

        price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("305.00"),
            created_by=self.owner,
            source_tender_offer=offer,
        )

        self.assertEqual(price.source_tender_offer_id, offer.id)

        offer.delete()
        price.refresh_from_db()
        self.assertIsNone(price.source_tender_offer_id)


class ResolveSupplierProductPriceTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(display_name="Поставщик Б")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Сахар песок 50кг",
            normalized_name="Сахар песок 50кг",
            current_price=Decimal("2000.00"),
        )
        self.venue = Venue.objects.create(display_name="Cafe I")
        self.other_venue = Venue.objects.create(display_name="Cafe J")
        self.owner = User.objects.create(email="resolve_owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)

    def test_falls_back_to_base_price_when_no_individual_price(self):
        price, currency, individual_id = resolve_supplier_product_price(
            supplier_product=self.supplier_product, venue_id=self.venue.id
        )
        self.assertEqual(price, Decimal("2000.00"))
        self.assertEqual(currency, "RUB")
        self.assertIsNone(individual_id)

    def test_uses_active_individual_price_for_covered_venue(self):
        row = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("1800.00"),
            currency="RUB",
            created_by=self.owner,
        )

        price, currency, individual_id = resolve_supplier_product_price(
            supplier_product=self.supplier_product, venue_id=self.venue.id
        )
        self.assertEqual(price, Decimal("1800.00"))
        self.assertEqual(currency, "RUB")
        self.assertEqual(individual_id, row.id)

        other_price, _, other_individual_id = resolve_supplier_product_price(
            supplier_product=self.supplier_product, venue_id=self.other_venue.id
        )
        self.assertEqual(other_price, Decimal("2000.00"))
        self.assertIsNone(other_individual_id)

    def test_ignores_archived_individual_price(self):
        SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("1800.00"),
            created_by=self.owner,
            status=SupplierProductVenuePriceStatus.ARCHIVED,
        )
        price, _, individual_id = resolve_supplier_product_price(
            supplier_product=self.supplier_product, venue_id=self.venue.id
        )
        self.assertEqual(price, Decimal("2000.00"))
        self.assertIsNone(individual_id)

    def test_ignores_future_and_expired_windows(self):
        now = timezone.now()
        SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("1700.00"),
            created_by=self.owner,
            valid_from=now + timedelta(days=1),
        )
        SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("1600.00"),
            created_by=self.owner,
            valid_from=now - timedelta(days=10),
            valid_to=now - timedelta(days=1),
        )

        price, _, _ = resolve_supplier_product_price(supplier_product=self.supplier_product, venue_id=self.venue.id)
        self.assertEqual(price, Decimal("2000.00"))

    def test_history_queryset_includes_active_and_archived_ordered_newest_first(self):
        older = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("1900.00"),
            created_by=self.owner,
            status=SupplierProductVenuePriceStatus.ARCHIVED,
            valid_from=timezone.now() - timedelta(days=5),
        )
        newer = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("1800.00"), created_by=self.owner
        )
        SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.other_venue,
            price=Decimal("1750.00"),
            created_by=self.owner,
        )

        rows = list(
            supplier_product_venue_price_history_queryset(
                supplier_product_id=self.supplier_product.id, venue_id=self.venue.id
            )
        )
        self.assertEqual(rows, [newer, older])


class SupplierProductVenuePriceServicesTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(display_name="Поставщик В")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Масло сливочное 82.5%",
            normalized_name="Масло сливочное 82.5%",
            current_price=Decimal("900.00"),
        )
        self.venue = Venue.objects.create(display_name="Cafe K")
        self.other_venue = Venue.objects.create(display_name="Cafe L")
        self.owner = User.objects.create(
            email="services_owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )

    def test_create_sets_active_price(self):
        rows = set_supplier_product_venue_price(
            supplier_product=self.supplier_product, venues=[self.venue], price=Decimal("850.00"), created_by=self.owner
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, SupplierProductVenuePriceStatus.ACTIVE)
        self.assertEqual(row.currency, "RUB")
        self.assertEqual(row.venue_id, self.venue.id)

    def test_create_for_several_venues_shares_one_group_id(self):
        rows = set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue, self.other_venue],
            price=Decimal("850.00"),
            created_by=self.owner,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.venue_id for row in rows}, {self.venue.id, self.other_venue.id})
        self.assertEqual(len({row.group_id for row in rows}), 1)

    def test_valid_to_before_valid_from_rejected(self):
        now = timezone.now()
        with self.assertRaises(ValidationError):
            set_supplier_product_venue_price(
                supplier_product=self.supplier_product,
                venues=[self.venue],
                price=Decimal("850.00"),
                created_by=self.owner,
                valid_from=now,
                valid_to=now - timedelta(days=1),
            )

    def test_new_active_price_supersedes_previous_overlapping_one_for_same_venue(self):
        (first,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product, venues=[self.venue], price=Decimal("850.00"), created_by=self.owner
        )
        (second,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("800.00"),
            created_by=self.owner,
            confirm_replace=True,
        )
        first.refresh_from_db()
        self.assertEqual(first.status, SupplierProductVenuePriceStatus.ARCHIVED)
        self.assertIsNotNone(first.valid_to)
        self.assertEqual(second.status, SupplierProductVenuePriceStatus.ACTIVE)

        price, _, _ = resolve_supplier_product_price(supplier_product=self.supplier_product, venue_id=self.venue.id)
        self.assertEqual(price, Decimal("800.00"))

    def test_conflict_raised_without_confirm_replace(self):
        set_supplier_product_venue_price(
            supplier_product=self.supplier_product, venues=[self.venue], price=Decimal("850.00"), created_by=self.owner
        )
        with self.assertRaises(SupplierProductVenuePriceConflict) as ctx:
            set_supplier_product_venue_price(
                supplier_product=self.supplier_product,
                venues=[self.venue],
                price=Decimal("800.00"),
                created_by=self.owner,
            )
        self.assertEqual(len(ctx.exception.conflicts), 1)
        self.assertEqual(ctx.exception.conflicts[0].venue_id, self.venue.id)
        # Nothing written — still exactly the one original ACTIVE row.
        self.assertEqual(
            SupplierProductVenuePrice.objects.filter(
                supplier_product=self.supplier_product, status=SupplierProductVenuePriceStatus.ACTIVE
            ).count(),
            1,
        )

    def test_conflict_only_lists_actually_colliding_venues(self):
        set_supplier_product_venue_price(
            supplier_product=self.supplier_product, venues=[self.venue], price=Decimal("850.00"), created_by=self.owner
        )
        with self.assertRaises(SupplierProductVenuePriceConflict) as ctx:
            set_supplier_product_venue_price(
                supplier_product=self.supplier_product,
                venues=[self.venue, self.other_venue],
                price=Decimal("800.00"),
                created_by=self.owner,
            )
        self.assertEqual([c.venue_id for c in ctx.exception.conflicts], [self.venue.id])

    def test_new_price_for_different_venue_does_not_supersede(self):
        (first,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product, venues=[self.venue], price=Decimal("850.00"), created_by=self.owner
        )
        set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.other_venue],
            price=Decimal("800.00"),
            created_by=self.owner,
        )
        first.refresh_from_db()
        self.assertEqual(first.status, SupplierProductVenuePriceStatus.ACTIVE)

    def test_new_price_does_not_supersede_non_overlapping_future_window(self):
        now = timezone.now()
        (first,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("850.00"),
            created_by=self.owner,
            valid_from=now,
            valid_to=now + timedelta(days=10),
        )
        set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("800.00"),
            created_by=self.owner,
            valid_from=now + timedelta(days=20),
        )
        first.refresh_from_db()
        self.assertEqual(first.status, SupplierProductVenuePriceStatus.ACTIVE)

    def test_changing_one_venue_of_a_group_does_not_affect_the_other(self):
        # The exact scenario ТЗ §12 asks for — a batch-assigned price to
        # several venues must be independently replaceable per venue,
        # unlike the old M2M model's documented "collateral damage" bug.
        first_venue, first_other = set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue, self.other_venue],
            price=Decimal("850.00"),
            created_by=self.owner,
        )
        set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("800.00"),
            created_by=self.owner,
            confirm_replace=True,
        )
        first_venue.refresh_from_db()
        first_other.refresh_from_db()
        self.assertEqual(first_venue.status, SupplierProductVenuePriceStatus.ARCHIVED)
        self.assertEqual(first_other.status, SupplierProductVenuePriceStatus.ACTIVE)
        other_price, _, _ = resolve_supplier_product_price(
            supplier_product=self.supplier_product, venue_id=self.other_venue.id
        )
        self.assertEqual(other_price, Decimal("850.00"))

    def test_archive_sets_archived_and_valid_to(self):
        (row,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product, venues=[self.venue], price=Decimal("850.00"), created_by=self.owner
        )
        archived = archive_supplier_product_venue_price(row)
        self.assertEqual(archived.status, SupplierProductVenuePriceStatus.ARCHIVED)
        self.assertIsNotNone(archived.valid_to)

    def test_archive_already_archived_rejected(self):
        (row,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product, venues=[self.venue], price=Decimal("850.00"), created_by=self.owner
        )
        archive_supplier_product_venue_price(row)
        with self.assertRaises(ValidationError):
            archive_supplier_product_venue_price(row)

    # --- 2026-09-01 final-review fix wave, C2: valid_to must never violate
    # the sp_venue_prices_period CheckConstraint (valid_to > valid_from) ---

    def test_superseding_future_dated_row_does_not_violate_period_constraint(self):
        now = timezone.now()
        (future,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("700.00"),
            created_by=self.owner,
            valid_from=now + timedelta(days=5),
        )
        (current,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("800.00"),
            created_by=self.owner,
            confirm_replace=True,
        )
        future.refresh_from_db()
        self.assertEqual(future.status, SupplierProductVenuePriceStatus.ARCHIVED)
        # valid_to may only be set to something strictly after valid_from —
        # for a row that starts in the future there is no such "truncation"
        # point at `now`, so it must be left alone.
        self.assertTrue(future.valid_to is None or future.valid_to > future.valid_from)
        self.assertEqual(current.status, SupplierProductVenuePriceStatus.ACTIVE)
        price, _, _ = resolve_supplier_product_price(supplier_product=self.supplier_product, venue_id=self.venue.id)
        self.assertEqual(price, Decimal("800.00"))
        # Only one ACTIVE row covers the venue afterwards.
        self.assertEqual(
            SupplierProductVenuePrice.objects.filter(
                supplier_product=self.supplier_product,
                venue_id=self.venue.id,
                status=SupplierProductVenuePriceStatus.ACTIVE,
            ).count(),
            1,
        )

    def test_two_creates_with_identical_valid_from_do_not_violate_period_constraint(self):
        moment = timezone.now() - timedelta(minutes=1)
        (first,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("850.00"),
            created_by=self.owner,
            valid_from=moment,
        )
        (second,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("800.00"),
            created_by=self.owner,
            valid_from=moment,
            confirm_replace=True,
        )
        first.refresh_from_db()
        self.assertEqual(first.status, SupplierProductVenuePriceStatus.ARCHIVED)
        self.assertTrue(first.valid_to is None or first.valid_to > first.valid_from)
        self.assertEqual(second.status, SupplierProductVenuePriceStatus.ACTIVE)
        price, _, _ = resolve_supplier_product_price(supplier_product=self.supplier_product, venue_id=self.venue.id)
        self.assertEqual(price, Decimal("800.00"))

    def test_archiving_future_dated_price_does_not_violate_period_constraint(self):
        (row,) = set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("850.00"),
            created_by=self.owner,
            valid_from=timezone.now() + timedelta(days=3),
        )
        archived = archive_supplier_product_venue_price(row)
        self.assertEqual(archived.status, SupplierProductVenuePriceStatus.ARCHIVED)
        self.assertTrue(archived.valid_to is None or archived.valid_to > archived.valid_from)
        archived.refresh_from_db()
        self.assertEqual(archived.status, SupplierProductVenuePriceStatus.ARCHIVED)

    # --- Задача 3 (§29 ТЗ): редактирование/удаление ещё не наступившей ручной цены ---

    def test_update_future_manual_price_edits_fields(self):
        future_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("300.00"),
            valid_from=timezone.now() + timedelta(days=3),
            created_by=self.owner,
        )
        updated = update_supplier_product_venue_price(
            future_price, price_value=Decimal("320.00"), comment="Скорректировано перед стартом"
        )
        self.assertEqual(updated.price, Decimal("320.00"))
        self.assertEqual(updated.comment, "Скорректировано перед стартом")

    def test_update_rejects_already_active_price(self):
        active_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("300.00"), created_by=self.owner
        )
        with self.assertRaises(ValidationError):
            update_supplier_product_venue_price(active_price, price_value=Decimal("320.00"))

    def test_update_rejects_tender_sourced_price(self):
        future_tender_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("300.00"),
            valid_from=timezone.now() + timedelta(days=3),
            source_type=SupplierProductVenuePriceSourceType.TENDER,
            created_by=self.owner,
        )
        with self.assertRaises(ValidationError):
            update_supplier_product_venue_price(future_tender_price, price_value=Decimal("320.00"))

    def test_delete_future_manual_price_removes_row(self):
        future_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("300.00"),
            valid_from=timezone.now() + timedelta(days=3),
            created_by=self.owner,
        )
        delete_future_supplier_product_venue_price(future_price)
        self.assertFalse(SupplierProductVenuePrice.objects.filter(pk=future_price.pk).exists())

    def test_delete_rejects_already_active_price(self):
        active_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("300.00"), created_by=self.owner
        )
        with self.assertRaises(ValidationError):
            delete_future_supplier_product_venue_price(active_price)

    # --- 2026-09-21 final-review fix wave, Fix 1: PATCH used to bypass the
    # period-conflict detection the create path enforces ---

    def test_update_rejects_valid_from_moved_into_the_past(self):
        future_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("300.00"),
            valid_from=timezone.now() + timedelta(days=3),
            created_by=self.owner,
        )
        original_valid_from = future_price.valid_from
        with self.assertRaises(ValidationError):
            update_supplier_product_venue_price(future_price, valid_from=timezone.now() - timedelta(days=5))
        future_price.refresh_from_db()
        self.assertEqual(future_price.valid_from, original_valid_from)

    def test_update_rejects_period_that_conflicts_with_another_active_price(self):
        now = timezone.now()
        active_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("850.00"),
            valid_from=now - timedelta(days=1),
            created_by=self.owner,
        )
        future_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("300.00"),
            valid_from=now + timedelta(days=10),
            created_by=self.owner,
        )
        original_valid_from = future_price.valid_from
        with self.assertRaises(SupplierProductVenuePriceConflict) as ctx:
            # Still in the future (passes the Fix-1-#1 guard) but now
            # overlaps active_price's own (unbounded) window.
            update_supplier_product_venue_price(future_price, valid_from=now + timedelta(hours=1))
        self.assertEqual(ctx.exception.conflicts[0].id, active_price.id)
        future_price.refresh_from_db()
        self.assertEqual(future_price.valid_from, original_valid_from)
        self.assertEqual(future_price.price, Decimal("300.00"))

    def test_update_does_not_conflict_with_itself(self):
        # A future price is itself ACTIVE and already owns the venue slot —
        # re-saving it (e.g. only changing the price) must not trip the new
        # conflict check against its own unchanged period.
        future_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("300.00"),
            valid_from=timezone.now() + timedelta(days=3),
            created_by=self.owner,
        )
        updated = update_supplier_product_venue_price(future_price, price_value=Decimal("310.00"))
        self.assertEqual(updated.price, Decimal("310.00"))

    # --- Fix 2: PATCH silently ignored explicit null/blank clears ---

    def test_update_clears_valid_to_when_explicitly_set_to_null(self):
        future_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("300.00"),
            valid_from=timezone.now() + timedelta(days=3),
            valid_to=timezone.now() + timedelta(days=30),
            created_by=self.owner,
        )
        updated = update_supplier_product_venue_price(future_price, valid_to=None)
        self.assertIsNone(updated.valid_to)
        future_price.refresh_from_db()
        self.assertIsNone(future_price.valid_to)

    def test_update_normalizes_blank_comment_to_null(self):
        future_price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("300.00"),
            valid_from=timezone.now() + timedelta(days=3),
            comment="старый комментарий",
            created_by=self.owner,
        )
        updated = update_supplier_product_venue_price(future_price, comment="")
        self.assertIsNone(updated.comment)


class SupplierProductVenuePricePermissionTests(TestCase):
    """2026-09-07: writing an individual price moved to the supplier cabinet
    (IsSupplierCabinetActiveMember, tested in api.v1.suppliers.tests) — these
    two permission classes now only ever gate read access for the venue
    side (list a venue's own prices / read one price's audit log)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.supplier = Supplier.objects.create(display_name="Поставщик Г")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Соль экстра", normalized_name="Соль экстра"
        )
        self.manager_user = User.objects.create(
            email="spvp_mgr@example.com", full_name="Manager", role=UserRole.VENUE_USER
        )
        self.stranger = User.objects.create(
            email="spvp_stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe M")
        VenueUser.objects.create(
            venue=self.venue, user=self.manager_user, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.price = SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("100.00"),
            created_by=self.manager_user,
        )

    def _request(self, user, query_params=None):
        request = self.factory.get("/", query_params or {})
        request.user = user
        request.query_params = request.GET
        return request

    def test_list_allowed_for_venue_manager(self):
        request = self._request(self.manager_user, query_params={"venue_id": str(self.venue.id)})
        self.assertTrue(HasSupplierProductVenuePriceAccess().has_permission(request, None))

    def test_list_denied_for_stranger(self):
        request = self._request(self.stranger, query_params={"venue_id": str(self.venue.id)})
        self.assertFalse(HasSupplierProductVenuePriceAccess().has_permission(request, None))

    def test_list_denied_without_venue_id(self):
        request = self._request(self.manager_user)
        self.assertFalse(HasSupplierProductVenuePriceAccess().has_permission(request, None))

    def test_audit_log_permission_resolves_venue_from_object(self):
        view = SimpleNamespace(kwargs={"price_id": str(self.price.id)})
        self.assertTrue(
            HasSupplierProductVenuePriceObjectAccess().has_permission(self._request(self.manager_user), view)
        )
        self.assertFalse(HasSupplierProductVenuePriceObjectAccess().has_permission(self._request(self.stranger), view))

    def test_audit_log_permission_denied_for_nonexistent_price(self):
        view = SimpleNamespace(kwargs={"price_id": str(uuid4())})
        request = self._request(self.manager_user)
        self.assertFalse(HasSupplierProductVenuePriceObjectAccess().has_permission(request, view))

    # --- 2026-09-01 final-review fix wave, C3: malformed input must never
    # reach the ORM from a permission class (uncaught 500) ---

    def test_audit_log_permission_denied_for_malformed_price_id(self):
        request = self._request(self.manager_user)
        for price_id in ("abc", "", None, 5, ["x"]):
            with self.subTest(price_id=price_id):
                view = SimpleNamespace(kwargs={"price_id": price_id})
                self.assertFalse(HasSupplierProductVenuePriceObjectAccess().has_permission(request, view))

    def test_list_defers_on_malformed_venue_id(self):
        # Malformed (but present) venue_id is a request-shape problem the
        # query serializer already 400s — defer instead of crashing, exactly
        # like HasMasterCatalogAccessToOwner does for owner_id.
        request = self._request(self.stranger, query_params={"venue_id": "abc"})
        self.assertTrue(HasSupplierProductVenuePriceAccess().has_permission(request, None))


class SupplierProductVenuePriceListAPITests(APITestCase):
    """Read-only — see api.v1.suppliers.tests for create/archive, which
    moved to the supplier cabinet (2026-09-07)."""

    def setUp(self):
        self.supplier = Supplier.objects.create(display_name="Поставщик Д")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Яйцо С1",
            normalized_name="Яйцо С1",
            current_price=Decimal("120.00"),
        )
        self.manager_user = User.objects.create(
            email="spvp_api_mgr@example.com", full_name="Manager", role=UserRole.VENUE_USER
        )
        self.stranger = User.objects.create(
            email="spvp_api_stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe P")
        VenueUser.objects.create(
            venue=self.venue, user=self.manager_user, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.url = "/api/v1/catalog/supplier-product-venue-prices"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_list_returns_rows_for_venue(self):
        set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("110.00"),
            created_by=self.manager_user,
        )
        self.auth(self.manager_user)
        response = self.client.get(
            self.url, {"supplier_product_id": str(self.supplier_product.id), "venue_id": str(self.venue.id)}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)
        results = data["results"]
        self.assertEqual(results[0]["price"], "110.00")
        self.assertEqual(results[0]["venue_id"], str(self.venue.id))
        self.assertEqual(results[0]["price_state"], "active")

    def test_list_includes_tender_source_fields(self):
        tender = Tender.objects.create(created_by=self.manager_user, name="Тендер на яйца", access=TenderAccess.OPEN)
        unit = Unit.objects.create(code="pcs-spvp", name="шт")
        master_product = MasterProduct.objects.create(owner=self.manager_user, canonical_name="Яйцо С1 MP", unit=unit)
        tender_item = TenderItem.objects.create(tender=tender, master_product=master_product)
        offer = TenderOffer.objects.create(
            tender_item=tender_item,
            supplier=self.supplier,
            supplier_product=self.supplier_product,
            price=Decimal("110.00"),
        )
        set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("110.00"),
            created_by=self.manager_user,
            source_type=SupplierProductVenuePriceSourceType.TENDER,
            source_tender_offer=offer,
        )
        self.auth(self.manager_user)
        response = self.client.get(
            self.url, {"supplier_product_id": str(self.supplier_product.id), "venue_id": str(self.venue.id)}
        )
        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["source_tender_offer_id"], str(offer.id))
        self.assertEqual(row["source_tender_id"], str(tender.id))
        self.assertEqual(row["source_tender_item_id"], str(tender_item.id))

    def test_list_source_tender_fields_null_for_manual_price(self):
        set_supplier_product_venue_price(
            supplier_product=self.supplier_product,
            venues=[self.venue],
            price=Decimal("110.00"),
            created_by=self.manager_user,
        )
        self.auth(self.manager_user)
        response = self.client.get(
            self.url, {"supplier_product_id": str(self.supplier_product.id), "venue_id": str(self.venue.id)}
        )
        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertIsNone(row["source_tender_offer_id"])
        self.assertIsNone(row["source_tender_id"])
        self.assertIsNone(row["source_tender_item_id"])

    def test_list_denied_without_venue_id(self):
        self.auth(self.manager_user)
        response = self.client.get(self.url, {"supplier_product_id": str(self.supplier_product.id)})
        self.assertEqual(response.status_code, 403)

    def test_list_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.get(
            self.url, {"supplier_product_id": str(self.supplier_product.id), "venue_id": str(self.venue.id)}
        )
        self.assertEqual(response.status_code, 403)

    def test_list_requires_authentication(self):
        response = self.client.get(
            self.url, {"supplier_product_id": str(self.supplier_product.id), "venue_id": str(self.venue.id)}
        )
        self.assertEqual(response.status_code, 401)

    def test_list_with_malformed_venue_id_returns_400(self):
        self.auth(self.manager_user)
        response = self.client.get(self.url, {"supplier_product_id": str(self.supplier_product.id), "venue_id": "abc"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_post_no_longer_allowed_here(self):
        # 2026-09-07: creating a price moved to the supplier cabinet
        # (POST /api/v1/suppliers/products/{id}/individual-prices). venue_id
        # is passed as a query param too so HasSupplierProductVenuePriceAccess
        # (GET-shaped now) actually grants permission, isolating that it's
        # specifically the missing POST handler causing the 405, not auth.
        self.auth(self.manager_user)
        response = self.client.post(
            f"{self.url}?venue_id={self.venue.id}",
            {"venue_ids": [str(self.venue.id)], "price": "1.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(SupplierProductVenuePrice.objects.count(), 0)


class VenueProductListModelTests(TestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe R")

    def test_create_and_defaults(self):
        product_list = VenueProductList.objects.create(venue=self.venue, name="Для бара")
        self.assertEqual(product_list.status, EntityStatus.ACTIVE)
        self.assertEqual(list(self.venue.product_lists.all()), [product_list])

    def test_venue_delete_cascades(self):
        product_list = VenueProductList.objects.create(venue=self.venue, name="Для бара")
        self.venue.delete()
        self.assertFalse(VenueProductList.objects.filter(pk=product_list.pk).exists())

    def test_unique_active_name_per_venue(self):
        VenueProductList.objects.create(venue=self.venue, name="Для бара")
        with self.assertRaises(IntegrityError):
            VenueProductList.objects.create(venue=self.venue, name="Для бара")

    def test_archived_name_does_not_block_new_active_list(self):
        VenueProductList.objects.create(venue=self.venue, name="Для бара", status=EntityStatus.ARCHIVED)
        # Should not raise — the unique constraint only covers active rows.
        VenueProductList.objects.create(venue=self.venue, name="Для бара")

    def test_same_name_allowed_in_different_venues(self):
        other_venue = Venue.objects.create(display_name="Cafe S")
        VenueProductList.objects.create(venue=self.venue, name="Для бара")
        # Should not raise — constraint is scoped per venue.
        VenueProductList.objects.create(venue=other_venue, name="Для бара")


class VenueProductMatrixItemListsFieldTests(TestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe T")
        self.item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука")
        self.product_list = VenueProductList.objects.create(venue=self.venue, name="Для пекарни")

    def test_defaults_to_no_lists(self):
        self.assertEqual(list(self.item.lists.all()), [])

    def test_add_and_reverse_accessor(self):
        self.item.lists.add(self.product_list)
        self.assertEqual(list(self.item.lists.all()), [self.product_list])
        self.assertEqual(list(self.product_list.items.all()), [self.item])

    def test_item_can_belong_to_several_lists(self):
        other_list = VenueProductList.objects.create(venue=self.venue, name="Общий список")
        self.item.lists.set([self.product_list, other_list])
        self.assertEqual(self.item.lists.count(), 2)

    def test_list_delete_does_not_delete_item(self):
        self.item.lists.add(self.product_list)
        self.product_list.delete()
        self.assertTrue(VenueProductMatrixItem.objects.filter(pk=self.item.pk).exists())


class VenueProductListSelectorsAndServicesTests(TestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe U")

    def test_create_sets_active_status(self):
        product_list = create_venue_product_list(venue=self.venue, name="Для бара")
        self.assertEqual(product_list.status, EntityStatus.ACTIVE)

    def test_create_rejects_duplicate_active_name(self):
        create_venue_product_list(venue=self.venue, name="Для бара")
        with self.assertRaises(ValidationError):
            create_venue_product_list(venue=self.venue, name="Для бара")

    def test_rename_updates_name(self):
        product_list = create_venue_product_list(venue=self.venue, name="Для бара")
        renamed = rename_venue_product_list(product_list, "Для кухни")
        self.assertEqual(renamed.name, "Для кухни")

    def test_rename_rejects_collision_with_another_active_list(self):
        create_venue_product_list(venue=self.venue, name="Для кухни")
        product_list = create_venue_product_list(venue=self.venue, name="Для бара")
        with self.assertRaises(ValidationError):
            rename_venue_product_list(product_list, "Для кухни")

    def test_rename_to_own_current_name_does_not_raise(self):
        product_list = create_venue_product_list(venue=self.venue, name="Для бара")
        rename_venue_product_list(product_list, "Для бара")

    def test_archive_does_not_touch_matrix_items(self):
        product_list = create_venue_product_list(venue=self.venue, name="Для бара")
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука")
        item.lists.add(product_list)

        archive_venue_product_list(product_list)

        product_list.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(product_list.status, EntityStatus.ARCHIVED)
        self.assertEqual(item.status, EntityStatus.ACTIVE)
        self.assertEqual(list(item.lists.all()), [product_list])

    def test_archive_frees_name_for_new_active_list(self):
        product_list = create_venue_product_list(venue=self.venue, name="Для бара")
        archive_venue_product_list(product_list)
        # Should not raise — archived rows are excluded from the collision check.
        create_venue_product_list(venue=self.venue, name="Для бара")

    def test_queryset_returns_only_active_with_items_count(self):
        active = create_venue_product_list(venue=self.venue, name="Активный")
        archived = create_venue_product_list(venue=self.venue, name="Архивный")
        archive_venue_product_list(archived)
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука")
        item.lists.add(active)
        archived_item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Сахар", status=EntityStatus.ARCHIVED
        )
        archived_item.lists.add(active)

        results = list(venue_product_lists_queryset(self.venue.id))
        self.assertEqual([r.id for r in results], [active.id])
        self.assertEqual(results[0].items_count, 1)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueProductListAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe V")
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/lists"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_create(self):
        self.authenticate()
        response = self.client.post(self.url, {"name": "Для бара"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["name"], "Для бара")
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["items_count"], 0)

    def test_create_rejects_duplicate_active_name(self):
        self.authenticate()
        self.client.post(self.url, {"name": "Для бара"}, format="json")
        response = self.client.post(self.url, {"name": "Для бара"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_list_returns_only_active(self):
        self.authenticate()
        self.client.post(self.url, {"name": "Активный"}, format="json")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_venue_user_can_access_own_venue(self):
        user = User.objects.create(
            email="own-list-venue@example.com", full_name="Owner", role=UserRole.VENUE_USER, venue=self.venue
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_venue_user_rejected_for_other_venue(self):
        other_venue = Venue.objects.create(display_name="Cafe W")
        user = User.objects.create(
            email="wrong-list-venue@example.com", full_name="Wrong", role=UserRole.VENUE_USER, venue=other_venue
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_rejects_without_service_token(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueProductListDetailAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe X")
        self.list_url = f"/api/v1/catalog/venues/{self.venue.id}/lists"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_rename(self):
        self.authenticate()
        list_id = self.client.post(self.list_url, {"name": "Для бара"}, format="json").json()["id"]
        response = self.client.patch(f"{self.list_url}/{list_id}", {"name": "Для кухни"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["name"], "Для кухни")

    def test_archive_returns_204_and_hides_from_list(self):
        self.authenticate()
        list_id = self.client.post(self.list_url, {"name": "Для бара"}, format="json").json()["id"]
        response = self.client.delete(f"{self.list_url}/{list_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get(self.list_url).json(), [])

    def test_archive_does_not_delete_matrix_items(self):
        self.authenticate()
        list_id = self.client.post(self.list_url, {"name": "Для бара"}, format="json").json()["id"]
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука")
        item.lists.add(VenueProductList.objects.get(pk=list_id))
        self.client.delete(f"{self.list_url}/{list_id}")
        item.refresh_from_db()
        self.assertEqual(item.status, EntityStatus.ACTIVE)

    def test_archived_list_returns_404_on_patch(self):
        self.authenticate()
        list_id = self.client.post(self.list_url, {"name": "Для бара"}, format="json").json()["id"]
        self.client.delete(f"{self.list_url}/{list_id}")
        response = self.client.patch(f"{self.list_url}/{list_id}", {"name": "Другое"}, format="json")
        self.assertEqual(response.status_code, 404)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueProductListItemAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe Y")
        self.matrix_url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix"
        self.list_url = f"/api/v1/catalog/venues/{self.venue.id}/lists"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _create_list_and_item(self):
        list_id = self.client.post(self.list_url, {"name": "Для бара"}, format="json").json()["id"]
        item_id = self.client.post(self.matrix_url, {"requested_name": "Лимон"}, format="json").json()["id"]
        return list_id, item_id

    def test_add_item_to_list(self):
        self.authenticate()
        list_id, item_id = self._create_list_and_item()
        response = self.client.post(f"{self.list_url}/{list_id}/items", {"item_id": item_id}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["list_ids"], [list_id])

    def test_add_item_is_idempotent(self):
        self.authenticate()
        list_id, item_id = self._create_list_and_item()
        self.client.post(f"{self.list_url}/{list_id}/items", {"item_id": item_id}, format="json")
        response = self.client.post(f"{self.list_url}/{list_id}/items", {"item_id": item_id}, format="json")
        self.assertEqual(response.status_code, 201)
        item = VenueProductMatrixItem.objects.get(pk=item_id)
        self.assertEqual(item.lists.count(), 1)

    def test_add_item_from_other_venue_rejected(self):
        self.authenticate()
        list_id, _ = self._create_list_and_item()
        other_venue = Venue.objects.create(display_name="Cafe Z")
        other_item = VenueProductMatrixItem.objects.create(venue=other_venue, requested_name="Апельсин")
        response = self.client.post(f"{self.list_url}/{list_id}/items", {"item_id": str(other_item.id)}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_list_items_returns_only_members(self):
        self.authenticate()
        list_id, item_id = self._create_list_and_item()
        self.client.post(self.matrix_url, {"requested_name": "Не в списке"}, format="json")
        self.client.post(f"{self.list_url}/{list_id}/items", {"item_id": item_id}, format="json")
        response = self.client.get(f"{self.list_url}/{list_id}/items")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], item_id)

    def test_remove_item_from_list(self):
        self.authenticate()
        list_id, item_id = self._create_list_and_item()
        self.client.post(f"{self.list_url}/{list_id}/items", {"item_id": item_id}, format="json")
        response = self.client.delete(f"{self.list_url}/{list_id}/items/{item_id}")
        self.assertEqual(response.status_code, 204)
        item = VenueProductMatrixItem.objects.get(pk=item_id)
        self.assertEqual(item.status, EntityStatus.ACTIVE)
        self.assertEqual(list(item.lists.all()), [])

    def test_matrix_item_response_includes_list_ids(self):
        self.authenticate()
        list_id, item_id = self._create_list_and_item()
        self.client.post(f"{self.list_url}/{list_id}/items", {"item_id": item_id}, format="json")
        response = self.client.get(self.matrix_url)
        item_data = next(row for row in response.json()["results"] if row["id"] == item_id)
        self.assertEqual(item_data["list_ids"], [list_id])

    def test_list_ids_excludes_archived_list(self):
        self.authenticate()
        list_id, item_id = self._create_list_and_item()
        self.client.post(f"{self.list_url}/{list_id}/items", {"item_id": item_id}, format="json")
        self.client.delete(f"{self.list_url}/{list_id}")
        response = self.client.get(self.matrix_url)
        item_data = next(row for row in response.json()["results"] if row["id"] == item_id)
        self.assertEqual(item_data["list_ids"], [])


class VenueImportResolutionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner14@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Resolve")
        self.existing = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука в/с")
        self.unit_kg = Unit.objects.create(
            code="testunit122kg", name="Тестовая единица TASK122", kind="weight", status=EntityStatus.ACTIVE
        )

    def test_exact_name_match_is_first_candidate_but_needs_review(self):
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["мука в/с"])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].action)
        self.assertFalse(rows[0].auto_resolved)
        self.assertEqual(rows[0].candidates[0].matrix_item_id, self.existing.id)
        self.assertEqual(rows[0].candidates[0].requested_name, "Мука в/с")
        self.assertEqual(rows[0].candidates[0].score, 1.0)
        self.assertEqual(rows[0].matrix_item_id, self.existing.id)

    def test_no_match_proposes_create_new(self):
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сахар"])
        self.assertEqual(rows[0].action, "create_new")
        self.assertIsNone(rows[0].matrix_item_id)
        self.assertFalse(rows[0].auto_resolved)
        self.assertEqual(rows[0].candidates, [])

    def test_ignores_archived_matrix_items(self):
        self.existing.status = EntityStatus.ARCHIVED
        self.existing.save(update_fields=["status", "updated_at"])
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["мука в/с"])
        self.assertEqual(rows[0].action, "create_new")

    def test_alias_auto_resolves_without_reasking(self):
        other_item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сахар-песок")
        VenueProductImportAlias.objects.create(
            venue=self.venue, raw_name="сахар п", matrix_item=other_item, confirmed_by=self.owner
        )
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["сахар п"])
        self.assertEqual(rows[0].action, "update_existing")
        self.assertEqual(rows[0].matrix_item_id, other_item.id)
        self.assertTrue(rows[0].auto_resolved)
        self.assertEqual(rows[0].candidates, [])

    def test_duplicate_raw_names_each_get_their_own_row(self):
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сахар", "Сахар"])
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.action for r in rows], ["create_new", "create_new"])

    def test_alias_pointing_at_archived_item_is_not_auto_resolved(self):
        # An auto_resolved row never surfaces a dialog, so if preview kept
        # trusting an alias whose target was archived, apply would reject the
        # exact same row and the user would have no way to fix it.
        archived = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Сахар-песок", status=EntityStatus.ARCHIVED
        )
        VenueProductImportAlias.objects.create(
            venue=self.venue, raw_name="сахар п", matrix_item=archived, confirmed_by=self.owner
        )
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["сахар п"])
        self.assertEqual(rows[0].action, "create_new")
        self.assertIsNone(rows[0].matrix_item_id)
        self.assertFalse(rows[0].auto_resolved)

    def test_alias_pointing_at_archived_item_falls_back_to_name_match(self):
        archived = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Старая мука", status=EntityStatus.ARCHIVED
        )
        VenueProductImportAlias.objects.create(
            venue=self.venue, raw_name="мука в/с", matrix_item=archived, confirmed_by=self.owner
        )
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["мука в/с"])
        self.assertIsNone(rows[0].action)
        self.assertEqual(rows[0].matrix_item_id, self.existing.id)
        self.assertEqual(rows[0].candidates[0].matrix_item_id, self.existing.id)
        self.assertFalse(rows[0].auto_resolved)

    def test_similar_name_is_offered_as_candidate(self):
        similar = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Сливки ультрапастеризованные 33%", requested_unit_label="л"
        )
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сливки ультрапастеризованные 33% 1 л"])
        self.assertIsNone(rows[0].action)
        self.assertEqual(rows[0].candidates[0].matrix_item_id, similar.id)
        self.assertEqual(rows[0].candidates[0].requested_unit_label, "л")
        self.assertLess(rows[0].candidates[0].score, 1.0)
        self.assertGreaterEqual(rows[0].candidates[0].score, 0.3)

    @override_settings(VENUE_IMPORT_MATCH_MIN_SIMILARITY=0.99)
    def test_below_threshold_similarity_is_not_offered(self):
        VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сливки ультрапастеризованные 33%")
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сливки ультрапастеризованные 33% 1 л"])
        self.assertEqual(rows[0].action, "create_new")
        self.assertEqual(rows[0].candidates, [])

    def test_exact_match_ranks_first_among_equal_similarity(self):
        # pg_trgm ignores punctuation, so both names score 1.0 against the raw
        # name — only the case-insensitive exact one must come first.
        VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Молоко 3,2%")
        exact = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="молоко 3.2%")
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Молоко 3.2%"])
        self.assertEqual(rows[0].candidates[0].matrix_item_id, exact.id)
        self.assertEqual(len(rows[0].candidates), 2)

    def test_returns_at_most_five_candidates(self):
        for number in range(1, 7):
            VenueProductMatrixItem.objects.create(venue=self.venue, requested_name=f"Сахар белый {number}")
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сахар белый"])
        self.assertEqual(len(rows[0].candidates), 5)
        scores = [candidate.score for candidate in rows[0].candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_candidates_for_whole_batch_in_one_query(self):
        raw_names = ["мука в/с", "Сахар", "Соль", "Перец"]
        # 1 query for aliases + 1 query for every row's candidates.
        with self.assertNumQueries(2):
            resolve_venue_import_rows(venue=self.venue, raw_names=raw_names)

    def _alias(self, raw_name, requested_name):
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name=requested_name)
        VenueProductImportAlias.objects.create(
            venue=self.venue, raw_name=raw_name, matrix_item=item, confirmed_by=self.owner
        )
        return item

    def test_alias_rows_interleaved_with_candidate_rows_keep_their_positions(self):
        # Candidates are fetched only for unaliased names and mapped back by
        # position — an off-by-one there would hand row 1 the flour candidate.
        salt = self._alias("соль пов", "Соль поваренная")
        pepper = self._alias("перец ч", "Перец чёрный молотый")
        sugar = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сахар")
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["соль пов", "Сахар", "перец ч", "мука в/с"])

        self.assertEqual([row.raw_name for row in rows], ["соль пов", "Сахар", "перец ч", "мука в/с"])
        self.assertEqual([row.action for row in rows], ["update_existing", None, "update_existing", None])
        self.assertEqual([row.auto_resolved for row in rows], [True, False, True, False])
        self.assertEqual([row.matrix_item_id for row in rows], [salt.id, sugar.id, pepper.id, self.existing.id])
        self.assertEqual(rows[0].candidates, [])
        self.assertEqual(rows[2].candidates, [])
        self.assertEqual(rows[1].candidates[0].matrix_item_id, sugar.id)
        self.assertEqual(rows[1].candidates[0].score, 1.0)
        self.assertEqual(rows[3].candidates[0].matrix_item_id, self.existing.id)
        self.assertEqual(rows[3].candidates[0].score, 1.0)

    def test_all_rows_aliased_skips_candidates_query(self):
        salt = self._alias("соль пов", "Соль поваренная")
        pepper = self._alias("перец ч", "Перец чёрный молотый")
        # Only the alias lookup — _venue_import_candidates short-circuits on
        # an empty list of unaliased names.
        with self.assertNumQueries(1):
            rows = resolve_venue_import_rows(venue=self.venue, raw_names=["перец ч", "соль пов"])
        self.assertEqual([row.action for row in rows], ["update_existing", "update_existing"])
        self.assertEqual([row.matrix_item_id for row in rows], [pepper.id, salt.id])
        self.assertEqual([row.candidates for row in rows], [[], []])
        self.assertTrue(all(row.auto_resolved for row in rows))

    def test_other_venue_items_are_never_offered(self):
        other_venue = Venue.objects.create(display_name="Cafe Other Candidates")
        VenueProductMatrixItem.objects.create(venue=other_venue, requested_name="Сахар")
        rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сахар"])
        self.assertEqual(rows[0].candidates, [])

    # TASK-122: LLM re-ranking of pg_trgm candidates in the ambiguous zone —
    # same pattern as ResolveMasterProductImportRowsTests, now extended to
    # Справочник компании (raw ТЗ §1: "одинаково для обоих справочников").
    @override_settings(VENUE_IMPORT_MATCH_CONFIDENT_SIMILARITY=0.8, VENUE_IMPORT_CANDIDATE_RANKING_ENABLED=True)
    def test_ambiguous_top_score_calls_llm_and_uses_its_order(self):
        second = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сыр Российский")

        def fake_rerank(*, rows):
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0]["candidates"],
                [
                    {"id": str(self.existing.id), "canonical_name": "Мука в/с"},
                    {"id": str(second.id), "canonical_name": "Сыр Российский"},
                ],
            )
            return {rows[0]["row_number"]: [str(second.id), str(self.existing.id)]}

        with patch(
            "api.v1.catalog.selectors.master_catalog._venue_import_candidates",
            return_value={0: self._fake_candidates([(self.existing, 0.65), (second, 0.6)])},
        ):
            with patch(
                "api.v1.catalog.selectors.master_catalog.rerank_venue_candidates", side_effect=fake_rerank
            ) as mock_rerank:
                rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сыр"])
        mock_rerank.assert_called_once()
        self.assertEqual([c.matrix_item_id for c in rows[0].candidates], [second.id, self.existing.id])
        self.assertEqual(rows[0].matrix_item_id, second.id)
        # ТЗ §46: ranking never auto-decides — still "требует проверки".
        self.assertIsNone(rows[0].action)

    @override_settings(VENUE_IMPORT_MATCH_CONFIDENT_SIMILARITY=0.8, VENUE_IMPORT_CANDIDATE_RANKING_ENABLED=True)
    def test_confident_top_score_skips_llm_entirely(self):
        with patch(
            "api.v1.catalog.selectors.master_catalog._venue_import_candidates",
            return_value={0: self._fake_candidates([(self.existing, 0.95)])},
        ):
            with patch("api.v1.catalog.selectors.master_catalog.rerank_venue_candidates") as mock_rerank:
                rows = resolve_venue_import_rows(venue=self.venue, raw_names=["мука в/с"])
        mock_rerank.assert_not_called()
        self.assertEqual(rows[0].matrix_item_id, self.existing.id)

    @override_settings(VENUE_IMPORT_CANDIDATE_RANKING_ENABLED=True)
    def test_no_candidates_skips_llm_entirely(self):
        with patch("api.v1.catalog.selectors.master_catalog.rerank_venue_candidates") as mock_rerank:
            rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Совсем новый товар"])
        mock_rerank.assert_not_called()
        self.assertEqual(rows[0].action, "create_new")

    @override_settings(VENUE_IMPORT_MATCH_CONFIDENT_SIMILARITY=0.8, VENUE_IMPORT_CANDIDATE_RANKING_ENABLED=True)
    def test_llm_failure_keeps_original_pg_trgm_order(self):
        second = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сыр Российский")
        with patch(
            "api.v1.catalog.selectors.master_catalog._venue_import_candidates",
            return_value={0: self._fake_candidates([(self.existing, 0.65), (second, 0.6)])},
        ):
            with patch(
                "api.v1.catalog.selectors.master_catalog.rerank_venue_candidates",
                side_effect=CandidateRankingError("boom"),
            ):
                rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сыр"])
        self.assertEqual([c.matrix_item_id for c in rows[0].candidates], [self.existing.id, second.id])

    @override_settings(VENUE_IMPORT_CANDIDATE_RANKING_ENABLED=False, VENUE_IMPORT_MATCH_CONFIDENT_SIMILARITY=0.8)
    def test_ranking_disabled_by_flag_skips_llm_even_in_ambiguous_zone(self):
        with patch(
            "api.v1.catalog.selectors.master_catalog._venue_import_candidates",
            return_value={0: self._fake_candidates([(self.existing, 0.65)])},
        ):
            with patch("api.v1.catalog.selectors.master_catalog.rerank_venue_candidates") as mock_rerank:
                resolve_venue_import_rows(venue=self.venue, raw_names=["мука в/с"])
        mock_rerank.assert_not_called()

    # TASK-122 п.1: unit-of-measurement LLM suggestion — same pattern as
    # ResolveMasterProductImportRowsTests' unit-LLM tests, now for
    # resolve_venue_import_rows.
    def test_unit_labels_none_skips_unit_resolution_entirely(self):
        with patch("api.v1.catalog.selectors.master_catalog.suggest_venue_unit_ids") as mock_suggest:
            rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сахар"])
        mock_suggest.assert_not_called()
        self.assertIsNone(rows[0].unit_id)
        self.assertFalse(rows[0].unit_unrecognized)
        self.assertIsNone(rows[0].unit_source)

    def test_known_unit_label_resolves_without_calling_llm(self):
        with patch("api.v1.catalog.selectors.master_catalog.suggest_venue_unit_ids") as mock_suggest:
            rows = resolve_venue_import_rows(
                venue=self.venue, raw_names=["Сахар"], unit_labels=["ТЕСТОВАЯ ЕДИНИЦА TASK122"]
            )
        mock_suggest.assert_not_called()
        self.assertEqual(rows[0].unit_id, self.unit_kg.id)
        self.assertFalse(rows[0].unit_unrecognized)
        self.assertIsNone(rows[0].unit_source)

    def test_llm_fills_unrecognized_unit_when_confident(self):
        def fake_suggest(*, rows, units):
            return {row["row_number"]: str(self.unit_kg.id) for row in rows}

        with patch(
            "api.v1.catalog.selectors.master_catalog.suggest_venue_unit_ids", side_effect=fake_suggest
        ) as mock_suggest:
            rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сахар"], unit_labels=["неизвестная единица"])
        mock_suggest.assert_called_once()
        self.assertFalse(rows[0].unit_unrecognized)
        self.assertEqual(rows[0].unit_id, self.unit_kg.id)
        self.assertEqual(rows[0].unit_source, "llm")
        call_kwargs = mock_suggest.call_args.kwargs
        for sent_row in call_kwargs["rows"]:
            self.assertEqual(set(sent_row.keys()), {"row_number", "raw_name", "unit_label"})
        for sent_unit in call_kwargs["units"]:
            self.assertEqual(set(sent_unit.keys()), {"id", "code", "name"})

    def test_llm_answer_for_a_non_offered_position_is_ignored(self):
        def fake_suggest(*, rows, units):
            # Simulate the model answering for a row it was never offered
            # (only "Второй" with the unrecognized label was offered).
            return {1: str(self.unit_kg.id), 2: str(self.unit_kg.id)}

        with patch("api.v1.catalog.selectors.master_catalog.suggest_venue_unit_ids", side_effect=fake_suggest):
            rows = resolve_venue_import_rows(
                venue=self.venue,
                raw_names=["Первый", "Второй"],
                unit_labels=["ТЕСТОВАЯ ЕДИНИЦА TASK122", "неизвестная единица"],
            )
        # "Первый" already matched from the file (known label) — it was never
        # in unrecognized_positions, so its unsolicited answer is ignored.
        self.assertEqual(rows[0].unit_id, self.unit_kg.id)
        self.assertIsNone(rows[0].unit_source)
        # "Второй" was actually offered and gets resolved.
        self.assertEqual(rows[1].unit_id, self.unit_kg.id)
        self.assertEqual(rows[1].unit_source, "llm")

    def test_llm_suggestion_outside_allowed_units_is_ignored(self):
        with patch(
            "api.v1.catalog.selectors.master_catalog.suggest_venue_unit_ids",
            return_value={1: "11111111-1111-1111-1111-111111111111"},
        ):
            rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сахар"], unit_labels=["неизвестная единица"])
        self.assertTrue(rows[0].unit_unrecognized)
        self.assertIsNone(rows[0].unit_id)
        self.assertIsNone(rows[0].unit_source)

    def test_llm_error_leaves_row_unaffected(self):
        with patch(
            "api.v1.catalog.selectors.master_catalog.suggest_venue_unit_ids", side_effect=UnitSuggestionError("boom")
        ):
            rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сахар"], unit_labels=["неизвестная единица"])
        self.assertTrue(rows[0].unit_unrecognized)
        self.assertIsNone(rows[0].unit_id)
        self.assertIsNone(rows[0].unit_source)

    @override_settings(VENUE_IMPORT_UNIT_LLM_ENABLED=False)
    def test_llm_not_called_when_flag_disabled(self):
        with patch("api.v1.catalog.selectors.master_catalog.suggest_venue_unit_ids") as mock_suggest:
            rows = resolve_venue_import_rows(venue=self.venue, raw_names=["Сахар"], unit_labels=["неизвестная единица"])
        mock_suggest.assert_not_called()
        self.assertTrue(rows[0].unit_unrecognized)

    def test_aliased_row_gets_no_unit_decision(self):
        VenueProductImportAlias.objects.create(
            venue=self.venue, raw_name="мука в/с", matrix_item=self.existing, confirmed_by=self.owner
        )
        with patch("api.v1.catalog.selectors.master_catalog.suggest_venue_unit_ids") as mock_suggest:
            rows = resolve_venue_import_rows(
                venue=self.venue, raw_names=["мука в/с"], unit_labels=["неизвестная единица"]
            )
        mock_suggest.assert_not_called()
        self.assertEqual(rows[0].action, "update_existing")
        self.assertIsNone(rows[0].unit_id)
        self.assertFalse(rows[0].unit_unrecognized)
        self.assertIsNone(rows[0].unit_source)

    def _fake_candidates(self, item_scores):
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                matrix_item_id=item.id,
                requested_name=item.requested_name,
                requested_unit_label=item.requested_unit_label,
                updated_at=item.updated_at,
                score=score,
            )
            for item, score in item_scores
        ]


class ApplyVenueImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner15@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Apply")
        self.existing = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука в/с")
        self.unit_kg = Unit.objects.create(
            code="testunit122kgapply", name="Тестовая единица TASK122 Apply", kind="weight", status=EntityStatus.ACTIVE
        )

    def test_update_existing_writes_only_alias(self):
        rows = apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[{"raw_name": "мука в/с 1кг", "action": "update_existing", "matrix_item_id": self.existing.id}],
        )
        self.assertEqual(rows[0].matrix_item_id, self.existing.id)
        self.assertTrue(rows[0].alias_created)
        alias = VenueProductImportAlias.objects.get(venue=self.venue, raw_name="мука в/с 1кг")
        self.assertEqual(alias.matrix_item_id, self.existing.id)
        self.assertEqual(alias.confirmed_by_id, self.owner.id)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.requested_name, "Мука в/с")

    def test_create_new_creates_matrix_item_with_accounting_system_source(self):
        rows = apply_venue_import(
            venue=self.venue, user=self.owner, decisions=[{"raw_name": "Сахар-песок", "action": "create_new"}]
        )
        item = VenueProductMatrixItem.objects.get(venue=self.venue, requested_name="Сахар-песок")
        self.assertEqual(item.source, VenueProductMatrixItemSource.ACCOUNTING_SYSTEM)
        self.assertEqual(rows[0].matrix_item_id, item.id)
        alias = VenueProductImportAlias.objects.get(venue=self.venue, raw_name="Сахар-песок")
        self.assertEqual(alias.matrix_item_id, item.id)

    def test_create_new_preserves_unit_and_note_from_file_preview(self):
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[
                {
                    "row_number": 7,
                    "raw_name": "Сахар-песок",
                    "requested_unit_label": "кг",
                    "note": "мешок 25 кг",
                    "action": "create_new",
                }
            ],
        )
        item = VenueProductMatrixItem.objects.get(venue=self.venue, requested_name="Сахар-песок")
        self.assertEqual(item.requested_unit_label, "кг")
        self.assertEqual(item.note, "мешок 25 кг")

    def test_create_new_persists_unit_id_when_provided(self):
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[
                {
                    "raw_name": "Соль",
                    "requested_unit_label": "кг",
                    "unit_id": str(self.unit_kg.id),
                    "action": "create_new",
                }
            ],
        )
        item = VenueProductMatrixItem.objects.get(venue=self.venue, requested_name="Соль")
        self.assertEqual(item.unit_id, self.unit_kg.id)
        self.assertEqual(item.requested_unit_label, "кг")

    def test_create_new_leaves_unit_null_when_not_provided(self):
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[{"raw_name": "Перец", "requested_unit_label": "банка", "action": "create_new"}],
        )
        item = VenueProductMatrixItem.objects.get(venue=self.venue, requested_name="Перец")
        self.assertIsNone(item.unit_id)
        self.assertEqual(item.requested_unit_label, "банка")

    def test_replace_persists_unit_id_when_provided(self):
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[
                {
                    "raw_name": "Мука пшеничная в/с",
                    "requested_unit_label": "кг",
                    "unit_id": str(self.unit_kg.id),
                    "action": "replace",
                    "matrix_item_id": str(self.existing.id),
                }
            ],
        )
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.unit_id, self.unit_kg.id)
        self.assertEqual(self.existing.requested_name, "Мука пшеничная в/с")

    def test_update_existing_does_not_touch_unit(self):
        self.existing.unit = self.unit_kg
        self.existing.save(update_fields=["unit", "updated_at"])
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[
                {
                    "raw_name": "мука в/с",
                    "action": "update_existing",
                    "matrix_item_id": str(self.existing.id),
                    "unit_id": "11111111-1111-1111-1111-111111111111",
                }
            ],
        )
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.unit_id, self.unit_kg.id)

    def test_skip_writes_nothing(self):
        rows = apply_venue_import(
            venue=self.venue, user=self.owner, decisions=[{"raw_name": "Что-то непонятное", "action": "skip"}]
        )
        self.assertIsNone(rows[0].matrix_item_id)
        self.assertFalse(
            VenueProductImportAlias.objects.filter(venue=self.venue, raw_name="Что-то непонятное").exists()
        )

    def test_update_existing_requires_matrix_item_id(self):
        with self.assertRaises(ValidationError):
            apply_venue_import(
                venue=self.venue, user=self.owner, decisions=[{"raw_name": "Мука в/с", "action": "update_existing"}]
            )

    def test_update_existing_rejects_matrix_item_from_another_venue(self):
        other_venue = Venue.objects.create(display_name="Cafe Other")
        other_item = VenueProductMatrixItem.objects.create(venue=other_venue, requested_name="Мука в/с")
        with self.assertRaises(ValidationError):
            apply_venue_import(
                venue=self.venue,
                user=self.owner,
                decisions=[{"raw_name": "Мука в/с", "action": "update_existing", "matrix_item_id": other_item.id}],
            )

    def test_duplicate_raw_names_in_one_batch_collapse_to_the_last_decision(self):
        # Postgres refuses to let one ON CONFLICT statement touch the same row
        # twice, so the batched alias write must dedupe first — last wins, as
        # the old row-by-row update_or_create loop did.
        other = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука 2-й сорт")
        rows = apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[
                {"raw_name": "мука", "action": "update_existing", "matrix_item_id": self.existing.id},
                {"raw_name": "мука", "action": "update_existing", "matrix_item_id": other.id},
            ],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.matrix_item_id for r in rows], [self.existing.id, other.id])
        aliases = VenueProductImportAlias.objects.filter(venue=self.venue, raw_name="мука")
        self.assertEqual(aliases.count(), 1)
        self.assertEqual(aliases.get().matrix_item_id, other.id)

    def test_invalid_decision_anywhere_in_the_batch_writes_nothing(self):
        with self.assertRaises(ValidationError):
            apply_venue_import(
                venue=self.venue,
                user=self.owner,
                decisions=[
                    {"raw_name": "Хороший товар", "action": "create_new"},
                    {"raw_name": "плохой", "action": "update_existing", "matrix_item_id": uuid4()},
                ],
            )
        self.assertFalse(
            VenueProductMatrixItem.objects.filter(venue=self.venue, requested_name="Хороший товар").exists()
        )
        self.assertFalse(VenueProductImportAlias.objects.filter(venue=self.venue).exists())

    def _apply_query_count(self, size):
        venue = Venue.objects.create(display_name=f"Cafe Bulk {size}")
        existing = [
            VenueProductMatrixItem.objects.create(venue=venue, requested_name=f"Старый {i}") for i in range(size)
        ]
        decisions = [
            {"raw_name": f"старый {i}", "action": "update_existing", "matrix_item_id": item.id}
            for i, item in enumerate(existing)
        ]
        decisions += [{"raw_name": f"Новый {i}", "action": "create_new"} for i in range(size)]
        decisions += [{"raw_name": f"Пропуск {i}", "action": "skip"} for i in range(size)]
        with CaptureQueriesContext(connection) as captured:
            rows = apply_venue_import(venue=venue, user=self.owner, decisions=decisions)
        self.assertEqual(len(rows), size * 3)
        self.assertEqual(VenueProductImportAlias.objects.filter(venue=venue).count(), size * 2)
        self.assertEqual(
            VenueProductMatrixItem.objects.filter(
                venue=venue, source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM
            ).count(),
            size,
        )
        return len(captured.captured_queries)

    def test_apply_query_count_is_constant_not_per_decision(self):
        # Import is a bulk operation: a realistic accounting-export batch is
        # hundreds of rows, so the write path must not issue queries per row.
        small = self._apply_query_count(3)
        large = self._apply_query_count(20)
        self.assertEqual(small, large, f"query count scales with batch size: {small} -> {large}")
        self.assertLessEqual(large, 9, f"expected bounded queries including the venue lock, got {large}")

    def test_update_existing_error_messages_distinguish_their_causes(self):
        other_venue = Venue.objects.create(display_name="Cafe Other Msg")
        foreign = VenueProductMatrixItem.objects.create(venue=other_venue, requested_name="Мука в/с")
        archived = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Мука архивная", status=EntityStatus.ARCHIVED
        )

        def message_for(decision):
            with self.assertRaises(ValidationError) as ctx:
                apply_venue_import(venue=self.venue, user=self.owner, decisions=[decision])
            return str(ctx.exception.detail["decisions"])

        missing = message_for({"raw_name": "мука", "action": "update_existing"})
        not_found = message_for({"raw_name": "мука", "action": "update_existing", "matrix_item_id": foreign.id})
        is_archived = message_for({"raw_name": "мука", "action": "update_existing", "matrix_item_id": archived.id})

        self.assertEqual(len({missing, not_found, is_archived}), 3, (missing, not_found, is_archived))
        # The archived case must not claim the item belongs to another venue.
        self.assertIn("рхив", is_archived)
        self.assertNotIn("рхив", not_found)

    def test_reapplying_same_update_existing_decision_does_not_duplicate_alias(self):
        decisions = [{"raw_name": "мука в/с 1кг", "action": "update_existing", "matrix_item_id": self.existing.id}]
        apply_venue_import(venue=self.venue, user=self.owner, decisions=decisions)
        apply_venue_import(venue=self.venue, user=self.owner, decisions=decisions)
        self.assertEqual(VenueProductImportAlias.objects.filter(venue=self.venue, raw_name="мука в/с 1кг").count(), 1)

    def _replace(self, target, raw_name="Мука пшеничная в/с", unit_label="мешок 25 кг"):
        return {
            "raw_name": raw_name,
            "action": "replace",
            "matrix_item_id": target.id,
            "requested_unit_label": unit_label,
        }

    def test_replace_overwrites_name_and_unit_keeping_id_and_links(self):
        master = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука")
        supplier_product = _make_supplier_product(price="100.00")
        self.existing.master_product = master
        self.existing.supplier_product = supplier_product
        self.existing.note = "старое"
        self.existing.requested_unit_label = "кг"
        self.existing.save(
            update_fields=["master_product", "supplier_product", "note", "requested_unit_label", "updated_at"]
        )
        rows = apply_venue_import(venue=self.venue, user=self.owner, decisions=[self._replace(self.existing)])
        self.assertEqual(rows[0].action, "replace")
        self.assertEqual(rows[0].matrix_item_id, self.existing.id)
        self.assertEqual(rows[0].matrix_item_requested_name, "Мука пшеничная в/с")
        self.assertTrue(rows[0].alias_created)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.requested_name, "Мука пшеничная в/с")
        self.assertEqual(self.existing.requested_unit_label, "мешок 25 кг")
        self.assertEqual(self.existing.note, "старое")
        self.assertEqual(self.existing.master_product_id, master.id)
        self.assertEqual(self.existing.supplier_product_id, supplier_product.id)
        self.assertEqual(VenueProductMatrixItem.objects.filter(venue=self.venue).count(), 1)

    def test_replace_writes_alias_and_clears_missing_from_latest_import(self):
        self.existing.source = VenueProductMatrixItemSource.ACCOUNTING_SYSTEM
        self.existing.missing_from_latest_import = True
        self.existing.save(update_fields=["source", "missing_from_latest_import", "updated_at"])
        apply_venue_import(venue=self.venue, user=self.owner, decisions=[self._replace(self.existing)])
        alias = VenueProductImportAlias.objects.get(venue=self.venue, raw_name="Мука пшеничная в/с")
        self.assertEqual(alias.matrix_item_id, self.existing.id)
        self.existing.refresh_from_db()
        self.assertFalse(self.existing.missing_from_latest_import)

    def test_replace_without_unit_label_raises_and_writes_nothing(self):
        for unit_label in (None, "", "   "):
            with self.subTest(unit_label=unit_label):
                with self.assertRaises(ValidationError):
                    apply_venue_import(
                        venue=self.venue,
                        user=self.owner,
                        decisions=[self._replace(self.existing, unit_label=unit_label)],
                    )
                self.existing.refresh_from_db()
                self.assertEqual(self.existing.requested_name, "Мука в/с")

    def test_replace_rejects_foreign_or_archived_target(self):
        other_venue = Venue.objects.create(display_name="Cafe Other Replace")
        foreign = VenueProductMatrixItem.objects.create(venue=other_venue, requested_name="Чужая мука")
        archived = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Архивная мука", status=EntityStatus.ARCHIVED
        )
        for target in (foreign, archived):
            with self.subTest(target=target.requested_name):
                with self.assertRaises(ValidationError):
                    apply_venue_import(venue=self.venue, user=self.owner, decisions=[self._replace(target)])
                target.refresh_from_db()
                self.assertNotEqual(target.requested_name, "Мука пшеничная в/с")

    def test_replace_with_same_values_does_not_rewrite_fields(self):
        self.existing.requested_unit_label = "кг"
        self.existing.save(update_fields=["requested_unit_label", "updated_at"])
        before = VenueProductMatrixItem.objects.get(pk=self.existing.pk).updated_at
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[self._replace(self.existing, raw_name="Мука в/с", unit_label="кг")],
        )
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.updated_at, before)

    def test_replace_writes_audit_entry(self):
        self.existing.requested_unit_label = "кг"
        self.existing.save(update_fields=["requested_unit_label", "updated_at"])
        apply_venue_import(venue=self.venue, user=self.owner, decisions=[self._replace(self.existing)])
        entry = AuditLog.objects.get(entity_id=self.existing.id, action=AuditLogAction.REPLACED)
        self.assertEqual(entry.entity_type, AuditLogEntityType.VENUE_PRODUCT_MATRIX_ITEM)
        self.assertEqual(entry.scope_id, self.venue.id)
        self.assertEqual(entry.actor_id, self.owner.id)
        self.assertEqual(entry.summary, "«Мука в/с» (кг) → «Мука пшеничная в/с» (мешок 25 кг)")

    def test_replace_flags_needs_review_when_master_product_linked(self):
        master = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука")
        self.existing.master_product = master
        self.existing.save(update_fields=["master_product", "updated_at"])
        apply_venue_import(venue=self.venue, user=self.owner, decisions=[self._replace(self.existing)])
        self.existing.refresh_from_db()
        self.assertTrue(self.existing.needs_review)

    def test_replace_does_not_flag_needs_review_without_master_product(self):
        self.assertIsNone(self.existing.master_product_id)
        apply_venue_import(venue=self.venue, user=self.owner, decisions=[self._replace(self.existing)])
        self.existing.refresh_from_db()
        self.assertFalse(self.existing.needs_review)

    def test_two_replaces_of_one_target_first_wins_second_conflicts(self):
        """2026-09-29 (unified-import-architecture spec §9.1): было
        whole-batch-abort (TASK-73), стало per-row, same shape as
        supplier-commit -- первая replace-декларация на target применяется,
        вторая возвращается с conflict."""
        rows = apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[
                self._replace(self.existing, raw_name="Мука в/с 1кг"),
                self._replace(self.existing, raw_name="Мука в/с 2кг"),
            ],
        )
        self.assertIsNone(rows[0].conflict)
        self.assertIsNotNone(rows[1].conflict)
        self.assertIn(str(self.existing.id), rows[1].conflict)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.requested_name, "Мука в/с 1кг")
        self.assertTrue(VenueProductImportAlias.objects.filter(venue=self.venue, raw_name="Мука в/с 1кг").exists())


class ApplyVenueImportAutoMatchTests(TestCase):
    """TASK-45 step 6/7: create_new rows from apply_venue_import now go
    through the same §11.1 auto-match rule as a manually added row
    (create_product_matrix_item) — see _auto_assign_master_products_to_new_items.
    update_existing rows are untouched by this (Plan 5 §6 rule 7 still
    applies): auto-match only ever runs on freshly created rows."""

    def setUp(self):
        self.owner = User.objects.create(
            email="owner-import-automatch@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Import Auto Match")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)

    def test_create_new_rows_are_auto_matched_to_master_catalog(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        rows = apply_venue_import(
            venue=self.venue, user=self.owner, decisions=[{"raw_name": "AAAA", "action": "create_new"}]
        )
        item = VenueProductMatrixItem.objects.get(pk=rows[0].matrix_item_id)
        self.assertEqual(item.master_product_id, master_product.id)
        self.assertFalse(item.needs_review)

    def test_two_create_new_rows_in_one_batch_do_not_claim_the_same_master_product(self):
        rows = apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[
                {"raw_name": "AAAA", "action": "create_new"},
                {"raw_name": "AAAA", "action": "create_new"},
            ],
        )
        items = [VenueProductMatrixItem.objects.get(pk=row.matrix_item_id) for row in rows]
        linked = [item for item in items if item.master_product_id is not None]
        self.assertEqual(len(linked), 1)
        self.assertEqual(sum(1 for item in items if item.needs_review), 1)
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner, canonical_name="AAAA").count(), 1)

    def test_missing_from_latest_import_flag_still_set_alongside_auto_match(self):
        stale = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Старый товар", source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM
        )
        apply_venue_import(venue=self.venue, user=self.owner, decisions=[{"raw_name": "AAAA", "action": "create_new"}])
        stale.refresh_from_db()
        self.assertTrue(stale.missing_from_latest_import)
        new_item = VenueProductMatrixItem.objects.get(venue=self.venue, requested_name="AAAA")
        self.assertIsNotNone(new_item.master_product_id)

    def test_update_existing_is_not_auto_matched(self):
        existing = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука в/с")
        MasterProduct.objects.create(owner=self.owner, canonical_name="Мука в/с")
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[{"raw_name": "мука в/с", "action": "update_existing", "matrix_item_id": existing.id}],
        )
        existing.refresh_from_db()
        self.assertIsNone(existing.master_product_id)

    def test_create_new_auto_link_is_seen_by_stage_two_as_already_resolved(self):
        from api.v1.catalog.selectors import resolve_master_product_matches_for_import

        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        rows = apply_venue_import(
            venue=self.venue, user=self.owner, decisions=[{"raw_name": "AAAA", "action": "create_new"}]
        )
        item_id = rows[0].matrix_item_id
        matches = resolve_master_product_matches_for_import(
            venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item_id]
        )
        # Deliberate, reviewed tradeoff (2026-09-05): import-time auto-match runs
        # immediately and is never re-surfaced through stage 2's own confirmation
        # step — a row that already has a master_product always reports
        # keep_existing here, regardless of how confident the original auto-match was.
        self.assertEqual(matches[0].proposed_action, "keep_existing")
        self.assertEqual(matches[0].current_master_product_id, master_product.id)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueProductImportAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner16@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe API Import")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.existing = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука в/с")
        self.unit_kg = Unit.objects.create(
            code="testunit122kgapi", name="Тестовая единица TASK122 API", kind="weight", status=EntityStatus.ACTIVE
        )
        self.preview_url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/preview"
        self.apply_url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/apply"
        self.file_preview_url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/file/preview"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_preview_matches_existing_and_proposes_new(self):
        self.auth(self.owner)
        response = self.client.post(self.preview_url, {"raw_names": ["мука в/с", "Сахар"]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()
        self.assertIsNone(rows[0]["action"])
        self.assertEqual(rows[0]["candidates"][0]["matrix_item_id"], str(self.existing.id))
        self.assertEqual(rows[1]["candidates"], [])
        self.assertEqual(rows[0]["matrix_item_id"], str(self.existing.id))
        self.assertEqual(rows[1]["action"], "create_new")
        self.assertIsNone(rows[1]["matrix_item_id"])

    def test_preview_does_not_write(self):
        self.auth(self.owner)
        self.client.post(self.preview_url, {"raw_names": ["Совсем новый товар"]}, format="json")
        self.assertFalse(VenueProductMatrixItem.objects.filter(requested_name="Совсем новый товар").exists())

    def test_preview_rejects_empty_raw_names(self):
        self.auth(self.owner)
        response = self.client.post(self.preview_url, {"raw_names": []}, format="json")
        self.assertEqual(response.status_code, 400)

    @patch("api.v1.catalog.services.master_core.find_candidate_master_products", return_value=[])
    def test_apply_writes_and_returns_rows(self, _external_embedding_lookup):
        self.auth(self.owner)
        response = self.client.post(
            self.apply_url,
            {
                "decisions": [
                    {"raw_name": "мука в/с", "action": "update_existing", "matrix_item_id": str(self.existing.id)},
                    {"raw_name": "Сахар", "action": "create_new"},
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(VenueProductMatrixItem.objects.filter(venue=self.venue, requested_name="Сахар").exists())
        self.assertEqual(VenueProductImportAlias.objects.filter(venue=self.venue).count(), 2)

    def test_apply_rejects_update_existing_without_matrix_item_id(self):
        self.auth(self.owner)
        response = self.client.post(
            self.apply_url, {"decisions": [{"raw_name": "мука в/с", "action": "update_existing"}]}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_apply_replace_and_field_errors(self):
        self.auth(self.owner)
        missing_unit = self.client.post(
            self.apply_url,
            {"decisions": [{"raw_name": "Мука в/с 1кг", "action": "replace", "matrix_item_id": str(self.existing.id)}]},
            format="json",
        )
        self.assertEqual(missing_unit.status_code, 400)
        self.assertEqual(
            missing_unit.json()["decisions"][0]["requested_unit_label"], ["Обязателен при action=replace."]
        )

        response = self.client.post(
            self.apply_url,
            {
                "decisions": [
                    {
                        "raw_name": "Мука в/с 1кг",
                        "action": "replace",
                        "matrix_item_id": str(self.existing.id),
                        "requested_unit_label": "кг",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()[0]  # venue apply returns a bare list of rows
        self.assertEqual(row["action"], "replace")
        self.assertEqual(row["matrix_item_requested_name"], "Мука в/с 1кг")

    def test_denied_for_stranger(self):
        stranger = User.objects.create(email="stranger7@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
        self.auth(stranger)
        response = self.client.post(self.preview_url, {"raw_names": ["Сахар"]}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_denied_without_auth(self):
        response = self.client.post(self.preview_url, {"raw_names": ["Сахар"]}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_preview_rejects_oversized_raw_names_list(self):
        self.auth(self.owner)
        response = self.client.post(self.preview_url, {"raw_names": ["Сахар"] * 1001}, format="json")
        self.assertEqual(response.status_code, 400, response.content)

    def test_apply_rejects_oversized_decisions_list(self):
        self.auth(self.owner)
        response = self.client.post(
            self.apply_url,
            {"decisions": [{"raw_name": "Сахар", "action": "skip"}] * 1001},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        # Same {"decisions": [str]} shape as every other batch-level error
        # (ImportDecisionsErrorSerializer), not DRF's {"non_field_errors": [...]}.
        self.assertEqual(response.json(), {"decisions": ["Не более 1000 решений за запрос."]})
        self.assertFalse(VenueProductImportAlias.objects.filter(venue=self.venue).exists())

    def test_preview_row_includes_matrix_item_updated_at(self):
        self.auth(self.owner)
        response = self.client.post(self.preview_url, {"raw_names": ["мука в/с"]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        updated_at = response.json()[0]["matrix_item_updated_at"]
        self.assertIsNotNone(updated_at)
        # Round-trips through parse_datetime() the same way apply's
        # expected_updated_at field would deserialize it (TASK-76).
        self.assertEqual(parse_datetime(updated_at), self.existing.updated_at)

    def test_apply_accepts_replace_with_matching_expected_updated_at(self):
        self.auth(self.owner)
        preview = self.client.post(self.preview_url, {"raw_names": ["мука в/с"]}, format="json")
        expected_updated_at = preview.json()[0]["matrix_item_updated_at"]

        response = self.client.post(
            self.apply_url,
            {
                "decisions": [
                    {
                        "raw_name": "Мука в/с 1кг",
                        "action": "replace",
                        "matrix_item_id": str(self.existing.id),
                        "requested_unit_label": "кг",
                        "expected_updated_at": expected_updated_at,
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.requested_name, "Мука в/с 1кг")

    def test_apply_conflicts_replace_with_stale_expected_updated_at(self):
        """2026-09-29 (unified-import-architecture spec §9.1/§9.2): было
        whole-batch-abort (TASK-76), стало per-row conflict."""
        self.auth(self.owner)
        preview = self.client.post(self.preview_url, {"raw_names": ["мука в/с"]}, format="json")
        stale_expected_updated_at = preview.json()[0]["matrix_item_updated_at"]

        # Someone else changes the target between preview and apply.
        self.existing.note = "изменено параллельно"
        self.existing.save(update_fields=["note", "updated_at"])

        response = self.client.post(
            self.apply_url,
            {
                "decisions": [
                    {
                        "raw_name": "Мука в/с 1кг",
                        "action": "replace",
                        "matrix_item_id": str(self.existing.id),
                        "requested_unit_label": "кг",
                        "expected_updated_at": stale_expected_updated_at,
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()[0]["conflict"],
            '"Мука в/с 1кг": товар изменился после проверки. Проверьте его ещё раз.',
        )
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.requested_name, "Мука в/с")

    def test_apply_conflicts_update_existing_with_stale_expected_updated_at(self):
        self.auth(self.owner)
        preview = self.client.post(self.preview_url, {"raw_names": ["мука в/с"]}, format="json")
        stale_expected_updated_at = preview.json()[0]["matrix_item_updated_at"]

        self.existing.note = "изменено параллельно"
        self.existing.save(update_fields=["note", "updated_at"])

        response = self.client.post(
            self.apply_url,
            {
                "decisions": [
                    {
                        "raw_name": "мука в/с",
                        "action": "update_existing",
                        "matrix_item_id": str(self.existing.id),
                        "expected_updated_at": stale_expected_updated_at,
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()[0]["conflict"],
            '"мука в/с": товар изменился после проверки. Проверьте его ещё раз.',
        )

    def test_apply_without_expected_updated_at_skips_the_check(self):
        """Backward compat (TASK-76): a caller that omits expected_updated_at
        (today's frontend) behaves exactly as before the check existed."""
        self.auth(self.owner)
        self.existing.note = "изменено параллельно"
        self.existing.save(update_fields=["note", "updated_at"])

        response = self.client.post(
            self.apply_url,
            {
                "decisions": [
                    {
                        "raw_name": "Мука в/с 1кг",
                        "action": "replace",
                        "matrix_item_id": str(self.existing.id),
                        "requested_unit_label": "кг",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_apply_foreign_target_error_is_a_list(self):
        foreign = VenueProductMatrixItem.objects.create(
            venue=Venue.objects.create(display_name="Cafe Foreign Target"), requested_name="Мука чужая"
        )
        self.auth(self.owner)
        response = self.client.post(
            self.apply_url,
            {
                "decisions": [
                    {
                        "raw_name": "Мука пшеничная",
                        "action": "replace",
                        "matrix_item_id": str(foreign.id),
                        "requested_unit_label": "кг",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        errors = response.json()["decisions"]
        self.assertIsInstance(errors, list)
        self.assertEqual(len(errors), 1)
        self.assertIn(f"matrix_item_id={foreign.id}", errors[0])
        self.assertIn("нет в товарной матрице этого заведения", errors[0])
        foreign.refresh_from_db()
        self.assertEqual(foreign.requested_name, "Мука чужая")

    def test_apply_two_replaces_of_one_target_first_wins_second_conflicts(self):
        """2026-09-29 (unified-import-architecture spec §9.1): было
        whole-batch-abort, стало per-row -- первая декларация применяется,
        вторая возвращается с conflict, ответ 200."""
        self.auth(self.owner)
        response = self.client.post(
            self.apply_url,
            {
                "decisions": [
                    {
                        "raw_name": raw_name,
                        "action": "replace",
                        "matrix_item_id": str(self.existing.id),
                        "requested_unit_label": "кг",
                    }
                    for raw_name in ("Мука пшеничная", "Мука ржаная")
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()
        self.assertIsNone(rows[0]["conflict"])
        self.assertIsNotNone(rows[1]["conflict"])
        self.assertIn(f"matrix_item_id={self.existing.id}", rows[1]["conflict"])
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.requested_name, "Мука пшеничная")

    def test_apply_denied_for_stranger(self):
        stranger = User.objects.create(email="stranger8@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
        self.auth(stranger)
        response = self.client.post(
            self.apply_url, {"decisions": [{"raw_name": "Сахар", "action": "create_new"}]}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_apply_denied_without_auth(self):
        response = self.client.post(
            self.apply_url, {"decisions": [{"raw_name": "Сахар", "action": "create_new"}]}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_preview_denied_for_service_token_only(self):
        # IsVenueMember accepts the shared service token via X-Service-Token, which
        # CoreUserJWTAuthentication never inspects — without an explicit isinstance
        # check in the view this leaked another venue's data to any token holder.
        self.client.credentials(HTTP_X_SERVICE_TOKEN="test-token")
        response = self.client.post(self.preview_url, {"raw_names": ["Сахар"]}, format="json")
        self.assertIn(response.status_code, (401, 403), response.content)

    def test_apply_denied_for_service_token_only(self):
        # Same side channel on apply: previously a 500, because AnonymousUser
        # cannot be assigned to VenueProductImportAlias.confirmed_by.
        self.client.credentials(HTTP_X_SERVICE_TOKEN="test-token")
        response = self.client.post(
            self.apply_url,
            {"decisions": [{"raw_name": "Сахар", "action": "create_new"}]},
            format="json",
        )
        self.assertIn(response.status_code, (401, 403), response.content)
        self.assertFalse(VenueProductMatrixItem.objects.filter(venue=self.venue, requested_name="Сахар").exists())

    def test_file_preview_detects_columns_and_source_fields(self):
        self.auth(self.owner)
        upload = _xlsx_upload(
            [
                ["Отчет из учетной системы"],
                ["Наименование товара", "Ед. изм.", "Примечание"],
                ["Мука в/с", "кг", "мешок"],
                ["Сахар", "кг", ""],
            ]
        )
        response = self.client.post(self.file_preview_url, {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["header_row_number"], 2)
        self.assertEqual(payload["detected_columns"]["name"], "Наименование товара")
        self.assertEqual(payload["detected_columns"]["unit"], "Ед. изм.")
        self.assertEqual(payload["rows"][0]["raw_name"], "Мука в/с")
        self.assertEqual(payload["rows"][0]["requested_unit_label"], "кг")
        # "Мука в/с" exactly matches self.existing case-insensitively, but since
        # TASK-71 (ТЗ §46) preview never picks an action for a found match — the
        # match is offered as candidates[0] and the user decides.
        self.assertIsNone(payload["rows"][0]["action"])
        self.assertEqual(payload["rows"][0]["candidates"][0]["matrix_item_id"], str(self.existing.id))
        self.assertEqual(payload["rows"][0]["matrix_item_id"], str(self.existing.id))
        self.assertFalse(payload["rows"][0]["auto_resolved"])
        self.assertEqual(payload["rows"][1]["action"], "create_new")

    def test_file_preview_llm_fills_unrecognized_unit_and_marks_source(self):
        self.auth(self.owner)

        def fake_suggest(*, rows, units):
            return {row["row_number"]: str(self.unit_kg.id) for row in rows}

        with patch("api.v1.catalog.selectors.master_catalog.suggest_venue_unit_ids", side_effect=fake_suggest):
            upload = _xlsx_upload([["Название товара", "Ед. изм."], ["Соль", "килограммы"]])
            response = self.client.post(self.file_preview_url, {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()["rows"][0]
        self.assertEqual(row["requested_unit_label"], "килограммы")
        self.assertFalse(row["unit_unrecognized"])
        self.assertEqual(row["unit_id"], str(self.unit_kg.id))
        self.assertEqual(row["unit_source"], "llm")

    def test_file_preview_unrecognized_unit_without_llm_match_has_null_source(self):
        self.auth(self.owner)
        upload = _xlsx_upload([["Название товара", "Ед. изм."], ["Соль", "неведомая единица"]])
        response = self.client.post(self.file_preview_url, {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()["rows"][0]
        self.assertTrue(row["unit_unrecognized"])
        self.assertIsNone(row["unit_id"])
        self.assertIsNone(row["unit_source"])

    def test_file_preview_then_apply_preserves_source_fields_and_autosnab_links(self):
        self.auth(self.owner)
        supplier_product = _make_supplier_product(price="100.00")
        product_list = VenueProductList.objects.create(venue=self.venue, name="Кухня")
        original_master = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука")
        self.existing.supplier_product = supplier_product
        self.existing.master_product = original_master
        self.existing.note = "старое"
        self.existing.save(update_fields=["supplier_product", "master_product", "note", "updated_at"])
        self.existing.lists.add(product_list)

        upload = _xlsx_upload([["Название товара"], ["МУКА В/С"]])
        preview = self.client.post(self.file_preview_url, {"file": upload}, format="multipart")
        self.assertEqual(preview.status_code, 200, preview.content)
        row = preview.json()["rows"][0]
        self.assertIsNone(row["action"])
        self.assertEqual(row["matrix_item_id"], str(self.existing.id))

        apply_response = self.client.post(
            self.apply_url,
            {
                "decisions": [
                    {"raw_name": row["raw_name"], "action": "update_existing", "matrix_item_id": row["matrix_item_id"]}
                ]
            },
            format="json",
        )
        self.assertEqual(apply_response.status_code, 200, apply_response.content)
        self.existing.refresh_from_db()
        # spec §6 rule 7, unchanged by this task: apply only ever writes the
        # alias — the file-based preview is a new way to *find* a row, not a
        # new way to overwrite one.
        self.assertEqual(self.existing.requested_name, "Мука в/с")
        self.assertEqual(self.existing.note, "старое")
        self.assertEqual(self.existing.supplier_product_id, supplier_product.id)
        self.assertEqual(self.existing.master_product_id, original_master.id)
        self.assertEqual(list(self.existing.lists.all()), [product_list])
        alias = VenueProductImportAlias.objects.get(venue=self.venue, raw_name=row["raw_name"])
        self.assertEqual(alias.matrix_item_id, self.existing.id)


class FindCandidateMasterProductsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner19@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.other_owner = User.objects.create(
            email="owner20@example.com", full_name="Other Owner", role=UserRole.VENUE_USER
        )
        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)

    def test_finds_close_match_by_similarity(self):
        MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA", status=EntityStatus.ACTIVE)
        candidates = find_candidate_master_products(name="AAAA", owner=self.owner)
        self.assertEqual(len(candidates), 1)
        product, score = candidates[0]
        self.assertEqual(product.canonical_name, "AAAA")
        self.assertGreater(score, 0.9)

    def test_dissimilar_name_scores_low(self):
        MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA", status=EntityStatus.ACTIVE)
        candidates = find_candidate_master_products(name="ZZZZ", owner=self.owner)
        self.assertEqual(len(candidates), 1)
        _, score = candidates[0]
        self.assertLess(score, 0.5)

    def test_backfills_missing_embedding_before_matching(self):
        mp = MasterProduct.objects.create(owner=self.owner, canonical_name="BBBB", status=EntityStatus.ACTIVE)
        self.assertFalse(MasterProduct.objects.filter(pk=mp.id, embedding__isnull=False).exists())
        find_candidate_master_products(name="BBBB", owner=self.owner)
        self.assertTrue(MasterProduct.objects.filter(pk=mp.id, embedding__isnull=False).exists())

    def test_scopes_to_owner(self):
        MasterProduct.objects.create(owner=self.other_owner, canonical_name="AAAA", status=EntityStatus.ACTIVE)
        self.assertEqual(find_candidate_master_products(name="AAAA", owner=self.owner), [])

    def test_excludes_archived(self):
        MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA", status=EntityStatus.ARCHIVED)
        self.assertEqual(find_candidate_master_products(name="AAAA", owner=self.owner), [])

    def test_respects_limit(self):
        for i in range(5):
            MasterProduct.objects.create(owner=self.owner, canonical_name=f"AAAA{i}", status=EntityStatus.ACTIVE)
        candidates = find_candidate_master_products(name="AAAA", owner=self.owner, limit=2)
        self.assertEqual(len(candidates), 2)


class ResolveMasterProductMatchesForImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner21@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Master Match")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)

    def test_already_linked_item_reports_keep_existing(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="AAAA", master_product=master_product
        )
        rows = resolve_master_product_matches_for_import(
            venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item.id]
        )
        self.assertEqual(rows[0].proposed_action, "keep_existing")
        self.assertEqual(rows[0].current_master_product_id, master_product.id)
        self.assertIsNone(rows[0].proposed_master_product_id)

    def test_close_match_proposes_link_existing(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA")
        rows = resolve_master_product_matches_for_import(
            venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item.id]
        )
        self.assertEqual(rows[0].proposed_action, "link_existing")
        self.assertEqual(rows[0].proposed_master_product_id, master_product.id)
        self.assertGreater(rows[0].score, 0.5)

    def test_no_match_proposes_create_new(self):
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Совсем новый")
        rows = resolve_master_product_matches_for_import(
            venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item.id]
        )
        self.assertEqual(rows[0].proposed_action, "create_new")
        self.assertIsNone(rows[0].proposed_master_product_id)

    def test_below_threshold_existing_candidate_still_reports_its_real_score(self):
        """Regression guard: before this plan's Task 3 rewrote this function, a
        create_new row's score was the real (below-min_score) top vector-search
        score, not None — Task 3's pending_candidates filtering (by min_score,
        for the LLM re-ranking step) must not leak into the not-found score/found
        decision, which has to keep using the unfiltered candidate list like before."""
        # "Молоко" vs "Хлеб" share a couple of characters under the tests'
        # deterministic bag-of-characters fake embedding (_fake_embedding) but are
        # not close — cosine similarity ~0.144, above 0 but below the default
        # MASTER_PRODUCT_MATCH_MIN_SCORE=0.5, i.e. a real, reportable, below-threshold match.
        MasterProduct.objects.create(owner=self.owner, canonical_name="Молоко")
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Хлеб")
        rows = resolve_master_product_matches_for_import(
            venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item.id]
        )
        self.assertEqual(rows[0].proposed_action, "create_new")
        self.assertIsNone(rows[0].proposed_master_product_id)
        self.assertIsNotNone(rows[0].score)
        self.assertGreater(rows[0].score, 0.0)
        self.assertLess(rows[0].score, 0.5)

    @override_settings(MASTER_PRODUCT_MATCH_CONFIDENT_SCORE=0.8, MASTER_PRODUCT_CANDIDATE_RANKING_ENABLED=True)
    def test_ambiguous_top_score_calls_llm_and_uses_its_top_pick(self):
        first = MasterProduct.objects.create(owner=self.owner, canonical_name="Сыр Костромской")
        second = MasterProduct.objects.create(owner=self.owner, canonical_name="Сыр Российский")
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сыр")

        def fake_rerank(*, rows):
            return {rows[0]["row_number"]: [str(second.id), str(first.id)]}

        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products",
            return_value=[(first, 0.65), (second, 0.6)],
        ):
            with patch(
                "api.v1.catalog.selectors.master_catalog.rerank_candidates", side_effect=fake_rerank
            ) as mock_rerank:
                rows = resolve_master_product_matches_for_import(
                    venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item.id]
                )
        mock_rerank.assert_called_once()
        self.assertEqual(rows[0].proposed_master_product_id, second.id)
        self.assertEqual(rows[0].proposed_action, "link_existing")

    @override_settings(MASTER_PRODUCT_MATCH_CONFIDENT_SCORE=0.8)
    def test_confident_top_score_skips_llm_entirely_stage_two(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Молоко")
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Молоко")
        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products", return_value=[(existing, 0.95)]
        ):
            with patch("api.v1.catalog.selectors.master_catalog.rerank_candidates") as mock_rerank:
                rows = resolve_master_product_matches_for_import(
                    venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item.id]
                )
        mock_rerank.assert_not_called()
        self.assertEqual(rows[0].proposed_master_product_id, existing.id)

    @override_settings(MASTER_PRODUCT_MATCH_CONFIDENT_SCORE=0.8)
    def test_llm_failure_keeps_original_top_pick_stage_two(self):
        first = MasterProduct.objects.create(owner=self.owner, canonical_name="Кофе молотый")
        second = MasterProduct.objects.create(owner=self.owner, canonical_name="Кофе зерновой")
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Кофе")
        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products",
            return_value=[(first, 0.65), (second, 0.6)],
        ):
            with patch(
                "api.v1.catalog.selectors.master_catalog.rerank_candidates", side_effect=CandidateRankingError("boom")
            ):
                rows = resolve_master_product_matches_for_import(
                    venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item.id]
                )
        self.assertEqual(rows[0].proposed_master_product_id, first.id)

    def test_already_linked_item_does_not_call_llm(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="AAAA", master_product=master_product
        )
        with patch("api.v1.catalog.selectors.master_catalog.rerank_candidates") as mock_rerank:
            resolve_master_product_matches_for_import(
                venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item.id]
            )
        mock_rerank.assert_not_called()

    def test_rejects_matrix_item_from_another_venue(self):
        other_venue = Venue.objects.create(display_name="Cafe Other")
        item = VenueProductMatrixItem.objects.create(venue=other_venue, requested_name="AAAA")
        with self.assertRaises(ValidationError):
            resolve_master_product_matches_for_import(
                venue=self.venue, owner_id=self.owner.id, matrix_item_ids=[item.id]
            )

    def test_rejects_owner_not_active_at_this_venue(self):
        stranger_owner = User.objects.create(
            email="owner22@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA")
        with self.assertRaises(ValidationError):
            resolve_master_product_matches_for_import(
                venue=self.venue, owner_id=stranger_owner.id, matrix_item_ids=[item.id]
            )


class ApplyMasterProductImportMatchesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner23@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Apply Match")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )

    def test_link_existing_sets_master_product(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA")
        rows = apply_master_product_import_matches(
            venue=self.venue,
            owner_id=self.owner.id,
            decisions=[{"matrix_item_id": item.id, "action": "link_existing", "master_product_id": master_product.id}],
        )
        item.refresh_from_db()
        self.assertEqual(item.master_product_id, master_product.id)
        self.assertEqual(rows[0].master_product_id, master_product.id)

    def test_create_new_creates_master_product_and_links_it(self):
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сахар-песок")
        rows = apply_master_product_import_matches(
            venue=self.venue, owner_id=self.owner.id, decisions=[{"matrix_item_id": item.id, "action": "create_new"}]
        )
        item.refresh_from_db()
        self.assertIsNotNone(item.master_product_id)
        master_product = MasterProduct.objects.get(pk=item.master_product_id)
        self.assertEqual(master_product.canonical_name, "Сахар-песок")
        self.assertEqual(master_product.owner_id, self.owner.id)
        self.assertEqual(rows[0].master_product_id, master_product.id)

    def test_skip_writes_nothing(self):
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Что-то")
        rows = apply_master_product_import_matches(
            venue=self.venue, owner_id=self.owner.id, decisions=[{"matrix_item_id": item.id, "action": "skip"}]
        )
        item.refresh_from_db()
        self.assertIsNone(item.master_product_id)
        self.assertIsNone(rows[0].master_product_id)

    def test_link_existing_requires_master_product_id(self):
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA")
        with self.assertRaises(ValidationError):
            apply_master_product_import_matches(
                venue=self.venue,
                owner_id=self.owner.id,
                decisions=[{"matrix_item_id": item.id, "action": "link_existing"}],
            )

    def test_link_existing_rejects_master_product_from_another_owner(self):
        other_owner = User.objects.create(email="owner24@example.com", full_name="Other", role=UserRole.VENUE_USER)
        other_master_product = MasterProduct.objects.create(owner=other_owner, canonical_name="AAAA")
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA")
        with self.assertRaises(ValidationError):
            apply_master_product_import_matches(
                venue=self.venue,
                owner_id=self.owner.id,
                decisions=[
                    {"matrix_item_id": item.id, "action": "link_existing", "master_product_id": other_master_product.id}
                ],
            )

    def test_link_existing_rejects_master_product_already_linked_to_another_item_in_venue(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA", master_product=master_product)
        other_item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA (2)")
        with self.assertRaises(ValidationError):
            apply_master_product_import_matches(
                venue=self.venue,
                owner_id=self.owner.id,
                decisions=[
                    {"matrix_item_id": other_item.id, "action": "link_existing", "master_product_id": master_product.id}
                ],
            )

    def test_rejects_matrix_item_from_another_venue(self):
        other_venue = Venue.objects.create(display_name="Cafe Other 2")
        item = VenueProductMatrixItem.objects.create(venue=other_venue, requested_name="AAAA")
        with self.assertRaises(ValidationError):
            apply_master_product_import_matches(
                venue=self.venue, owner_id=self.owner.id, decisions=[{"matrix_item_id": item.id, "action": "skip"}]
            )

    def test_link_existing_rejects_two_decisions_in_same_batch_targeting_same_master_product(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        item_a = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA")
        item_b = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA (2)")
        with self.assertRaises(ValidationError):
            apply_master_product_import_matches(
                venue=self.venue,
                owner_id=self.owner.id,
                decisions=[
                    {"matrix_item_id": item_a.id, "action": "link_existing", "master_product_id": master_product.id},
                    {"matrix_item_id": item_b.id, "action": "link_existing", "master_product_id": master_product.id},
                ],
            )
        item_a.refresh_from_db()
        item_b.refresh_from_db()
        self.assertIsNone(item_a.master_product_id)
        self.assertIsNone(item_b.master_product_id)

    def test_link_existing_clears_needs_review(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA", needs_review=True)
        apply_master_product_import_matches(
            venue=self.venue,
            owner_id=self.owner.id,
            decisions=[{"matrix_item_id": item.id, "action": "link_existing", "master_product_id": master_product.id}],
        )
        item.refresh_from_db()
        self.assertFalse(item.needs_review)

    def test_create_new_clears_needs_review(self):
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Сахар-песок", needs_review=True)
        apply_master_product_import_matches(
            venue=self.venue, owner_id=self.owner.id, decisions=[{"matrix_item_id": item.id, "action": "create_new"}]
        )
        item.refresh_from_db()
        self.assertFalse(item.needs_review)

    def test_skip_does_not_change_needs_review(self):
        item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Что-то", needs_review=True)
        apply_master_product_import_matches(
            venue=self.venue, owner_id=self.owner.id, decisions=[{"matrix_item_id": item.id, "action": "skip"}]
        )
        item.refresh_from_db()
        self.assertTrue(item.needs_review)


class VenueProductImportMasterMatchAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="owner25@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Master Match API")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        self.item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="AAAA")
        self.preview_url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/master-match/preview"
        self.apply_url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/master-match/apply"
        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_preview_proposes_link_existing(self):
        self.auth(self.owner)
        response = self.client.post(
            self.preview_url,
            {"owner_id": str(self.owner.id), "matrix_item_ids": [str(self.item.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()[0]
        self.assertEqual(row["proposed_action"], "link_existing")
        self.assertEqual(row["proposed_master_product_id"], str(self.master_product.id))

    def test_preview_does_not_write(self):
        self.auth(self.owner)
        self.client.post(
            self.preview_url, {"owner_id": str(self.owner.id), "matrix_item_ids": [str(self.item.id)]}, format="json"
        )
        self.item.refresh_from_db()
        self.assertIsNone(self.item.master_product_id)

    def test_preview_rejects_empty_matrix_item_ids(self):
        self.auth(self.owner)
        response = self.client.post(
            self.preview_url, {"owner_id": str(self.owner.id), "matrix_item_ids": []}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_apply_writes_and_returns_rows(self):
        self.auth(self.owner)
        response = self.client.post(
            self.apply_url,
            {
                "owner_id": str(self.owner.id),
                "decisions": [
                    {
                        "matrix_item_id": str(self.item.id),
                        "action": "link_existing",
                        "master_product_id": str(self.master_product.id),
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.master_product_id, self.master_product.id)

    def test_apply_rejects_link_existing_without_master_product_id(self):
        self.auth(self.owner)
        response = self.client.post(
            self.apply_url,
            {
                "owner_id": str(self.owner.id),
                "decisions": [{"matrix_item_id": str(self.item.id), "action": "link_existing"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_denied_for_stranger(self):
        stranger = User.objects.create(email="stranger9@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
        self.auth(stranger)
        response = self.client.post(
            self.preview_url, {"owner_id": str(self.owner.id), "matrix_item_ids": [str(self.item.id)]}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_denied_without_auth(self):
        response = self.client.post(
            self.preview_url, {"owner_id": str(self.owner.id), "matrix_item_ids": [str(self.item.id)]}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_preview_denied_for_service_token_only(self):
        self.client.credentials(HTTP_X_SERVICE_TOKEN="test-token")
        response = self.client.post(
            self.preview_url, {"owner_id": str(self.owner.id), "matrix_item_ids": [str(self.item.id)]}, format="json"
        )
        self.assertIn(response.status_code, (401, 403), response.content)


@override_settings(SERVICE_API_TOKEN="test-token")
class CatalogAuditLogWriteTests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.user = User.objects.create(
            email="au-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER, venue=self.venue
        )

    def test_delete_product_matrix_item_records_audit_log(self):
        item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Соль", status=EntityStatus.ACTIVE
        )
        delete_product_matrix_item(item, actor=self.user)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.VENUE_PRODUCT_MATRIX_ITEM, entity_id=item.id)
        self.assertEqual(log.action, AuditLogAction.ARCHIVED)
        self.assertEqual(log.summary, "Соль")
        self.assertEqual(log.actor_type, AuditActorType.CORE_USER)
        self.assertEqual(log.actor_id, self.user.id)

    def test_delete_master_product_records_audit_log_with_scope_id(self):
        mp = MasterProduct.objects.create(owner=self.user, canonical_name="Сахар", status=EntityStatus.ACTIVE)
        mp_id = mp.id
        delete_master_product(mp, actor=self.user)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.MASTER_PRODUCT, entity_id=mp_id)
        self.assertEqual(log.action, AuditLogAction.DELETED)
        self.assertEqual(log.summary, "Сахар")
        self.assertEqual(log.scope_id, self.user.id)

    def test_archive_price_records_audit_log(self):
        sp = _make_supplier_product(price="100.00")
        (row,) = set_supplier_product_venue_price(
            supplier_product=sp, venues=[self.venue], price=Decimal("90.00"), created_by=self.user
        )
        archive_supplier_product_venue_price(row, actor=self.user)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.SUPPLIER_PRODUCT_VENUE_PRICE, entity_id=row.id)
        self.assertEqual(log.action, AuditLogAction.ARCHIVED)
        self.assertIn(sp.normalized_name, log.summary)

    # --- 2026-09-21 final-review fix wave, Fix 4: actor was accepted but
    # never used by update/delete of a future manual price — no audit trail
    # was ever written for either action ---

    def test_update_price_records_audit_log(self):
        sp = _make_supplier_product(price="100.00")
        future_price = SupplierProductVenuePrice.objects.create(
            supplier_product=sp,
            venue=self.venue,
            price=Decimal("90.00"),
            valid_from=timezone.now() + timedelta(days=3),
            created_by=self.user,
        )
        update_supplier_product_venue_price(future_price, actor=self.user, price_value=Decimal("95.00"))
        self.assertTrue(
            AuditLog.objects.filter(
                entity_type=AuditLogEntityType.SUPPLIER_PRODUCT_VENUE_PRICE, entity_id=future_price.id
            ).exists()
        )
        log = AuditLog.objects.get(
            entity_type=AuditLogEntityType.SUPPLIER_PRODUCT_VENUE_PRICE, entity_id=future_price.id
        )
        self.assertEqual(log.action, AuditLogAction.UPDATED)
        self.assertEqual(log.actor_id, self.user.id)

    def test_delete_price_records_audit_log(self):
        sp = _make_supplier_product(price="100.00")
        future_price = SupplierProductVenuePrice.objects.create(
            supplier_product=sp,
            venue=self.venue,
            price=Decimal("90.00"),
            valid_from=timezone.now() + timedelta(days=3),
            created_by=self.user,
        )
        price_id = future_price.id
        delete_future_supplier_product_venue_price(future_price, actor=self.user)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.SUPPLIER_PRODUCT_VENUE_PRICE, entity_id=price_id)
        self.assertEqual(log.action, AuditLogAction.DELETED)
        self.assertEqual(log.actor_id, self.user.id)

    def test_delete_via_api_records_actor_from_jwt(self):
        item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Перец", status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.delete(f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/{item.id}")
        self.assertEqual(response.status_code, 204)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.VENUE_PRODUCT_MATRIX_ITEM, entity_id=item.id)
        self.assertEqual(log.actor_id, self.user.id)

    def test_delete_via_service_token_records_service_actor(self):
        item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Тмин", status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.delete(f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/{item.id}")
        self.assertEqual(response.status_code, 204)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.VENUE_PRODUCT_MATRIX_ITEM, entity_id=item.id)
        self.assertEqual(log.actor_type, AuditActorType.SERVICE)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueProductMatrixAuditLogViewTests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_shows_archive_history(self):
        self.authenticate()
        item_id = self.client.post(self.url, {"requested_name": "Куркума"}, format="json").json()["id"]
        self.client.delete(f"{self.url}/{item_id}")
        response = self.client.get(f"{self.url}/{item_id}/audit-log")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["action"], "archived")
        self.assertEqual(data[0]["summary"], "Куркума")

    def test_wrong_venue_membership_is_denied(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=_jwt_bearer(
                User.objects.create(email="stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
            )
        )
        item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Имбирь", status=EntityStatus.ACTIVE
        )
        response = self.client.get(f"{self.url}/{item.id}/audit-log")
        self.assertEqual(response.status_code, 403)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductVenuePriceAuditLogViewTests(APITestCase):
    def test_shows_archive_history_for_owner(self):
        venue = Venue.objects.create(display_name="Test Cafe")
        owner = User.objects.create(email="price-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        VenueUser.objects.create(venue=venue, user=owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        sp = _make_supplier_product(price="100.00")
        (row,) = set_supplier_product_venue_price(
            supplier_product=sp, venues=[venue], price=Decimal("90.00"), created_by=owner
        )
        archive_supplier_product_venue_price(row, actor=owner)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.get(f"/api/v1/catalog/supplier-product-venue-prices/{row.id}/audit-log")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["action"], "archived")

    def test_unrelated_user_is_denied(self):
        venue = Venue.objects.create(display_name="Test Cafe")
        owner = User.objects.create(email="price-owner-2@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        VenueUser.objects.create(venue=venue, user=owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        sp = _make_supplier_product(price="100.00")
        (row,) = set_supplier_product_venue_price(
            supplier_product=sp, venues=[venue], price=Decimal("90.00"), created_by=owner
        )
        stranger = User.objects.create(email="stranger-2@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(stranger))
        response = self.client.get(f"/api/v1/catalog/supplier-product-venue-prices/{row.id}/audit-log")
        self.assertEqual(response.status_code, 403)


class MasterProductAuditLogViewTests(APITestCase):
    def test_owner_reads_history_after_hard_delete(self):
        owner = User.objects.create(email="mp-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        mp = MasterProduct.objects.create(owner=owner, canonical_name="Ваниль", status=EntityStatus.ACTIVE)
        mp_id = mp.id
        delete_master_product(mp, actor=owner)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.get(f"/api/v1/catalog/master-products/{mp_id}/audit-log")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["action"], "deleted")
        self.assertEqual(data[0]["summary"], "Ваниль")

    def test_different_user_denied_after_hard_delete(self):
        owner = User.objects.create(email="mp-owner-2@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        mp = MasterProduct.objects.create(owner=owner, canonical_name="Корица", status=EntityStatus.ACTIVE)
        mp_id = mp.id
        delete_master_product(mp, actor=owner)
        stranger = User.objects.create(email="mp-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(stranger))
        response = self.client.get(f"/api/v1/catalog/master-products/{mp_id}/audit-log")
        self.assertEqual(response.status_code, 403)

    def test_bad_id_with_no_history_is_denied(self):
        owner = User.objects.create(email="mp-owner-3@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.get(f"/api/v1/catalog/master-products/{uuid4()}/audit-log")
        self.assertEqual(response.status_code, 403)

    def test_owner_reads_history_while_still_live(self):
        owner = User.objects.create(email="mp-owner-4@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        mp = MasterProduct.objects.create(owner=owner, canonical_name="Кориандр", status=EntityStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.get(f"/api/v1/catalog/master-products/{mp.id}/audit-log")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


# ---------------------------------------------------------------------------
# supplier_product_deleted flag on serializers
# ---------------------------------------------------------------------------


class SupplierProductDeletedFieldSerializerTests(APITestCase):
    def test_matrix_item_serializer_exposes_flag(self):
        venue = Venue.objects.create(display_name="Кафе Сериализатор")
        sp = _make_supplier_product(price="10.00")
        item = VenueProductMatrixItem.objects.create(
            venue=venue, requested_name="Тест", supplier_product=sp, status=EntityStatus.ACTIVE
        )
        self.assertFalse(VenueProductMatrixItemSerializer(item).data["supplier_product_deleted"])
        item.supplier_product = None
        item.supplier_product_deleted = True
        item.save(update_fields=["supplier_product", "supplier_product_deleted"])
        self.assertTrue(VenueProductMatrixItemSerializer(item).data["supplier_product_deleted"])


class CreateProductMatrixItemAutoMatchTests(TestCase):
    """TASK-38 / §11.1 raw ТЗ: manually adding a company-catalog item
    outside the import flow auto-resolves against the owner's master
    catalog."""

    def setUp(self):
        self.owner = User.objects.create(
            email="owner-automatch1@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Auto Match")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)

    def test_auto_links_high_confidence_match(self):
        master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        item = create_product_matrix_item(venue=self.venue, requested_name="AAAA")
        self.assertEqual(item.master_product_id, master_product.id)
        self.assertFalse(item.needs_review)

    def test_auto_creates_when_no_candidate_at_all(self):
        item = create_product_matrix_item(venue=self.venue, requested_name="Совсем новый товар")
        self.assertIsNotNone(item.master_product_id)
        self.assertEqual(item.master_product.canonical_name, "Совсем новый товар")
        self.assertEqual(item.master_product.owner_id, self.owner.id)
        self.assertFalse(item.needs_review)

    @override_settings(MASTER_PRODUCT_MATCH_MIN_SCORE=0.999)
    def test_marks_needs_review_when_candidate_below_threshold(self):
        MasterProduct.objects.create(owner=self.owner, canonical_name="AAAB")
        item = create_product_matrix_item(venue=self.venue, requested_name="AAAA")
        self.assertIsNone(item.master_product_id)
        self.assertTrue(item.needs_review)

    def test_skips_auto_match_when_venue_has_no_single_owner(self):
        second_owner = User.objects.create(
            email="owner-automatch2@example.com", full_name="Owner 2", role=UserRole.VENUE_USER
        )
        VenueUser.objects.create(
            venue=self.venue, user=second_owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        item = create_product_matrix_item(venue=self.venue, requested_name="AAAA")
        self.assertIsNone(item.master_product_id)
        self.assertFalse(item.needs_review)
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 1)


class CreateProductMatrixItemAutoMatchConcurrencyTests(TransactionTestCase):
    """TASK-45 step 5/7 — see WishlistConcurrencyTests above for why
    TransactionTestCase + real threads is needed. Two concurrent adds of the
    same name race to claim the same auto-matched MasterProduct; without
    serializing per-venue, both can pass "not already linked" before either
    commits, and the second's INSERT then violates
    vpmi_master_product_venue_uniq with an unhandled IntegrityError (500).
    The sequential regression test in CreateProductMatrixItemDuplicateAutoLinkAPITests
    cannot catch this because its two calls never overlap in time."""

    def setUp(self):
        self.owner = User.objects.create(
            email="owner-automatch-race@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Auto Match Race")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.patcher_single = patch(
            "api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding
        )
        self.patcher_batch = patch(
            "api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings
        )
        self.patcher_single.start()
        self.patcher_batch.start()
        self.addCleanup(self.patcher_single.stop)
        self.addCleanup(self.patcher_batch.stop)

    def test_two_concurrent_adds_never_crash_and_never_double_link(self):
        results = []
        errors = []

        def worker():
            connections.close_all()
            try:
                item = create_product_matrix_item(venue=self.venue, requested_name="Молоко")
                results.append(item)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"auto-match crashed under concurrency: {errors}")
        self.assertEqual(len(results), 2)
        linked = [item for item in results if item.master_product_id is not None]
        self.assertEqual(len(linked), 1, "exactly one of the two rows should win the MasterProduct link")
        needing_review = [item for item in results if item.needs_review]
        self.assertEqual(len(needing_review), 1)


class LinkMasterProductToVenueConcurrencyTests(TransactionTestCase):
    """TASK-46: link_master_product_to_venue (manual PUT-linking path) took
    no select_for_update() on Venue, unlike
    _auto_assign_master_products_to_new_items (auto-match path, TASK-45).
    A concurrent manual link and auto-match for the same (master_product,
    venue) slot could each pass their own occupancy check before either
    commits, and one .create() then violates vpmi_master_product_venue_uniq
    with an unhandled IntegrityError (500). See
    CreateProductMatrixItemAutoMatchConcurrencyTests above for why
    TransactionTestCase + real threads is needed to catch this."""

    def setUp(self):
        self.owner = User.objects.create(
            email="owner-link-race@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Link Race")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.mp = create_master_product(owner=self.owner, canonical_name="Молоко")
        self.patcher_single = patch(
            "api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding
        )
        self.patcher_batch = patch(
            "api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings
        )
        self.patcher_single.start()
        self.patcher_batch.start()
        self.addCleanup(self.patcher_single.stop)
        self.addCleanup(self.patcher_batch.stop)

    def test_manual_link_races_auto_match_without_crashing_or_double_linking(self):
        errors = []

        def link_worker():
            connections.close_all()
            try:
                link_master_product_to_venue(master_product=self.mp, venue=self.venue, force_create=True)
            except ValidationError:
                pass  # expected outcome if this side loses the race
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        def auto_match_worker():
            connections.close_all()
            try:
                create_product_matrix_item(venue=self.venue, requested_name="Молоко")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=link_worker), threading.Thread(target=auto_match_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"linking crashed under concurrency: {errors}")
        linked = VenueProductMatrixItem.objects.filter(
            venue=self.venue, master_product=self.mp, status=EntityStatus.ACTIVE
        )
        self.assertEqual(linked.count(), 1, "exactly one row should hold the (master_product, venue) slot")


class ApplyMasterProductImportMatchesConcurrencyTests(TransactionTestCase):
    """TASK-100: apply_master_product_import_matches linked MasterProduct to
    VenueProductMatrixItem under the same vpmi_master_product_venue_uniq
    constraint as _auto_assign_master_products_to_new_items, but with no
    select_for_update() — only an in-process claimed_by dict scoped to one
    call. Two concurrent calls linking the same master_product_id to two
    different matrix items of the same venue both pass their own claimed_by
    check and the second bulk_update raises an unhandled IntegrityError.
    See LinkMasterProductToVenueConcurrencyTests above for why
    TransactionTestCase + real threads is needed to catch this."""

    def setUp(self):
        self.owner = User.objects.create(
            email="owner-match-apply-race@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Match Apply Race")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.mp = MasterProduct.objects.create(owner=self.owner, canonical_name="Молоко")
        self.item_a = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Молоко пакет А")
        self.item_b = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Молоко пакет Б")

    def test_two_concurrent_link_calls_for_same_master_product_never_crash_and_never_double_link(self):
        from api.v1.catalog.services import apply_master_product_import_matches

        errors = []

        def worker(item):
            connections.close_all()
            try:
                apply_master_product_import_matches(
                    venue=self.venue,
                    owner_id=self.owner.id,
                    decisions=[{"matrix_item_id": item.id, "action": "link_existing", "master_product_id": self.mp.id}],
                )
            except ValidationError:
                pass  # expected outcome if this side loses the race
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=worker, args=(self.item_a,)),
            threading.Thread(target=worker, args=(self.item_b,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"linking crashed under concurrency: {errors}")
        linked = VenueProductMatrixItem.objects.filter(venue=self.venue, master_product=self.mp)
        self.assertEqual(linked.count(), 1, "exactly one item should hold the (master_product, venue) slot")


class VenueProductMatrixFilterAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe Filter")
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix"
        self.plain = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Обычный товар")
        self.needs_review = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Спорный товар", needs_review=True
        )
        master_product = MasterProduct.objects.create(
            owner=User.objects.create(email="owner-filter1@example.com", full_name="Owner", role=UserRole.VENUE_USER),
            canonical_name="Мастер-товар",
        )
        self.linked = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Привязанный товар", master_product=master_product
        )

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_default_returns_all(self):
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row["id"] for row in response.json()["results"]},
            {str(self.plain.id), str(self.needs_review.id), str(self.linked.id)},
        )

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_filter_needs_review(self):
        self.authenticate()
        response = self.client.get(self.url, {"filter": "needs_review"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(self.needs_review.id)])

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_filter_no_master_product(self):
        self.authenticate()
        response = self.client.get(self.url, {"filter": "no_master_product"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row["id"] for row in response.json()["results"]}, {str(self.plain.id), str(self.needs_review.id)}
        )

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_invalid_filter_value_returns_400(self):
        self.authenticate()
        response = self.client.get(self.url, {"filter": "not-a-real-choice"})
        self.assertEqual(response.status_code, 400)

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_search_by_q_filters_by_name_substring(self):
        self.authenticate()
        response = self.client.get(self.url, {"q": "Спорный"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(self.needs_review.id)])

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_search_by_q_is_case_insensitive(self):
        self.authenticate()
        response = self.client.get(self.url, {"q": "спорный"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(self.needs_review.id)])

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_search_by_q_no_match_returns_empty(self):
        self.authenticate()
        response = self.client.get(self.url, {"q": "Несуществующий"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_search_by_q_combines_with_filter(self):
        self.authenticate()
        response = self.client.get(self.url, {"q": "товар", "filter": "no_master_product"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row["id"] for row in response.json()["results"]}, {str(self.plain.id), str(self.needs_review.id)}
        )

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_search_by_q_matches_linked_master_product_name(self):
        self.authenticate()

        response = self.client.get(self.url, {"q": "мастер"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(self.linked.id)])

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_search_by_q_matches_words_in_any_order(self):
        target = VenueProductMatrixItem.objects.create(
            venue=self.venue,
            requested_name="Мука пшеничная высший сорт",
        )
        VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука рисовая")
        self.authenticate()

        response = self.client.get(self.url, {"q": "пшеничная мука"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(target.id)])

    @override_settings(SERVICE_API_TOKEN="test-token")
    @skipUnless(connection.vendor == "postgresql", "pg_trgm is PostgreSQL-only")
    def test_search_by_q_tolerates_typo(self):
        target = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Мука пшеничная")
        self.authenticate()

        response = self.client.get(self.url, {"q": "пшенична"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [str(target.id)])

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_blank_q_is_ignored(self):
        self.authenticate()
        response = self.client.get(self.url, {"q": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row["id"] for row in response.json()["results"]},
            {str(self.plain.id), str(self.needs_review.id), str(self.linked.id)},
        )


class VenueProductMatrixPaginationAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe Pagination")
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_list_response_is_paginated_envelope(self):
        VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Товар")
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data.keys()), {"count", "next", "previous", "results"})
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["requested_name"], "Товар")

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_list_default_page_size_is_20(self):
        for i in range(25):
            VenueProductMatrixItem.objects.create(venue=self.venue, requested_name=f"Товар {i:02d}")
        self.authenticate()
        response = self.client.get(self.url)
        data = response.json()
        self.assertEqual(data["count"], 25)
        self.assertEqual(len(data["results"]), 20)
        self.assertIsNotNone(data["next"])

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_list_second_page_returns_remaining_items(self):
        for i in range(25):
            VenueProductMatrixItem.objects.create(venue=self.venue, requested_name=f"Товар {i:02d}")
        self.authenticate()
        response = self.client.get(self.url, {"page": "2"})
        data = response.json()
        self.assertEqual(len(data["results"]), 5)
        self.assertIsNone(data["next"])

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_list_page_size_query_param_overrides_default(self):
        for i in range(5):
            VenueProductMatrixItem.objects.create(venue=self.venue, requested_name=f"Товар {i}")
        self.authenticate()
        response = self.client.get(self.url, {"page_size": "2"})
        data = response.json()
        self.assertEqual(data["count"], 5)
        self.assertEqual(len(data["results"]), 2)


class VenueProductMatrixPriceAPITests(APITestCase):
    """TASK-44 / §35 raw ТЗ: GET .../product-matrix must show the active
    individual (per-venue) price where one covers this venue, falling back
    to the supplier's base current_price otherwise — not always the base
    price regardless of an individual override."""

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe Price")
        self.other_venue = Venue.objects.create(display_name="Cafe Price Other")
        self.owner = User.objects.create(
            email="owner-matrixprice1@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.supplier = Supplier.objects.create(display_name="Поставщик Цена")
        self.supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Сахар песок 50кг",
            normalized_name="Сахар песок 50кг",
            current_price=Decimal("2000.00"),
        )
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_falls_back_to_base_price_without_individual_price(self):
        VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Сахар", supplier_product=self.supplier_product
        )
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["price"], 2000.0)

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_shows_active_individual_price_for_this_venue(self):
        VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Сахар", supplier_product=self.supplier_product
        )
        SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("1800.00"), created_by=self.owner
        )
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["price"], 1800.0)

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_ignores_archived_individual_price(self):
        VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Сахар", supplier_product=self.supplier_product
        )
        SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product,
            venue=self.venue,
            price=Decimal("1800.00"),
            created_by=self.owner,
            status=SupplierProductVenuePriceStatus.ARCHIVED,
        )
        self.authenticate()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["price"], 2000.0)

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_individual_price_does_not_leak_to_other_venue(self):
        VenueProductMatrixItem.objects.create(
            venue=self.other_venue, requested_name="Сахар", supplier_product=self.supplier_product
        )
        SupplierProductVenuePrice.objects.create(
            supplier_product=self.supplier_product, venue=self.venue, price=Decimal("1800.00"), created_by=self.owner
        )
        self.authenticate()
        response = self.client.get(f"/api/v1/catalog/venues/{self.other_venue.id}/product-matrix")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["price"], 2000.0)


class ApplyVenueImportMissingDetectionTests(TestCase):
    """TASK-40 / §30 raw ТЗ: an active accounting_system-sourced item not
    present in a new import batch gets flagged, never auto-removed."""

    def setUp(self):
        self.owner = User.objects.create(
            email="owner-missing1@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Missing Import")

    def test_flags_accounting_item_absent_from_batch(self):
        stale = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Кетчуп", source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM
        )
        apply_venue_import(
            venue=self.venue, user=self.owner, decisions=[{"raw_name": "Майонез", "action": "create_new"}]
        )
        stale.refresh_from_db()
        self.assertTrue(stale.missing_from_latest_import)

    def test_resets_flag_when_item_reappears(self):
        item = VenueProductMatrixItem.objects.create(
            venue=self.venue,
            requested_name="Кетчуп",
            source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM,
            missing_from_latest_import=True,
        )
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[{"raw_name": "Кетчуп", "action": "update_existing", "matrix_item_id": item.id}],
        )
        item.refresh_from_db()
        self.assertFalse(item.missing_from_latest_import)

    def test_does_not_flag_manual_source_items(self):
        manual_item = VenueProductMatrixItem.objects.create(venue=self.venue, requested_name="Соль")
        apply_venue_import(
            venue=self.venue, user=self.owner, decisions=[{"raw_name": "Майонез", "action": "create_new"}]
        )
        manual_item.refresh_from_db()
        self.assertFalse(manual_item.missing_from_latest_import)

    def test_does_not_flag_other_venues_items(self):
        other_venue = Venue.objects.create(display_name="Cafe Other Missing")
        other_item = VenueProductMatrixItem.objects.create(
            venue=other_venue, requested_name="Кетчуп", source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM
        )
        apply_venue_import(
            venue=self.venue, user=self.owner, decisions=[{"raw_name": "Майонез", "action": "create_new"}]
        )
        other_item.refresh_from_db()
        self.assertFalse(other_item.missing_from_latest_import)

    def test_present_matrix_item_ids_protects_undecided_rows_from_missing_flag(self):
        # A partial apply (TASK-74): the file also contains "Кетчуп", still
        # undecided in this call — the client passes its matched item id in
        # present_matrix_item_ids so it isn't wrongly flagged missing.
        pending = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Кетчуп", source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM
        )
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[{"raw_name": "Майонез", "action": "create_new"}],
            present_matrix_item_ids=[pending.id],
        )
        pending.refresh_from_db()
        self.assertFalse(pending.missing_from_latest_import)

    def test_present_matrix_item_ids_defaults_to_old_behavior(self):
        stale = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Кетчуп", source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM
        )
        apply_venue_import(
            venue=self.venue, user=self.owner, decisions=[{"raw_name": "Майонез", "action": "create_new"}]
        )
        stale.refresh_from_db()
        self.assertTrue(stale.missing_from_latest_import)

    def test_present_matrix_item_ids_does_not_unflag_out_of_scope_items(self):
        # present_matrix_item_ids is caller-supplied and unvalidated — an id
        # for an item outside this venue/status/source scope must not be
        # touched by the un-flag write, even though it lands in touched_ids.
        other_venue = Venue.objects.create(display_name="Cafe Other Missing 2")
        foreign_item = VenueProductMatrixItem.objects.create(
            venue=other_venue,
            requested_name="Кетчуп",
            source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM,
            missing_from_latest_import=True,
        )
        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[{"raw_name": "Майонез", "action": "create_new"}],
            present_matrix_item_ids=[foreign_item.id],
        )
        foreign_item.refresh_from_db()
        self.assertTrue(foreign_item.missing_from_latest_import)

    def test_present_matrix_item_ids_protects_item_created_by_a_previous_partial_apply(self):
        # Final-review finding: present_matrix_item_ids' documented contract
        # across a SEQUENCE of partial apply calls for the same file isn't
        # limited to ids from the original preview — it must also include
        # matrix_item_id from any PREVIOUS apply call's response in this same
        # sequence. Call 1 creates "Кетчуп" via create_new — it has no
        # preview id at all, since it didn't exist before this import. Call 2
        # decides a different row ("Майонез") and, per the documented usage,
        # passes the id returned by call 1 to protect the still-undecided
        # "Кетчуп" from being swept up as missing.
        call_1_rows = apply_venue_import(
            venue=self.venue, user=self.owner, decisions=[{"raw_name": "Кетчуп", "action": "create_new"}]
        )
        created_item_id = call_1_rows[0].matrix_item_id
        self.assertIsNotNone(created_item_id)

        apply_venue_import(
            venue=self.venue,
            user=self.owner,
            decisions=[{"raw_name": "Майонез", "action": "create_new"}],
            present_matrix_item_ids=[created_item_id],
        )

        created_item = VenueProductMatrixItem.objects.get(pk=created_item_id)
        self.assertFalse(created_item.missing_from_latest_import)


class VenueImportTrigramIndexTests(TestCase):
    def test_gin_trgm_index_exists_on_requested_name(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = %s AND indexname = %s",
                [VenueProductMatrixItem._meta.db_table, "vpmi_requested_name_trgm_gin"],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row, "vpmi_requested_name_trgm_gin index not found")
        self.assertIn("gin_trgm_ops", row[0])
        self.assertIn("USING gin", row[0])


class MasterProductBulkCreateAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="owner-bulkcreate1@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.stranger = User.objects.create(
            email="stranger-bulkcreate1@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.url = "/api/v1/catalog/master-products/bulk"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_creates_all_new_rows(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "items": [{"canonical_name": "Мука"}, {"canonical_name": "Сахар"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        results = response.json()["results"]
        self.assertEqual([r["outcome"] for r in results], ["created", "created"])
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 2)

    def test_skips_case_insensitive_duplicate(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука")
        self.auth(self.owner)
        response = self.client.post(
            self.url, {"owner_id": str(self.owner.id), "items": [{"canonical_name": "МУКА"}]}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        results = response.json()["results"]
        self.assertEqual(results[0]["outcome"], "skipped_duplicate")
        self.assertEqual(results[0]["master_product_id"], str(existing.id))
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 1)

    def test_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.post(
            self.url, {"owner_id": str(self.owner.id), "items": [{"canonical_name": "Мука"}]}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_empty_items_rejected(self):
        self.auth(self.owner)
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "items": []}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_over_limit_batch_rejected(self):
        """Final whole-branch review fix wave (Finding 3, Important):
        unbounded batch size on the bulk-create endpoint — capped at 500."""
        self.auth(self.owner)
        items = [{"canonical_name": f"Товар {i}"} for i in range(501)]
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "items": items}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 0)


class MasterProductBulkDeleteAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="owner-bulkdelete1@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.stranger = User.objects.create(
            email="stranger-bulkdelete1@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.mp1 = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука")
        self.mp2 = MasterProduct.objects.create(owner=self.owner, canonical_name="Сахар")
        self.other_owner = User.objects.create(
            email="other-owner-bulkdelete1@example.com", full_name="Other Owner", role=UserRole.VENUE_USER
        )
        self.foreign_mp = MasterProduct.objects.create(owner=self.other_owner, canonical_name="Чужой")
        self.url = "/api/v1/catalog/master-products/bulk-delete"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_deletes_all_owned_rows(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "master_product_ids": [str(self.mp1.id), str(self.mp2.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        results = response.json()["results"]
        self.assertEqual([r["outcome"] for r in results], ["deleted", "deleted"])
        self.assertEqual(MasterProduct.objects.filter(pk__in=[self.mp1.id, self.mp2.id]).count(), 0)

    def test_reports_not_found_for_foreign_id_without_deleting_it(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "master_product_ids": [str(self.mp1.id), str(self.foreign_mp.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        results = {r["master_product_id"]: r["outcome"] for r in response.json()["results"]}
        self.assertEqual(results[str(self.mp1.id)], "deleted")
        self.assertEqual(results[str(self.foreign_mp.id)], "not_found")
        self.assertTrue(MasterProduct.objects.filter(pk=self.foreign_mp.id).exists())

    def test_denied_for_stranger(self):
        self.auth(self.stranger)
        response = self.client.post(
            self.url, {"owner_id": str(self.owner.id), "master_product_ids": [str(self.mp1.id)]}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(MasterProduct.objects.filter(pk=self.mp1.id).exists())

    def test_duplicate_id_in_batch_deletes_once(self):
        self.auth(self.owner)
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "master_product_ids": [str(self.mp1.id), str(self.mp1.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([r["outcome"] for r in response.json()["results"]], ["deleted", "deleted"])
        self.assertEqual(MasterProduct.objects.filter(pk=self.mp1.id).count(), 0)

    def test_over_limit_batch_rejected(self):
        """Final whole-branch review fix wave (Finding 3, Important):
        unbounded batch size on the bulk-delete endpoint — highest
        blast-radius endpoint in this plan (hard delete, single atomic
        transaction) — capped at 500."""
        self.auth(self.owner)
        ids = [str(uuid.uuid4()) for _ in range(501)]
        response = self.client.post(
            self.url, {"owner_id": str(self.owner.id), "master_product_ids": ids}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(MasterProduct.objects.filter(pk=self.mp1.id).exists())


class HasMasterCatalogAccessToOwnerMultipartTests(TestCase):
    """Task 4 (TASK-45): request.data for a multipart upload is a QueryDict,
    not a dict — isinstance(request.data, dict) is False for it, so the
    owner_id check used to be skipped entirely (silent 403) for any
    multipart POST, e.g. MasterProductImportView."""

    def test_grants_access_when_owner_id_is_in_a_querydict_body(self):
        from django.http import QueryDict

        from api.v1.permissions import HasMasterCatalogAccessToOwner

        owner = User.objects.create(email="mapping-fix@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        request = RequestFactory().post("/api/v1/catalog/master-products/import")
        request.user = owner
        request.data = QueryDict(f"owner_id={owner.id}")
        self.assertTrue(HasMasterCatalogAccessToOwner().has_permission(request, view=None))


class CreateProductMatrixItemDuplicateAutoLinkAPITests(APITestCase):
    """Final whole-branch review fix wave (Finding 1, Critical): POSTing the
    same requested_name twice into a venue's product matrix used to crash
    with an unhandled IntegrityError (500) on the second call — the second
    item's embedding-similarity search finds the SAME MasterProduct the
    first call just auto-linked (or auto-created), and
    vpmi_master_product_venue_uniq (catalog/models.py) has no status filter,
    so the second .save() violates the partial-unique constraint. Goes
    through the real HTTP endpoint (unlike the service-level
    CreateProductMatrixItemAutoMatchTests, which mocks the embedding
    boundary but never exercises the view)."""

    def setUp(self):
        self.owner = User.objects.create(
            email="owner-dupautolink1@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Dup Auto Link")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix"
        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def test_second_identical_add_does_not_crash_and_flags_needs_review(self):
        first = self.client.post(self.url, {"requested_name": "Молоко"}, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        first_master_product_id = first.json()["master_product_id"]
        self.assertIsNotNone(first_master_product_id)

        second = self.client.post(self.url, {"requested_name": "Молоко"}, format="json")
        self.assertEqual(second.status_code, 201, second.content)
        body = second.json()
        self.assertIsNone(body["master_product_id"])
        self.assertTrue(body["needs_review"])


class ParseCatalogFileTests(TestCase):
    def test_detects_header_row_after_preamble_and_maps_columns(self):
        from api.v1.catalog.importers import parse_catalog_file

        upload = _xlsx_upload(
            [
                ["Отчёт из учётной системы, экспорт 2026-09-05"],
                ["Наименование товара", "Ед. изм.", "Вес нетто", "Примечание"],
                ["Мука в/с", "кг", "1", "мешок"],
                ["Сахар", "кг", "", ""],
            ]
        )
        parsed = parse_catalog_file(upload)
        self.assertEqual(parsed.header_row_number, 2)
        self.assertEqual(parsed.detected_columns["name"], "Наименование товара")
        self.assertEqual(parsed.detected_columns["unit"], "Ед. изм.")
        self.assertEqual(len(parsed.rows), 2)
        self.assertEqual(parsed.rows[0]["name"], "Мука в/с")
        self.assertEqual(parsed.rows[0]["unit"], "кг")
        self.assertEqual(parsed.rows[0]["note"], "мешок")
        self.assertNotIn("note", parsed.rows[1])

    def test_headerless_single_column_falls_back_to_name(self):
        from api.v1.catalog.importers import parse_catalog_file

        upload = _xlsx_upload([["Мука в/с"], ["Сахар"]])
        parsed = parse_catalog_file(upload)
        self.assertIsNone(parsed.header_row_number)
        self.assertEqual([row["name"] for row in parsed.rows], ["Мука в/с", "Сахар"])

    def test_rows_without_name_are_counted_not_silently_dropped(self):
        from api.v1.catalog.importers import parse_catalog_file

        upload = _xlsx_upload([["Название"], ["Мука в/с"], [""], ["Сахар"]])
        parsed = parse_catalog_file(upload)
        self.assertEqual(len(parsed.rows), 2)
        self.assertEqual(parsed.rows_without_name, 1)

    def test_csv_semicolon_delimiter_is_detected(self):
        from api.v1.catalog.importers import parse_catalog_file

        content = "Название;Единица\nМука в/с;кг\n".encode("utf-8-sig")
        upload = SimpleUploadedFile("catalog.csv", content, content_type="text/csv")
        parsed = parse_catalog_file(upload)
        self.assertEqual(parsed.rows[0]["name"], "Мука в/с")
        self.assertEqual(parsed.rows[0]["unit"], "кг")

    def test_txt_is_extracted_through_shared_file_import_layer(self):
        from api.v1.catalog.importers import parse_catalog_file

        upload = SimpleUploadedFile(
            "catalog.txt",
            "Мука в/с\nСахар\n".encode("utf-8"),
            content_type="text/plain",
        )

        parsed = parse_catalog_file(upload)

        self.assertEqual([row["name"] for row in parsed.rows], ["Мука в/с", "Сахар"])

    def test_rejects_empty_file(self):
        from api.v1.catalog.importers import parse_catalog_file

        upload = SimpleUploadedFile("empty.csv", b"", content_type="text/csv")
        with self.assertRaises(ValidationError):
            parse_catalog_file(upload)

    def test_rejects_unsupported_extension(self):
        from api.v1.catalog.importers import parse_catalog_file

        upload = SimpleUploadedFile("catalog.xls", b"not really xls", content_type="application/vnd.ms-excel")
        with self.assertRaises(ValidationError):
            parse_catalog_file(upload)

    def test_rejects_file_with_no_name_column_detected_and_no_data(self):
        from api.v1.catalog.importers import parse_catalog_file

        upload = _xlsx_upload([[None, None], [None, None]])
        with self.assertRaises(ValidationError):
            parse_catalog_file(upload)

    def test_raw_row_ceiling_raises_instead_of_silently_truncating(self):
        # Regression test: the raw-row OOM-safety ceiling used to `break` out
        # of the read loop silently once hit, while parse_catalog_file's
        # "too many rows" check only counts parsed (named) rows. A file with
        # enough blank rows between named rows could have its raw reading
        # truncated below the ceiling before all named rows were ever read,
        # while the parsed-row count never crossed max_rows — so the file
        # "imported successfully" with rows silently missing instead of
        # raising the intended error. With max_rows=3 the ceiling is
        # 3 + HEADER_SCAN_ROWS(20) + 5 = 28 raw rows. This file has a header
        # row, one named row, then 27 blank rows (pushing the raw count to
        # 29, past the ceiling) before 4 more named rows — so only "Товар 1"
        # would ever be read under the old silent-break behaviour, well
        # under max_rows, and no error would be raised.
        from api.v1.catalog.importers import parse_catalog_file

        rows = [["Название"], ["Товар 1"]]
        rows.extend([[None]] * 27)
        rows.extend([["Товар 2"], ["Товар 3"], ["Товар 4"], ["Товар 5"]])
        upload = _xlsx_upload(rows)

        with self.assertRaises(ValidationError) as ctx:
            parse_catalog_file(upload, max_rows=3)
        self.assertIn("Слишком много строк", str(ctx.exception))

    def test_trailing_blank_formatting_rows_do_not_trip_row_ceiling(self):
        # Regression test for a real accounting-system export: Excel can
        # report a used range (ws.max_row) far past the last row with actual
        # data, purely from leftover cell formatting. The raw-row ceiling
        # used to count every physical row from iter_rows(), including that
        # blank tail, so a file with only a handful of real products could
        # still be rejected as "too many rows" solely because of formatting
        # bloat unrelated to its content.
        from api.v1.catalog.importers import parse_catalog_file

        rows = [["Название"], ["Товар 1"], ["Товар 2"]]
        rows.extend([[None]] * 40)
        upload = _xlsx_upload(rows)

        parsed = parse_catalog_file(upload, max_rows=5)

        self.assertEqual([row["name"] for row in parsed.rows], ["Товар 1", "Товар 2"])

    def test_header_with_source_system_suffix_is_recognized(self):
        # Regression test: accounting-system (УС) exports commonly label
        # columns "Наименование товара в УС" / "Ед. изм. в УС" — the trailing
        # "в УС" qualifier meant no alias ever matched, so the header row
        # fell through to headerless fallback and got parsed as a phantom
        # product instead of being recognized and skipped.
        from api.v1.catalog.importers import parse_catalog_file

        upload = _xlsx_upload(
            [
                ["Наименование товара в УС", "Ед. изм. в УС", "Код товара УС"],
                ["Мука в/с", "кг", 183],
                ["Сахар", "кг", 184],
            ]
        )
        parsed = parse_catalog_file(upload)

        self.assertEqual(parsed.header_row_number, 1)
        self.assertEqual([row["name"] for row in parsed.rows], ["Мука в/с", "Сахар"])
        self.assertEqual(parsed.rows[0]["unit"], "кг")


class MasterProductImportApplyAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="import-apply-api@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.unit = Unit.objects.create(code="kg", name="Килограмм", kind="weight", status=EntityStatus.ACTIVE)
        self.url = "/api/v1/catalog/master-products/import/apply"

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def test_apply_creates_new_master_product(self):
        self.auth()
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "decisions": [{"raw_name": "Манго пюре", "action": "create_new", "unit_id": str(self.unit.id)}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["rows"][0]["action"], "create_new")
        self.assertTrue(MasterProduct.objects.filter(owner=self.owner, canonical_name="Манго пюре").exists())

    def test_apply_update_existing_links_without_overwriting(self):
        self.auth()
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки", note="исходное")
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "decisions": [
                    {"raw_name": "Сливки 33", "action": "update_existing", "master_product_id": str(existing.id)}
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["rows"][0]["master_product_id"], str(existing.id))
        existing.refresh_from_db()
        self.assertEqual(existing.note, "исходное")

    def test_apply_update_existing_without_master_product_id_is_400(self):
        self.auth()
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "decisions": [{"raw_name": "Товар", "action": "update_existing"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_apply_create_new_with_unknown_unit_id_is_400(self):
        self.auth()
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "decisions": [{"raw_name": "Товар", "action": "create_new", "unit_id": str(uuid.uuid4())}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MasterProduct.objects.filter(owner=self.owner, canonical_name="Товар").exists())

    def test_apply_denied_for_stranger_owner_id(self):
        stranger = User.objects.create(
            email="import-apply-api-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(stranger))
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "decisions": [{"raw_name": "Товар", "action": "create_new"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_old_direct_write_endpoint_is_gone(self):
        self.auth()
        upload = _xlsx_upload([["Название"], ["Товар"]])
        response = self.client.post(
            "/api/v1/catalog/master-products/import",
            {"owner_id": str(self.owner.id), "file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, 404)

    def test_apply_replace_returns_new_values(self):
        self.auth()
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "decisions": [
                    {
                        "raw_name": "Сливки 33%",
                        "action": "replace",
                        "master_product_id": str(existing.id),
                        "unit_id": str(self.unit.id),
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()["rows"][0]
        self.assertEqual(row["action"], "replace")
        self.assertEqual(row["master_product_id"], str(existing.id))
        self.assertEqual(row["canonical_name"], "Сливки 33%")
        self.assertEqual(row["unit_id"], str(self.unit.id))
        entry = AuditLog.objects.get(entity_id=existing.id, action=AuditLogAction.REPLACED)
        self.assertEqual(entry.actor_id, self.owner.id)

    def test_apply_replace_without_unit_id_is_400_with_field_error(self):
        self.auth()
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "decisions": [{"raw_name": "Сливки 33%", "action": "replace", "master_product_id": str(existing.id)}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["decisions"][0]["unit_id"], ["Обязателен при action=replace."])

    def _replace(self, raw_name, master_product_id):
        return {
            "raw_name": raw_name,
            "action": "replace",
            "master_product_id": str(master_product_id),
            "unit_id": str(self.unit.id),
        }

    def test_apply_replace_foreign_or_archived_target_error_is_a_list(self):
        self.auth()
        stranger = User.objects.create(email="import-apply-api-foreign@example.com", role=UserRole.VENUE_USER)
        foreign = MasterProduct.objects.create(owner=stranger, canonical_name="Чужие сливки")
        archived = MasterProduct.objects.create(
            owner=self.owner, canonical_name="Архивные сливки", status=EntityStatus.ARCHIVED
        )
        for target in (foreign, archived):
            with self.subTest(target=target.canonical_name):
                response = self.client.post(
                    self.url,
                    {"owner_id": str(self.owner.id), "decisions": [self._replace("Сливки 33%", target.id)]},
                    format="json",
                )
                self.assertEqual(response.status_code, 400, response.content)
                errors = response.json()["decisions"]
                self.assertIsInstance(errors, list)
                self.assertEqual(
                    errors,
                    [f'"Сливки 33%": мастер-товара с id={target.id} нет у этого владельца, либо он архивен.'],
                )
                target.refresh_from_db()
                self.assertNotEqual(target.canonical_name, "Сливки 33%")

    def test_apply_two_replaces_of_one_target_first_wins_second_conflicts(self):
        """2026-09-29 (unified-import-architecture spec §9.1): было
        whole-batch-abort (TASK-73), стало per-row -- первая replace-декларация
        на target применяется, вторая в той же пачке возвращается с conflict,
        остальная пачка (тут -- первая строка) не страдает."""
        self.auth()
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        response = self.client.post(
            self.url,
            {
                "owner_id": str(self.owner.id),
                "decisions": [self._replace("Сливки 33%", existing.id), self._replace("Сливки 10%", existing.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["rows"]
        self.assertIsNone(rows[0]["conflict"])
        self.assertEqual(rows[0]["canonical_name"], "Сливки 33%")
        self.assertIsNotNone(rows[1]["conflict"])
        self.assertIn(str(existing.id), rows[1]["conflict"])
        self.assertEqual(rows[1]["canonical_name"], "Сливки 33%")
        existing.refresh_from_db()
        self.assertEqual(existing.canonical_name, "Сливки 33%")

    def test_apply_rejects_oversized_decisions_list_with_list_error(self):
        self.auth()
        response = self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "decisions": [{"raw_name": "Товар", "action": "skip"}] * 5001},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), {"decisions": ["Не более 5000 решений за запрос."]})

    def test_apply_accepts_more_than_500_decisions(self):
        self.auth()
        decisions = [
            {"raw_name": f"Товар {n}", "action": "create_new", "unit_id": str(self.unit.id)} for n in range(600)
        ]
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "decisions": decisions}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["rows"]), 600)
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 600)


class MasterProductImportUploadAdminTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="import-upload-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )

    def test_reset_stuck_processing_only_touches_processing_rows(self):
        from django.contrib.messages.middleware import MessageMiddleware
        from django.contrib.sessions.middleware import SessionMiddleware

        from catalog.admin import MasterProductImportUploadAdmin

        stuck = MasterProductImportUpload.objects.create(owner=self.owner, rows_total=600)
        already_ready = MasterProductImportUpload.objects.create(
            owner=self.owner, rows_total=10, status=MasterProductImportUploadStatus.READY, rows_done=10
        )
        admin_instance = MasterProductImportUploadAdmin(MasterProductImportUpload, admin.site)
        request = RequestFactory().post("/admin/")
        # Add session middleware to the request
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        # Add messages middleware to the request
        MessageMiddleware(lambda r: None).process_request(request)
        request._messages = FallbackStorage(request)

        admin_instance.reset_stuck_processing(request, MasterProductImportUpload.objects.all())

        stuck.refresh_from_db()
        already_ready.refresh_from_db()
        self.assertEqual(stuck.status, MasterProductImportUploadStatus.FAILED)
        self.assertTrue(stuck.error_message)
        self.assertIsNotNone(stuck.processed_at)
        self.assertEqual(already_ready.status, MasterProductImportUploadStatus.READY)


class ResolveMasterProductImportRowsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="import-rows-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.unit_kg = Unit.objects.create(
            code="testunit51kg", name="Тестовая единица TASK51", kind="weight", status=EntityStatus.ACTIVE
        )
        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)

    def test_similar_candidate_leaves_action_for_the_user(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA", unit=self.unit_kg)
        rows = resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 2, "name": "AAAA"}])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.row_number, 2)
        self.assertEqual(row.raw_name, "AAAA")
        # ТЗ §46: preview never picks replace/update_existing on its own.
        self.assertIsNone(row.action)
        self.assertTrue(row.needs_attention)
        self.assertEqual(len(row.candidates), 1)
        self.assertEqual(row.candidates[0].master_product_id, existing.id)
        self.assertEqual(row.candidates[0].canonical_name, "AAAA")
        self.assertEqual(row.candidates[0].unit_id, self.unit_kg.id)
        self.assertEqual(row.candidates[0].unit_name, "Тестовая единица TASK51")
        # Legacy top-1 fields mirror candidates[0].
        self.assertEqual(row.candidate_master_product_id, existing.id)
        self.assertEqual(row.candidate_canonical_name, "AAAA")
        self.assertEqual(row.score, row.candidates[0].score)

    @override_settings(MASTER_PRODUCT_MATCH_MIN_SCORE=0.999)
    def test_below_threshold_candidate_is_not_offered(self):
        MasterProduct.objects.create(owner=self.owner, canonical_name="AAAB")
        rows = resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 4, "name": "AAAA"}])
        row = rows[0]
        self.assertEqual(row.action, "create_new")
        self.assertEqual(row.candidates, [])
        self.assertIsNone(row.candidate_master_product_id)
        self.assertIsNone(row.score)
        self.assertFalse(row.needs_attention)

    def test_returns_at_most_five_candidates_sorted_by_score(self):
        for name in ["AAAA", "AAAB", "AABB", "ABBB", "AAAC", "AACC"]:
            MasterProduct.objects.create(owner=self.owner, canonical_name=name)
        rows = resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 2, "name": "AAAA"}])
        candidates = rows[0].candidates
        self.assertEqual(len(candidates), 5)
        self.assertEqual(candidates[0].canonical_name, "AAAA")
        scores = [candidate.score for candidate in candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))

    @override_settings(MASTER_PRODUCT_MATCH_CONFIDENT_SCORE=0.8, MASTER_PRODUCT_CANDIDATE_RANKING_ENABLED=True)
    def test_ambiguous_top_score_calls_llm_and_uses_its_order(self):
        first = MasterProduct.objects.create(owner=self.owner, canonical_name="Сыр Костромской")
        second = MasterProduct.objects.create(owner=self.owner, canonical_name="Сыр Российский")
        fake_candidates = [(first, 0.65), (second, 0.6)]

        def fake_rerank(*, rows):
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0]["candidates"],
                [
                    {"id": str(first.id), "canonical_name": "Сыр Костромской"},
                    {"id": str(second.id), "canonical_name": "Сыр Российский"},
                ],
            )
            return {rows[0]["row_number"]: [str(second.id), str(first.id)]}

        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products", return_value=fake_candidates
        ):
            with patch(
                "api.v1.catalog.selectors.master_catalog.rerank_candidates", side_effect=fake_rerank
            ) as mock_rerank:
                rows = resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 3, "name": "Сыр"}])
        mock_rerank.assert_called_once()
        row = rows[0]
        self.assertEqual([c.master_product_id for c in row.candidates], [second.id, first.id])
        self.assertEqual(row.candidate_master_product_id, second.id)
        # ТЗ §46: still requires user review, ranking never auto-decides.
        self.assertIsNone(row.action)

    @override_settings(MASTER_PRODUCT_MATCH_CONFIDENT_SCORE=0.8)
    def test_confident_top_score_skips_llm_entirely(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Молоко")
        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products", return_value=[(existing, 0.95)]
        ):
            with patch("api.v1.catalog.selectors.master_catalog.rerank_candidates") as mock_rerank:
                rows = resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 4, "name": "Молоко"}])
        mock_rerank.assert_not_called()
        self.assertEqual(rows[0].candidate_master_product_id, existing.id)

    def test_no_candidates_skips_llm_entirely(self):
        with patch("api.v1.catalog.selectors.master_catalog.rerank_candidates") as mock_rerank:
            rows = resolve_master_product_import_rows(
                owner=self.owner, rows=[{"row_number": 5, "name": "Совсем новый товар"}]
            )
        mock_rerank.assert_not_called()
        self.assertEqual(rows[0].action, "create_new")

    @override_settings(MASTER_PRODUCT_MATCH_CONFIDENT_SCORE=0.8)
    def test_llm_failure_keeps_original_vector_order(self):
        first = MasterProduct.objects.create(owner=self.owner, canonical_name="Кофе молотый")
        second = MasterProduct.objects.create(owner=self.owner, canonical_name="Кофе зерновой")
        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products",
            return_value=[(first, 0.65), (second, 0.6)],
        ):
            with patch(
                "api.v1.catalog.selectors.master_catalog.rerank_candidates", side_effect=CandidateRankingError("boom")
            ):
                rows = resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 6, "name": "Кофе"}])
        self.assertEqual([c.master_product_id for c in rows[0].candidates], [first.id, second.id])

    @override_settings(MASTER_PRODUCT_MATCH_CONFIDENT_SCORE=0.8)
    def test_llm_returning_no_opinion_keeps_original_vector_order(self):
        first = MasterProduct.objects.create(owner=self.owner, canonical_name="Масло сливочное")
        second = MasterProduct.objects.create(owner=self.owner, canonical_name="Масло растительное")
        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products",
            return_value=[(first, 0.65), (second, 0.6)],
        ):
            with patch("api.v1.catalog.selectors.master_catalog.rerank_candidates", return_value={}):
                rows = resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 7, "name": "Масло"}])
        self.assertEqual([c.master_product_id for c in rows[0].candidates], [first.id, second.id])

    @override_settings(MASTER_PRODUCT_CANDIDATE_RANKING_ENABLED=False, MASTER_PRODUCT_MATCH_CONFIDENT_SCORE=0.8)
    def test_ranking_disabled_by_flag_skips_llm_even_in_ambiguous_zone(self):
        first = MasterProduct.objects.create(owner=self.owner, canonical_name="Рис")
        with patch(
            "api.v1.catalog.selectors.master_catalog.find_candidate_master_products", return_value=[(first, 0.65)]
        ):
            with patch("api.v1.catalog.selectors.master_catalog.rerank_candidates") as mock_rerank:
                resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 8, "name": "Рис"}])
        mock_rerank.assert_not_called()

    def test_candidate_without_unit_reports_null_unit(self):
        MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA")
        rows = resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 2, "name": "AAAA"}])
        self.assertIsNone(rows[0].candidates[0].unit_id)
        self.assertIsNone(rows[0].candidates[0].unit_name)

    def test_no_candidate_at_all_proposes_create_new_without_attention(self):
        rows = resolve_master_product_import_rows(
            owner=self.owner, rows=[{"row_number": 3, "name": "Совсем новый товар"}]
        )
        row = rows[0]
        self.assertEqual(row.action, "create_new")
        self.assertIsNone(row.candidate_master_product_id)
        self.assertFalse(row.needs_attention)
        self.assertEqual(row.candidates, [])

    def test_resolves_unit_label_case_insensitively_by_code_or_name(self):
        rows = resolve_master_product_import_rows(
            owner=self.owner,
            rows=[{"row_number": 5, "name": "Новый товар", "unit": "ТЕСТОВАЯ ЕДИНИЦА TASK51"}],
        )
        self.assertEqual(rows[0].unit_id, self.unit_kg.id)
        self.assertEqual(rows[0].unit_label, "ТЕСТОВАЯ ЕДИНИЦА TASK51")

    @override_settings(MASTER_CATALOG_UNIT_LLM_ENABLED=False)
    def test_unknown_unit_label_flags_only_that_row_not_the_whole_preview(self):
        rows = resolve_master_product_import_rows(
            owner=self.owner,
            rows=[
                {"row_number": 6, "name": "Товар А"},
                {"row_number": 7, "name": "Товар Б", "unit": "неизвестная единица"},
            ],
        )
        self.assertEqual(len(rows), 2)
        good, bad = rows
        self.assertFalse(good.unit_unrecognized)
        self.assertTrue(bad.unit_unrecognized)
        self.assertIsNone(bad.unit_id)
        self.assertEqual(bad.unit_label, "неизвестная единица")
        # Final-review finding: an unrecognized unit requires manual review
        # (raw ТЗ §52) even though it has no similarity candidates and its
        # suggested action stays create_new.
        self.assertFalse(good.needs_attention)
        self.assertEqual(bad.action, "create_new")
        self.assertTrue(bad.needs_attention)

    def test_llm_fills_unrecognized_unit_when_confident(self):
        def fake_suggest(*, rows, units):
            return {row["row_number"]: str(self.unit_kg.id) for row in rows}

        with patch(
            "api.v1.catalog.selectors.master_catalog.suggest_unit_ids", side_effect=fake_suggest
        ) as mock_suggest:
            rows = resolve_master_product_import_rows(
                owner=self.owner,
                rows=[{"row_number": 9, "name": "Товар Г", "unit": "неизвестная единица"}],
            )
        mock_suggest.assert_called_once()
        row = rows[0]
        self.assertFalse(row.unit_unrecognized)
        self.assertEqual(row.unit_id, self.unit_kg.id)
        self.assertEqual(row.unit_source, "llm")
        # No candidates for this brand-new name, so needs_attention is now
        # driven purely by candidates — the LLM fill itself never forces it.
        self.assertFalse(row.needs_attention)
        # Final-review finding: verify the payload shape actually sent to
        # the LLM, so a silent field rename in the caller (e.g. raw_name ->
        # name) doesn't pass this test undetected.
        call_kwargs = mock_suggest.call_args.kwargs
        for sent_row in call_kwargs["rows"]:
            self.assertEqual(set(sent_row.keys()), {"row_number", "raw_name", "unit_label"})
        sent_units = call_kwargs["units"]
        for sent_unit in sent_units:
            self.assertEqual(set(sent_unit.keys()), {"id", "code", "name"})
        unit_ids = [sent_unit["id"] for sent_unit in sent_units]
        self.assertEqual(len(unit_ids), len(set(unit_ids)))

    def test_llm_answer_for_a_non_offered_row_number_is_ignored(self):
        """Final-review finding: a row_number the LLM returns that wasn't
        among the unrecognized rows actually offered to it must never
        overwrite an already-resolved row, even if the model "helpfully"
        answers for it too (a realistic renumbering/hallucination failure
        mode) — that row must be excluded from LLM review entirely."""
        known_row_number = 20
        unrecognized_row_number = 21

        def fake_suggest(*, rows, units):
            # Simulate the LLM answering both for the row it was actually
            # asked about AND for a row_number it was never offered (the
            # already-good, known-unit row).
            return {
                known_row_number: str(self.unit_kg.id),
                unrecognized_row_number: str(self.unit_kg.id),
            }

        with patch("api.v1.catalog.selectors.master_catalog.suggest_unit_ids", side_effect=fake_suggest):
            rows = resolve_master_product_import_rows(
                owner=self.owner,
                rows=[
                    {
                        "row_number": known_row_number,
                        "name": "Товар с известной единицей",
                        "unit": "ТЕСТОВАЯ ЕДИНИЦА TASK51",
                    },
                    {
                        "row_number": unrecognized_row_number,
                        "name": "Товар с неизвестной единицей",
                        "unit": "неизвестная единица",
                    },
                ],
            )

        known_row = next(row for row in rows if row.row_number == known_row_number)
        unrecognized_row = next(row for row in rows if row.row_number == unrecognized_row_number)

        # The known row was never in unrecognized_row_numbers (its unit
        # already matched from the file), so it was never offered to the
        # LLM — its unsolicited answer must be ignored entirely.
        self.assertEqual(known_row.unit_id, self.unit_kg.id)
        self.assertEqual(known_row.unit_label, "ТЕСТОВАЯ ЕДИНИЦА TASK51")
        self.assertFalse(known_row.unit_unrecognized)
        self.assertIsNone(known_row.unit_source)

        # The row that was actually offered still gets resolved normally.
        self.assertFalse(unrecognized_row.unit_unrecognized)
        self.assertEqual(unrecognized_row.unit_id, self.unit_kg.id)
        self.assertEqual(unrecognized_row.unit_source, "llm")

    def test_llm_suggestion_outside_allowed_units_is_ignored(self):
        with patch(
            "api.v1.catalog.selectors.master_catalog.suggest_unit_ids",
            return_value={10: "11111111-1111-1111-1111-111111111111"},
        ):
            rows = resolve_master_product_import_rows(
                owner=self.owner,
                rows=[{"row_number": 10, "name": "Товар Д", "unit": "неизвестная единица"}],
            )
        row = rows[0]
        self.assertTrue(row.unit_unrecognized)
        self.assertIsNone(row.unit_id)
        self.assertIsNone(row.unit_source)

    def test_llm_error_leaves_row_unaffected(self):
        with patch("api.v1.catalog.selectors.master_catalog.suggest_unit_ids", side_effect=UnitSuggestionError("boom")):
            rows = resolve_master_product_import_rows(
                owner=self.owner,
                rows=[{"row_number": 11, "name": "Товар Е", "unit": "неизвестная единица"}],
            )
        row = rows[0]
        self.assertTrue(row.unit_unrecognized)
        self.assertIsNone(row.unit_id)
        self.assertIsNone(row.unit_source)

    @override_settings(MASTER_CATALOG_UNIT_LLM_ENABLED=False)
    def test_llm_not_called_when_flag_disabled(self):
        with patch("api.v1.catalog.selectors.master_catalog.suggest_unit_ids") as mock_suggest:
            rows = resolve_master_product_import_rows(
                owner=self.owner,
                rows=[{"row_number": 12, "name": "Товар Ж", "unit": "неизвестная единица"}],
            )
        mock_suggest.assert_not_called()
        self.assertTrue(rows[0].unit_unrecognized)

    def test_llm_not_called_when_no_unrecognized_rows(self):
        with patch("api.v1.catalog.selectors.master_catalog.suggest_unit_ids") as mock_suggest:
            resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 13, "name": "Товар З"}])
        mock_suggest.assert_not_called()

    def test_known_unit_label_is_not_flagged_unrecognized(self):
        rows = resolve_master_product_import_rows(
            owner=self.owner,
            rows=[{"row_number": 5, "name": "Новый товар", "unit": "ТЕСТОВАЯ ЕДИНИЦА TASK51"}],
        )
        self.assertFalse(rows[0].unit_unrecognized)

    def test_row_without_unit_is_not_flagged_unrecognized(self):
        rows = resolve_master_product_import_rows(owner=self.owner, rows=[{"row_number": 1, "name": "Товар"}])
        self.assertFalse(rows[0].unit_unrecognized)
        self.assertIsNone(rows[0].unit_id)

    def test_parses_net_value_with_comma_decimal(self):
        rows = resolve_master_product_import_rows(
            owner=self.owner, rows=[{"row_number": 8, "name": "Товар В", "net": "1,5"}]
        )
        self.assertEqual(rows[0].net_value, Decimal("1.5"))


class MasterProductImportPreviewAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="import-preview@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.unit = Unit.objects.create(code="kg", name="Килограмм", kind="weight", status=EntityStatus.ACTIVE)
        self.url = "/api/v1/catalog/master-products/import/preview"
        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def test_preview_writes_nothing_and_proposes_actions(self):
        self.auth()
        existing = MasterProduct.objects.create(
            owner=self.owner, canonical_name="AAAA", unit=self.unit, net_value=Decimal("2.5"), note="исходное"
        )
        upload = _xlsx_upload([["Название"], ["AAAA"], ["Совсем новый товар"]], name="preview.xlsx")
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(len(payload["rows"]), 2)
        by_name = {row["raw_name"]: row for row in payload["rows"]}
        self.assertIsNone(by_name["AAAA"]["action"])
        self.assertTrue(by_name["AAAA"]["needs_attention"])
        self.assertEqual(by_name["AAAA"]["candidates"][0]["master_product_id"], str(existing.id))
        self.assertEqual(by_name["AAAA"]["candidates"][0]["unit_name"], "Килограмм")
        self.assertEqual(by_name["Совсем новый товар"]["candidates"], [])
        self.assertEqual(payload["needs_attention_count"], 1)
        self.assertEqual(payload["auto_resolved_count"], 1)
        self.assertEqual(by_name["Совсем новый товар"]["action"], "create_new")
        # No new MasterProduct row was created — only the one from setUp.
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 1)
        # The existing row's visible fields are untouched by the preview. We
        # deliberately don't assert against `embedding`: find_candidate_master_products
        # may lazily backfill it via _ensure_master_product_embeddings (selectors.py) —
        # an accepted self-healing cache write, not business data — so it's excluded here.
        existing.refresh_from_db()
        self.assertEqual(existing.canonical_name, "AAAA")
        self.assertEqual(existing.unit_id, self.unit.id)
        self.assertEqual(existing.net_value, Decimal("2.5"))
        self.assertEqual(existing.note, "исходное")

    def test_preview_reports_rows_dropped_without_name(self):
        self.auth()
        upload = _xlsx_upload([["Название"], ["AAAA"], [""], ["Другой товар"]], name="preview2.xlsx")
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["rows_without_name"], 1)
        self.assertEqual(payload["total_rows"], 3)

    def test_preview_denied_for_stranger_owner_id(self):
        stranger = User.objects.create(
            email="import-preview-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(stranger))
        upload = _xlsx_upload([["Название"], ["Товар"]])
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        self.assertEqual(response.status_code, 403)

    @override_settings(MASTER_CATALOG_UNIT_LLM_ENABLED=False)
    def test_preview_flags_unknown_unit_row_instead_of_rejecting(self):
        self.auth()
        upload = _xlsx_upload([["Название", "Единица"], ["Товар", "неведомая единица"]])
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200)
        row = response.data["rows"][0]
        self.assertTrue(row["unit_unrecognized"])
        self.assertIsNone(row["unit_id"])

    def test_preview_candidate_includes_updated_at(self):
        self.auth()
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="AAAA", unit=self.unit)
        upload = _xlsx_upload([["Название"], ["AAAA"]])
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        candidate = response.json()["rows"][0]["candidates"][0]
        self.assertIsNotNone(candidate["updated_at"])
        # Round-trips through parse_datetime() the same way apply's
        # expected_updated_at field would deserialize it (TASK-76).
        self.assertEqual(parse_datetime(candidate["updated_at"]), existing.updated_at)

    def test_apply_accepts_replace_with_matching_expected_updated_at(self):
        self.auth()
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        upload = _xlsx_upload([["Название"], ["Сливки 33%"]])
        preview = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        expected_updated_at = preview.json()["rows"][0]["candidates"][0]["updated_at"]

        response = self.client.post(
            "/api/v1/catalog/master-products/import/apply",
            {
                "owner_id": str(self.owner.id),
                "decisions": [
                    {
                        "raw_name": "Сливки 33%",
                        "action": "replace",
                        "master_product_id": str(existing.id),
                        "unit_id": str(self.unit.id),
                        "expected_updated_at": expected_updated_at,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        existing.refresh_from_db()
        self.assertEqual(existing.canonical_name, "Сливки 33%")

    def test_apply_conflicts_replace_with_stale_expected_updated_at(self):
        """2026-09-29 (unified-import-architecture spec §9.1/§9.2): было
        whole-batch-abort (TASK-76), стало per-row conflict -- строка не
        применяется, остальная пачка (тут единственная строка) продолжает
        обрабатываться, ответ 200 с conflict на самой строке."""
        self.auth()
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        upload = _xlsx_upload([["Название"], ["Сливки 33%"]])
        preview = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        stale_expected_updated_at = preview.json()["rows"][0]["candidates"][0]["updated_at"]

        # Someone else changes the target between preview and apply.
        existing.note = "изменено параллельно"
        existing.save(update_fields=["note", "updated_at"])

        response = self.client.post(
            "/api/v1/catalog/master-products/import/apply",
            {
                "owner_id": str(self.owner.id),
                "decisions": [
                    {
                        "raw_name": "Сливки 33%",
                        "action": "replace",
                        "master_product_id": str(existing.id),
                        "unit_id": str(self.unit.id),
                        "expected_updated_at": stale_expected_updated_at,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["rows"][0]["conflict"],
            '"Сливки 33%": товар изменился после проверки. Проверьте его ещё раз.',
        )
        existing.refresh_from_db()
        self.assertEqual(existing.canonical_name, "Сливки")

    def test_apply_conflicts_update_existing_with_stale_expected_updated_at(self):
        self.auth()
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        upload = _xlsx_upload([["Название"], ["Сливки 33%"]])
        preview = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        stale_expected_updated_at = preview.json()["rows"][0]["candidates"][0]["updated_at"]

        existing.note = "изменено параллельно"
        existing.save(update_fields=["note", "updated_at"])

        response = self.client.post(
            "/api/v1/catalog/master-products/import/apply",
            {
                "owner_id": str(self.owner.id),
                "decisions": [
                    {
                        "raw_name": "Сливки 33%",
                        "action": "update_existing",
                        "master_product_id": str(existing.id),
                        "expected_updated_at": stale_expected_updated_at,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["rows"][0]["conflict"],
            '"Сливки 33%": товар изменился после проверки. Проверьте его ещё раз.',
        )

    def test_apply_without_expected_updated_at_skips_the_check(self):
        """Backward compat (TASK-76): a caller that omits expected_updated_at
        (today's frontend) behaves exactly as before the check existed."""
        self.auth()
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        existing.note = "изменено параллельно"
        existing.save(update_fields=["note", "updated_at"])

        response = self.client.post(
            "/api/v1/catalog/master-products/import/apply",
            {
                "owner_id": str(self.owner.id),
                "decisions": [
                    {
                        "raw_name": "Сливки 33%",
                        "action": "replace",
                        "master_product_id": str(existing.id),
                        "unit_id": str(self.unit.id),
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_preview_llm_fills_unrecognized_unit_and_marks_source(self):
        self.auth()

        def fake_suggest(*, rows, units):
            return {row["row_number"]: str(self.unit.id) for row in rows}

        with patch("api.v1.catalog.selectors.master_catalog.suggest_unit_ids", side_effect=fake_suggest):
            upload = _xlsx_upload([["Название", "Единица"], ["Товар", "килограммы"]])
            response = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")

        self.assertEqual(response.status_code, 200)
        row = response.data["rows"][0]
        self.assertFalse(row["unit_unrecognized"])
        self.assertEqual(row["unit_id"], str(self.unit.id))
        self.assertEqual(row["unit_source"], "llm")

    @override_settings(MASTER_CATALOG_UNIT_LLM_ENABLED=False)
    def test_preview_unrecognized_unit_without_llm_match_has_null_source(self):
        self.auth()
        upload = _xlsx_upload([["Название", "Единица"], ["Товар", "неведомая единица"]])
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200)
        row = response.data["rows"][0]
        self.assertTrue(row["unit_unrecognized"])
        self.assertIsNone(row["unit_source"])

    def test_large_file_returns_202_and_creates_upload(self):
        self.auth()
        rows = [["Название"]] + [[f"Товар {n}"] for n in range(1, 502)]  # 501 data rows > threshold 500
        upload_file = _xlsx_upload(rows, name="large.xlsx")
        with patch("api.v1.catalog.tasks.process_master_product_import_upload_task.delay"):
            response = self.client.post(
                self.url, {"owner_id": str(self.owner.id), "file": upload_file}, format="multipart"
            )
        self.assertEqual(response.status_code, 202, response.content)
        payload = response.json()
        self.assertIn("upload_id", payload)
        self.assertEqual(payload["status"], "processing")
        self.assertEqual(
            payload["status_url"], f"/api/v1/catalog/master-products/import/uploads/{payload['upload_id']}"
        )
        upload = MasterProductImportUpload.objects.get(pk=payload["upload_id"])
        self.assertEqual(upload.owner_id, self.owner.id)
        self.assertEqual(upload.rows_total, 501)
        self.assertEqual(upload.status, MasterProductImportUploadStatus.PROCESSING)

    def test_large_file_with_invalid_net_fails_fast_before_enqueueing(self):
        """Fix 2a (final-review finding): a bad `net` value used to only
        surface once the Celery task got around to that row's batch,
        potentially minutes into a 202-accepted upload. It must fail the
        same way the synchronous (<=500 rows) path already does: immediate
        400, no upload row created."""
        self.auth()
        rows = (
            [["Название", "Нетто"], ["Товар 1", "не число"]]
            + [[f"Товар {n}", ""] for n in range(2, 502)]  # 501 data rows > threshold 500
        )
        upload_file = _xlsx_upload(rows, name="large_bad_net.xlsx")
        with patch("api.v1.catalog.tasks.process_master_product_import_upload_task.delay") as mock_delay:
            response = self.client.post(
                self.url, {"owner_id": str(self.owner.id), "file": upload_file}, format="multipart"
            )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("некорректное значение нетто", str(response.json()["file"]))
        mock_delay.assert_not_called()
        self.assertEqual(MasterProductImportUpload.objects.count(), 0)

    def test_small_file_creates_no_upload_row(self):
        # test_preview_writes_nothing_and_proposes_actions (existing test, above) already proves
        # the ≤500-row response shape is unchanged — this test only proves the new model isn't
        # touched on that path.
        self.auth()
        upload = _xlsx_upload([["Название"], ["AAAA"]], name="small.xlsx")
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(MasterProductImportUpload.objects.count(), 0)

    def test_file_over_ceiling_is_still_rejected(self):
        # Acceptance criterion 3: the absolute ceiling moves from 500 to
        # MAX_IMPORT_ROWS=5000, but a file over that ceiling is still a 400 —
        # this task raises the threshold, it does not remove it.
        self.auth()
        rows = [["Название"]] + [[f"Товар {n}"] for n in range(1, 5002)]  # 5001 data rows > 5000
        upload = _xlsx_upload(rows, name="too_large.xlsx")
        response = self.client.post(self.url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Слишком много строк", str(response.json()))
        self.assertEqual(MasterProductImportUpload.objects.count(), 0)


class MasterProductImportUploadStatusAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="import-status@example.com", full_name="Owner", role=UserRole.VENUE_USER)

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _url(self, upload_id):
        return f"/api/v1/catalog/master-products/import/uploads/{upload_id}"

    def test_processing_status_has_no_result(self):
        self.auth()
        upload = MasterProductImportUpload.objects.create(owner=self.owner, rows_total=600, rows_done=200)
        response = self.client.get(self._url(upload.id), {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["status"], "processing")
        self.assertEqual(payload["rows_done"], 200)
        self.assertEqual(payload["rows_total"], 600)
        self.assertEqual(payload["percent"], 33)
        self.assertIsNone(payload["result"])

    def test_ready_status_includes_result(self):
        self.auth()
        upload = MasterProductImportUpload.objects.create(
            owner=self.owner,
            rows_total=1,
            rows_done=1,
            status=MasterProductImportUploadStatus.READY,
            result={
                "sheet_name": None,
                "header_row_number": None,
                "detected_columns": {},
                "total_rows": 1,
                "rows_without_name": 0,
                "auto_resolved_count": 1,
                "needs_attention_count": 0,
                "rows": [],
            },
        )
        response = self.client.get(self._url(upload.id), {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["percent"], 100)
        self.assertIsNotNone(payload["result"])

    def test_failed_status_includes_error_message(self):
        self.auth()
        upload = MasterProductImportUpload.objects.create(
            owner=self.owner,
            rows_total=10,
            rows_done=3,
            status=MasterProductImportUploadStatus.FAILED,
            error_message="boom",
        )
        response = self.client.get(self._url(upload.id), {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["error_message"], "boom")

    def test_non_uuid_owner_id_gets_400(self):
        """Fix 3a (final-review finding): every other owner_id-taking
        endpoint in this file validates owner_id through a query serializer
        and returns 400 for a non-UUID value; this endpoint used to read it
        raw and silently fall through to a 404 instead."""
        upload = MasterProductImportUpload.objects.create(owner=self.owner, rows_total=1)
        self.auth()
        response = self.client.get(self._url(upload.id), {"owner_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("owner_id", response.json())

    def test_wrong_owner_id_gets_404(self):
        other_owner = User.objects.create(
            email="import-status-other@example.com", full_name="Other", role=UserRole.VENUE_USER
        )
        upload = MasterProductImportUpload.objects.create(owner=other_owner, rows_total=1)
        self.auth()
        response = self.client.get(self._url(upload.id), {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 404)


class MasterProductImportUploadStatusJsonRoundTripAPITests(APITransactionTestCase):
    """Separate from MasterProductImportUploadStatusAPITests (a plain
    APITestCase/TestCase, atomic-wrapped per test) specifically to run the
    Celery task function body directly, the same way
    ProcessMasterProductImportUploadTaskTests does and for the identical
    reason documented on that class: the task calls close_old_connections()
    at entry and in its finally block, which breaks every following query
    under a plain TestCase's atomic wrapping ("the connection is closed").
    APITransactionTestCase avoids the mismatch (real commits, no atomic
    wrapping) while keeping self.client (APIClient) available."""

    def setUp(self):
        self.owner = User.objects.create(
            email="import-status-fix5@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _url(self, upload_id):
        return f"/api/v1/catalog/master-products/import/uploads/{upload_id}"

    def test_ready_result_round_trips_decimal_uuid_datetime_through_json(self):
        """Fix 5 (final-review finding): result's JSON round-trip (the task
        builds it via MasterProductImportPreviewResponseSerializer +
        JSONRenderer + json.loads, then it's re-served here through this
        endpoint's nested result field) was only manually verified to
        preserve Decimal/UUID/datetime fields by reading the serializer
        code — no test exercised it with realistic (non-empty) data.
        Runs the actual Celery task function directly (not through
        .delay()/Celery) against a real MasterProduct candidate, then
        fetches the status endpoint over HTTP, matching what the
        synchronous preview endpoint would have returned for the same
        input (see test_preview_candidate_includes_updated_at)."""
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        unit = Unit.objects.create(code="kg-fix5", name="Килограмм", kind="weight", status=EntityStatus.ACTIVE)
        existing = MasterProduct.objects.create(
            owner=self.owner, canonical_name="AAAA", unit=unit, net_value=Decimal("2.5")
        )
        upload = MasterProductImportUpload.objects.create(owner=self.owner, rows_total=1)
        rows = [{"row_number": 1, "name": "AAAA", "unit": None, "net": "2.5", "note": None}]
        parsed_meta = {"sheet_name": None, "header_row_number": None, "detected_columns": {}, "rows_without_name": 0}

        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)

        process_master_product_import_upload_task(
            upload_id=str(upload.id), owner_id=str(self.owner.id), rows=rows, parsed_meta=parsed_meta
        )

        self.auth()
        response = self.client.get(self._url(upload.id), {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["status"], "ready")
        row = payload["result"]["rows"][0]
        # The row's own net_value (Decimal) round-trips as a decimal string without trailing zeros.
        self.assertEqual(row["net_value"], "2.5")
        candidate = row["candidates"][0]
        # candidates[].master_product_id (UUID) round-trips as a valid UUID string.
        self.assertEqual(candidate["master_product_id"], str(existing.id))
        uuid.UUID(candidate["master_product_id"])
        # candidates[].updated_at (datetime) round-trips as a parseable ISO datetime,
        # matching what the synchronous preview endpoint returns for the same field
        # (test_preview_candidate_includes_updated_at, above).
        self.assertEqual(parse_datetime(candidate["updated_at"]), existing.updated_at)


class ApplyMasterProductImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="import-apply-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.unit = Unit.objects.create(code="kg", name="Килограмм", kind="weight", status=EntityStatus.ACTIVE)

    def test_create_new_creates_master_product_with_given_fields(self):
        rows = apply_master_product_import(
            owner=self.owner,
            decisions=[
                {
                    "raw_name": "Манго пюре",
                    "action": "create_new",
                    "unit_id": self.unit.id,
                    "net_value": Decimal("1.5"),
                    "note": "центральная матрица",
                }
            ],
        )
        self.assertEqual(rows[0].action, "create_new")
        mp = MasterProduct.objects.get(owner=self.owner, canonical_name="Манго пюре")
        self.assertEqual(rows[0].master_product_id, mp.id)
        self.assertEqual(mp.unit_id, self.unit.id)
        self.assertEqual(mp.net_value, Decimal("1.5"))
        self.assertEqual(mp.note, "центральная матрица")

    def test_update_existing_does_not_overwrite_existing_fields(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%", note="старое")
        rows = apply_master_product_import(
            owner=self.owner,
            decisions=[{"raw_name": "Сливки 33 проц", "action": "update_existing", "master_product_id": existing.id}],
        )
        self.assertEqual(rows[0].master_product_id, existing.id)
        self.assertEqual(rows[0].canonical_name, "Сливки 33%")
        existing.refresh_from_db()
        self.assertEqual(existing.note, "старое")

    def test_skip_writes_nothing(self):
        rows = apply_master_product_import(
            owner=self.owner, decisions=[{"raw_name": "Пропущенный товар", "action": "skip"}]
        )
        self.assertIsNone(rows[0].master_product_id)
        self.assertFalse(MasterProduct.objects.filter(owner=self.owner, canonical_name="Пропущенный товар").exists())

    def test_update_existing_without_master_product_id_raises(self):
        with self.assertRaises(ValidationError):
            apply_master_product_import(
                owner=self.owner, decisions=[{"raw_name": "Товар", "action": "update_existing"}]
            )

    def test_update_existing_with_another_owners_master_product_raises(self):
        stranger = User.objects.create(
            email="import-apply-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        foreign = MasterProduct.objects.create(owner=stranger, canonical_name="Чужой товар")
        with self.assertRaises(ValidationError):
            apply_master_product_import(
                owner=self.owner,
                decisions=[{"raw_name": "Товар", "action": "update_existing", "master_product_id": foreign.id}],
            )

    def test_two_create_new_rows_with_same_name_deduplicate_to_one_master_product(self):
        rows = apply_master_product_import(
            owner=self.owner,
            decisions=[
                {"raw_name": "Дубликат", "action": "create_new"},
                {"raw_name": "дубликат", "action": "create_new"},
            ],
        )
        self.assertEqual(rows[0].master_product_id, rows[1].master_product_id)
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner, canonical_name="Дубликат").count(), 1)

    def test_whole_batch_atomic_on_invalid_decision(self):
        with self.assertRaises(ValidationError):
            apply_master_product_import(
                owner=self.owner,
                decisions=[
                    {"raw_name": "Валидный", "action": "create_new"},
                    {"raw_name": "Невалидный", "action": "update_existing"},  # missing master_product_id
                ],
            )
        self.assertFalse(MasterProduct.objects.filter(owner=self.owner, canonical_name="Валидный").exists())

    def _replace(self, target, raw_name="Сливки 33% новые", unit=None):
        return {
            "raw_name": raw_name,
            "action": "replace",
            "master_product_id": target.id,
            "unit_id": (unit or self.unit).id,
        }

    def test_replace_overwrites_name_and_unit_keeping_id(self):
        litre = Unit.objects.create(code="l-replace", name="Литр", kind="volume", status=EntityStatus.ACTIVE)
        existing = MasterProduct.objects.create(
            owner=self.owner, canonical_name="Сливки", unit=self.unit, net_value=Decimal("1"), note="старое"
        )
        rows = apply_master_product_import(owner=self.owner, decisions=[self._replace(existing, unit=litre)])
        self.assertEqual(rows[0].action, "replace")
        self.assertEqual(rows[0].master_product_id, existing.id)
        self.assertEqual(rows[0].canonical_name, "Сливки 33% новые")
        self.assertEqual(rows[0].unit_id, litre.id)
        existing.refresh_from_db()
        self.assertEqual(existing.canonical_name, "Сливки 33% новые")
        self.assertEqual(existing.unit_id, litre.id)
        self.assertEqual(existing.net_value, Decimal("1"))
        self.assertEqual(existing.note, "старое")
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 1)

    def test_replace_resets_embedding_so_search_uses_new_name(self):
        from api.v1.catalog.selectors import _vector_literal

        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {MasterProduct._meta.db_table} SET embedding = %s::vector WHERE id = %s",
                [_vector_literal(_fake_embedding("Сливки")), str(existing.id)],
            )
        apply_master_product_import(owner=self.owner, decisions=[self._replace(existing)])
        self.assertTrue(MasterProduct.objects.filter(pk=existing.id, embedding__isnull=True).exists())

    def test_replace_keeps_links_from_company_catalogs(self):
        venue = Venue.objects.create(display_name="Cafe Replace Link")
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        item = VenueProductMatrixItem.objects.create(venue=venue, requested_name="Сливки", master_product=existing)
        apply_master_product_import(owner=self.owner, decisions=[self._replace(existing)])
        item.refresh_from_db()
        self.assertEqual(item.master_product_id, existing.id)

    def test_replace_flags_linked_venue_items_for_review(self):
        venue = Venue.objects.create(display_name="Cafe Replace Review")
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        item = VenueProductMatrixItem.objects.create(venue=venue, requested_name="Сливки", master_product=existing)
        apply_master_product_import(owner=self.owner, decisions=[self._replace(existing)])
        item.refresh_from_db()
        self.assertTrue(item.needs_review)

    def test_replace_does_not_flag_archived_or_unrelated_venue_items(self):
        venue = Venue.objects.create(display_name="Cafe Replace Review 2")
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        other = MasterProduct.objects.create(owner=self.owner, canonical_name="Молоко")
        archived_item = VenueProductMatrixItem.objects.create(
            venue=venue, requested_name="Сливки старая", master_product=existing, status=EntityStatus.ARCHIVED
        )
        unrelated_item = VenueProductMatrixItem.objects.create(
            venue=venue, requested_name="Молоко", master_product=other
        )
        apply_master_product_import(owner=self.owner, decisions=[self._replace(existing)])
        archived_item.refresh_from_db()
        unrelated_item.refresh_from_db()
        self.assertFalse(archived_item.needs_review)
        self.assertFalse(unrelated_item.needs_review)

    def test_replace_without_unit_id_raises_and_writes_nothing(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        decision = self._replace(existing)
        decision["unit_id"] = None
        with self.assertRaises(ValidationError):
            apply_master_product_import(owner=self.owner, decisions=[decision])
        existing.refresh_from_db()
        self.assertEqual(existing.canonical_name, "Сливки")

    def test_replace_with_foreign_or_archived_target_raises_and_writes_nothing(self):
        stranger = User.objects.create(
            email="import-replace-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        foreign = MasterProduct.objects.create(owner=stranger, canonical_name="Чужой")
        archived = MasterProduct.objects.create(
            owner=self.owner, canonical_name="Архивный", status=EntityStatus.ARCHIVED
        )
        for target in (foreign, archived):
            with self.subTest(target=target.canonical_name):
                with self.assertRaises(ValidationError):
                    apply_master_product_import(
                        owner=self.owner,
                        decisions=[{"raw_name": "Новый", "action": "create_new"}, self._replace(target)],
                    )
                target.refresh_from_db()
                self.assertNotEqual(target.canonical_name, "Сливки 33% новые")
                self.assertFalse(MasterProduct.objects.filter(owner=self.owner, canonical_name="Новый").exists())

    def test_replace_with_archived_unit_raises(self):
        archived_unit = Unit.objects.create(
            code="old-replace", name="Старая", kind="piece", status=EntityStatus.ARCHIVED
        )
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        with self.assertRaises(ValidationError):
            apply_master_product_import(owner=self.owner, decisions=[self._replace(existing, unit=archived_unit)])

    def test_replace_with_same_name_and_unit_writes_nothing(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки", unit=self.unit)
        before = existing.updated_at
        rows = apply_master_product_import(owner=self.owner, decisions=[self._replace(existing, raw_name="Сливки")])
        self.assertEqual(rows[0].action, "replace")
        existing.refresh_from_db()
        self.assertEqual(existing.updated_at, before)

    def test_replace_writes_audit_entry(self):
        litre = Unit.objects.create(code="l-audit", name="Литр", kind="volume", status=EntityStatus.ACTIVE)
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки", unit=self.unit)
        apply_master_product_import(owner=self.owner, decisions=[self._replace(existing, unit=litre)], actor=self.owner)
        entry = AuditLog.objects.get(entity_id=existing.id, action=AuditLogAction.REPLACED)
        self.assertEqual(entry.entity_type, AuditLogEntityType.MASTER_PRODUCT)
        self.assertEqual(entry.scope_id, self.owner.id)
        self.assertEqual(entry.actor_type, AuditActorType.CORE_USER)
        self.assertEqual(entry.actor_id, self.owner.id)
        self.assertEqual(entry.summary, "«Сливки» (Килограмм) → «Сливки 33% новые» (Литр)")

    def test_replace_from_no_unit_logs_dash(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        apply_master_product_import(owner=self.owner, decisions=[self._replace(existing)], actor=self.owner)
        entry = AuditLog.objects.get(entity_id=existing.id, action=AuditLogAction.REPLACED)
        self.assertEqual(entry.summary, "«Сливки» (—) → «Сливки 33% новые» (Килограмм)")

    def test_unchanged_replace_writes_no_audit_entry(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки", unit=self.unit)
        apply_master_product_import(
            owner=self.owner, decisions=[self._replace(existing, raw_name="Сливки")], actor=self.owner
        )
        self.assertFalse(AuditLog.objects.filter(entity_id=existing.id).exists())

    def test_two_replaces_of_one_target_first_wins_second_conflicts_rest_of_batch_applies(self):
        """2026-09-29 (unified-import-architecture spec §9.1): было
        whole-batch-abort (TASK-73), стало per-row, same shape as
        supplier-commit -- первая replace-декларация на target применяется,
        вторая возвращается с conflict, независимый create_new в той же
        пачке не страдает."""
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        rows = apply_master_product_import(
            owner=self.owner,
            decisions=[
                {"raw_name": "Новый товар", "action": "create_new"},
                self._replace(existing, raw_name="Сливки 33%"),
                self._replace(existing, raw_name="Сливки 35%"),
            ],
            actor=self.owner,
        )
        self.assertIsNone(rows[0].conflict)
        self.assertIsNone(rows[1].conflict)
        self.assertIsNotNone(rows[2].conflict)
        self.assertIn(str(existing.id), rows[2].conflict)
        existing.refresh_from_db()
        self.assertEqual(existing.canonical_name, "Сливки 33%")
        self.assertTrue(MasterProduct.objects.filter(owner=self.owner, canonical_name="Новый товар").exists())
        self.assertTrue(AuditLog.objects.filter(entity_id=existing.id).exists())

    def test_replace_and_update_existing_of_one_target_are_allowed(self):
        existing = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        rows = apply_master_product_import(
            owner=self.owner,
            decisions=[
                self._replace(existing, raw_name="Сливки 33%"),
                {"raw_name": "Сливки жирные", "action": "update_existing", "master_product_id": existing.id},
            ],
        )
        self.assertEqual([row.action for row in rows], ["replace", "update_existing"])

    def test_replaces_of_different_targets_are_allowed(self):
        first = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        second = MasterProduct.objects.create(owner=self.owner, canonical_name="Молоко")
        apply_master_product_import(
            owner=self.owner,
            decisions=[self._replace(first, raw_name="Сливки 33%"), self._replace(second, raw_name="Молоко 3,2%")],
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.canonical_name, second.canonical_name), ("Сливки 33%", "Молоко 3,2%"))


class MasterProductVenueBatchCurrentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="batch-current@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue_a = Venue.objects.create(display_name="Cafe Batch A")
        self.venue_b = Venue.objects.create(display_name="Cafe Batch B")
        for venue in (self.venue_a, self.venue_b):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.mp = create_master_product(owner=self.owner, canonical_name="Баклажан африка")
        self.item_a = link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)

    def test_returns_only_requested_venues_with_live_objects(self):
        current = master_product_venue_batch_current(
            master_product=self.mp, venue_ids=[self.venue_a.id, self.venue_b.id]
        )
        self.assertEqual(set(current), {self.venue_a.id, self.venue_b.id})
        self.assertTrue(current[self.venue_a.id].linked)
        self.assertEqual(current[self.venue_a.id].matrix_item.id, self.item_a.id)
        self.assertEqual(current[self.venue_a.id].venue, self.venue_a)
        self.assertEqual(current[self.venue_a.id].venue_name, self.venue_a.display_name)
        self.assertFalse(current[self.venue_b.id].linked)
        self.assertIsNone(current[self.venue_b.id].matrix_item)
        self.assertEqual(current[self.venue_b.id].venue, self.venue_b)
        self.assertEqual(current[self.venue_b.id].venue_name, self.venue_b.display_name)

    def test_handles_unmatched_venue_id(self):
        unmatched_id = uuid4()
        current = master_product_venue_batch_current(master_product=self.mp, venue_ids=[unmatched_id])
        self.assertEqual(set(current), {unmatched_id})
        self.assertIsNone(current[unmatched_id].venue)
        self.assertIsNone(current[unmatched_id].venue_name)
        self.assertFalse(current[unmatched_id].linked)
        self.assertIsNone(current[unmatched_id].matrix_item)
        self.assertIsNone(current[unmatched_id].matrix_item_id)

    def test_handles_archived_venue(self):
        archived_venue = Venue.objects.create(display_name="Archived Venue", status=EntityStatus.ARCHIVED)
        current = master_product_venue_batch_current(master_product=self.mp, venue_ids=[archived_venue.id])
        self.assertEqual(set(current), {archived_venue.id})
        self.assertIsNone(current[archived_venue.id].venue)
        self.assertIsNone(current[archived_venue.id].venue_name)
        self.assertFalse(current[archived_venue.id].linked)
        self.assertIsNone(current[archived_venue.id].matrix_item)
        self.assertIsNone(current[archived_venue.id].matrix_item_id)


class ApplyMasterProductVenueBatchTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="batch-apply@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue_a = Venue.objects.create(display_name="Cafe Batch C")
        self.venue_b = Venue.objects.create(display_name="Cafe Batch D")
        for venue in (self.venue_a, self.venue_b):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.mp = create_master_product(owner=self.owner, canonical_name="Баклажан африка")
        self.supplier = Supplier.objects.create(display_name="Поставщик Batch")
        self.sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Баклажан",
            normalized_name="Баклажан",
            current_price=Decimal("100.00"),
        )

    def test_preview_does_not_write(self):
        rows = master_product_venue_batch_preview(
            master_product=self.mp, changes=[{"venue_id": self.venue_a.id, "link_action": "link", "force_create": True}]
        )
        self.assertEqual(rows[0].link_action, "link")
        self.assertTrue(rows[0].changed)
        self.assertFalse(VenueProductMatrixItem.objects.filter(venue=self.venue_a, master_product=self.mp).exists())

    def test_apply_link_with_force_create_then_set_supplier_product_in_one_call(self):
        rows = apply_master_product_venue_batch(
            master_product=self.mp,
            changes=[
                {
                    "venue_id": self.venue_a.id,
                    "link_action": "link",
                    "force_create": True,
                    "supplier_product_id": self.sp.id,
                }
            ],
        )
        item = VenueProductMatrixItem.objects.get(venue=self.venue_a, master_product=self.mp)
        self.assertEqual(item.supplier_product_id, self.sp.id)
        self.assertEqual(rows[0].new_matrix_item_id, item.id)
        self.assertEqual(rows[0].new_supplier_product_id, self.sp.id)
        self.assertEqual(rows[0].new_supplier_product_name, self.sp.normalized_name)

    def test_apply_keep_only_changes_supplier_product(self):
        item = link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)
        rows = apply_master_product_venue_batch(
            master_product=self.mp,
            changes=[{"venue_id": self.venue_a.id, "link_action": "keep", "supplier_product_id": self.sp.id}],
        )
        item.refresh_from_db()
        self.assertEqual(item.supplier_product_id, self.sp.id)
        self.assertTrue(rows[0].changed)
        self.assertEqual(rows[0].current_matrix_item_id, item.id)
        self.assertIsNone(rows[0].current_supplier_product_id)

    def test_apply_omitted_supplier_product_id_leaves_it_unchanged(self):
        item = link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)
        item.supplier_product = self.sp
        item.save(update_fields=["supplier_product"])
        rows = apply_master_product_venue_batch(
            master_product=self.mp, changes=[{"venue_id": self.venue_a.id, "link_action": "keep"}]
        )
        item.refresh_from_db()
        self.assertEqual(item.supplier_product_id, self.sp.id)
        self.assertFalse(rows[0].changed)

    def test_apply_unlink(self):
        link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)
        rows = apply_master_product_venue_batch(
            master_product=self.mp, changes=[{"venue_id": self.venue_a.id, "link_action": "unlink"}]
        )
        self.assertIsNone(rows[0].new_matrix_item_id)
        self.assertTrue(rows[0].changed)
        self.assertFalse(
            VenueProductMatrixItem.objects.filter(
                venue=self.venue_a, master_product=self.mp, status=EntityStatus.ACTIVE
            ).exists()
        )

    def test_apply_two_venues_in_one_atomic_call(self):
        rows = apply_master_product_venue_batch(
            master_product=self.mp,
            changes=[
                {"venue_id": self.venue_a.id, "link_action": "link", "force_create": True},
                {"venue_id": self.venue_b.id, "link_action": "link", "force_create": True},
            ],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(VenueProductMatrixItem.objects.filter(master_product=self.mp).count(), 2)

    def test_unlink_with_supplier_product_id_is_rejected(self):
        link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)
        with self.assertRaises(ValidationError):
            apply_master_product_venue_batch(
                master_product=self.mp,
                changes=[{"venue_id": self.venue_a.id, "link_action": "unlink", "supplier_product_id": self.sp.id}],
            )

    def test_keep_with_supplier_product_id_but_not_linked_is_rejected(self):
        with self.assertRaises(ValidationError):
            apply_master_product_venue_batch(
                master_product=self.mp,
                changes=[{"venue_id": self.venue_a.id, "link_action": "keep", "supplier_product_id": self.sp.id}],
            )

    def test_link_conflict_aborts_whole_batch(self):
        # §16 duplicate check (TASK-33): an unlinked matrix item with the exact
        # same name as the master product blocks force_create=False link.
        # venue_b is deliberately FIRST so its write is already done when
        # venue_a raises — the assertion below then really proves rollback.
        VenueProductMatrixItem.objects.create(
            venue=self.venue_a, requested_name="Баклажан африка", status=EntityStatus.ACTIVE
        )
        with self.assertRaises(MasterProductVenueLinkConflict):
            apply_master_product_venue_batch(
                master_product=self.mp,
                changes=[
                    {"venue_id": self.venue_b.id, "link_action": "link", "force_create": True},
                    {"venue_id": self.venue_a.id, "link_action": "link"},
                ],
            )
        self.assertFalse(VenueProductMatrixItem.objects.filter(venue=self.venue_b, master_product=self.mp).exists())

    def test_venue_not_owners_active_venue_raises(self):
        stranger_venue = Venue.objects.create(display_name="Stranger Cafe")
        with self.assertRaises(ValidationError):
            apply_master_product_venue_batch(
                master_product=self.mp,
                changes=[{"venue_id": stranger_venue.id, "link_action": "link", "force_create": True}],
            )

    def test_archived_venue_of_the_owner_is_rejected(self):
        archived = Venue.objects.create(display_name="Archived Batch Cafe", status=EntityStatus.ARCHIVED)
        VenueUser.objects.create(venue=archived, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        with self.assertRaises(ValidationError):
            apply_master_product_venue_batch(
                master_product=self.mp,
                changes=[{"venue_id": archived.id, "link_action": "link", "force_create": True}],
            )

    def test_preview_and_apply_return_the_same_rows(self):
        changes = [
            {"venue_id": self.venue_a.id, "link_action": "link", "force_create": True},
            {"venue_id": self.venue_b.id, "link_action": "keep"},
        ]
        preview_rows = master_product_venue_batch_preview(master_product=self.mp, changes=changes)
        apply_rows = apply_master_product_venue_batch(master_product=self.mp, changes=changes)
        self.assertEqual(
            [(row.venue_id, row.link_action, row.changed) for row in preview_rows],
            [(row.venue_id, row.link_action, row.changed) for row in apply_rows],
        )


class MasterProductVenueBatchPreviewRollbackTests(TransactionTestCase):
    """The preview's rollback path differs depending on whether an outer
    transaction is already open: inside TestCase (and inside a view that
    wraps the call in atomic()) transaction.set_rollback(True) only unwinds
    a savepoint, while in a plain request — this project sets no
    ATOMIC_REQUESTS — the preview's own atomic() is the outermost block and
    the rollback is a real connection.rollback(). TransactionTestCase is the
    only way to exercise that second branch."""

    def setUp(self):
        self.owner = User.objects.create(
            email="batch-preview-rollback@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Batch Rollback")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.mp = create_master_product(owner=self.owner, canonical_name="Баклажан африка")

    def test_preview_writes_nothing_without_an_outer_transaction(self):
        self.assertFalse(connection.in_atomic_block)
        rows = master_product_venue_batch_preview(
            master_product=self.mp, changes=[{"venue_id": self.venue.id, "link_action": "link", "force_create": True}]
        )
        self.assertTrue(rows[0].changed)
        self.assertIsNotNone(rows[0].new_matrix_item_id)
        self.assertFalse(VenueProductMatrixItem.objects.filter(venue=self.venue, master_product=self.mp).exists())


class MasterProductVenueBatchAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="batch-api@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue_a = Venue.objects.create(display_name="Cafe Batch API A")
        self.venue_b = Venue.objects.create(display_name="Cafe Batch API B")
        for venue in (self.venue_a, self.venue_b):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.mp = create_master_product(owner=self.owner, canonical_name="Горчица дижонская")
        self.supplier = Supplier.objects.create(display_name="Поставщик Batch API")
        self.sp = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Горчица", normalized_name="Горчица", current_price=Decimal("50.00")
        )
        self.preview_url = f"/api/v1/catalog/master-products/{self.mp.id}/venues/batch/preview"
        self.apply_url = f"/api/v1/catalog/master-products/{self.mp.id}/venues/batch/apply"

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def test_preview_shows_diff_without_writing(self):
        self.auth()
        response = self.client.post(
            self.preview_url,
            {"changes": [{"venue_id": str(self.venue_a.id), "link_action": "link", "force_create": True}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["rows"][0]["changed"])
        self.assertFalse(VenueProductMatrixItem.objects.filter(venue=self.venue_a, master_product=self.mp).exists())

    def test_apply_links_and_sets_supplier_product_together(self):
        self.auth()
        response = self.client.post(
            self.apply_url,
            {
                "changes": [
                    {
                        "venue_id": str(self.venue_a.id),
                        "link_action": "link",
                        "force_create": True,
                        "supplier_product_id": str(self.sp.id),
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        item = VenueProductMatrixItem.objects.get(venue=self.venue_a, master_product=self.mp)
        self.assertEqual(item.supplier_product_id, self.sp.id)

    def test_apply_omitted_supplier_product_id_leaves_existing_item_unchanged(self):
        # Controller ruling (Task 7): the "omitted vs. present" distinction
        # is carried by PrimaryKeyRelatedField(source="supplier_product") —
        # this exercises it end-to-end through the view, not just the
        # service layer (already covered by
        # ApplyMasterProductVenueBatchTests.test_apply_omitted_supplier_product_id_leaves_it_unchanged).
        self.auth()
        item = link_master_product_to_venue(master_product=self.mp, venue=self.venue_a)
        item.supplier_product = self.sp
        item.save(update_fields=["supplier_product"])
        response = self.client.post(
            self.apply_url,
            {"changes": [{"venue_id": str(self.venue_a.id), "link_action": "keep"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertEqual(item.supplier_product_id, self.sp.id)
        self.assertFalse(response.json()["rows"][0]["changed"])

    def test_apply_unknown_supplier_product_id_returns_400_not_404(self):
        # Controller ruling (Task 7): supplier_product_id is validated by
        # the serializer's PrimaryKeyRelatedField, so an unknown/inactive id
        # must come back as a 400 field-level error, never a bare 404.
        self.auth()
        response = self.client.post(
            self.apply_url,
            {
                "changes": [
                    {
                        "venue_id": str(self.venue_a.id),
                        "link_action": "link",
                        "force_create": True,
                        "supplier_product_id": str(uuid4()),
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("supplier_product_id", str(response.json()["changes"][0]))
        self.assertFalse(VenueProductMatrixItem.objects.filter(venue=self.venue_a, master_product=self.mp).exists())

    def test_apply_two_venues_atomic_rollback_on_conflict(self):
        self.auth()
        VenueProductMatrixItem.objects.create(
            venue=self.venue_a, requested_name="Горчица дижонская", status=EntityStatus.ACTIVE
        )
        response = self.client.post(
            self.apply_url,
            {
                "changes": [
                    {"venue_id": str(self.venue_a.id), "link_action": "link"},
                    {"venue_id": str(self.venue_b.id), "link_action": "link", "force_create": True},
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(VenueProductMatrixItem.objects.filter(venue=self.venue_b, master_product=self.mp).exists())

    def test_denied_for_stranger(self):
        stranger = User.objects.create(
            email="batch-api-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(stranger))
        response = self.client.post(
            self.preview_url, {"changes": [{"venue_id": str(self.venue_a.id), "link_action": "keep"}]}, format="json"
        )
        self.assertEqual(response.status_code, 403)


class ProcessMasterProductImportUploadTaskTests(TransactionTestCase):
    """TransactionTestCase, not TestCase (deviates from the original plan
    draft): the task calls close_old_connections() at entry and in its
    finally block, exactly like api/v1/suppliers/tasks.py's tasks — but
    those are only ever tested through mocked .delay() calls, never invoked
    directly. Calling this task function body directly (as these tests do,
    since it's exercised synchronously rather than through a real Celery
    worker) under a plain TestCase's atomic-wrapped test breaks: the atomic
    block leaves autocommit False while settings AUTOCOMMIT stays True, and
    close_if_unusable_or_obsolete() (base.py) closes the connection outright
    on that mismatch, breaking every following query with "the connection is
    closed". TransactionTestCase avoids the mismatch (real commits, no
    atomic wrapping) — same reasoning as ApplyProgressReporterTests in
    api/v1/suppliers/tests.py, which documents the identical class of
    problem for connection-visibility tests."""

    def setUp(self):
        self.owner = User.objects.create(
            email="import-task-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.upload = MasterProductImportUpload.objects.create(owner=self.owner, rows_total=3)

    def _row(self, n):
        return {"row_number": n, "name": f"Товар {n}", "unit": None, "net": None, "note": None}

    def test_processes_rows_in_batches_and_marks_ready(self):
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        rows = [self._row(1), self._row(2), self._row(3)]
        parsed_meta = {
            "sheet_name": "Sheet1",
            "header_row_number": 1,
            "detected_columns": {"name": "Название"},
            "rows_without_name": 0,
        }

        with override_settings(MASTER_PRODUCT_IMPORT_BATCH_SIZE=2):
            with patch("api.v1.catalog.tasks.resolve_master_product_import_rows") as mock_resolve:

                def fake_resolve(*, owner, rows):
                    return [
                        SimpleNamespace(
                            row_number=row["row_number"],
                            raw_name=row["name"],
                            unit_label=None,
                            unit_id=None,
                            unit_unrecognized=False,
                            unit_source=None,
                            net_value=None,
                            note=None,
                            action="create_new",
                            candidates=[],
                            candidate_master_product_id=None,
                            candidate_canonical_name=None,
                            score=None,
                            needs_attention=False,
                        )
                        for row in rows
                    ]

                mock_resolve.side_effect = fake_resolve

                process_master_product_import_upload_task(
                    upload_id=str(self.upload.id), owner_id=str(self.owner.id), rows=rows, parsed_meta=parsed_meta
                )

                self.assertEqual(mock_resolve.call_count, 2)  # batch of 2 + batch of 1

        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, MasterProductImportUploadStatus.READY)
        self.assertEqual(self.upload.rows_done, 3)
        self.assertIsNotNone(self.upload.processed_at)
        self.assertEqual(len(self.upload.result["rows"]), 3)
        self.assertEqual(self.upload.result["total_rows"], 3)
        self.assertEqual(self.upload.result["auto_resolved_count"], 3)
        self.assertEqual(self.upload.result["needs_attention_count"], 0)

    def test_progress_reaches_full_when_rows_without_name_were_dropped(self):
        """Fix 1 (final-review finding): rows_total (set by
        start_master_product_import) includes rows_without_name, but the
        task's batch loop only ever counted processed rows — so a file with
        at least one blank-name row left rows_done permanently below
        rows_total even once processing finished, and percent never hit 100
        for a ready upload."""
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        upload = MasterProductImportUpload.objects.create(owner=self.owner, rows_total=5)
        rows = [self._row(1), self._row(2), self._row(3)]
        parsed_meta = {
            "sheet_name": None,
            "header_row_number": None,
            "detected_columns": {},
            "rows_without_name": 2,
        }

        def fake_resolve(*, owner, rows):
            return [
                SimpleNamespace(
                    row_number=row["row_number"],
                    raw_name=row["name"],
                    unit_label=None,
                    unit_id=None,
                    unit_unrecognized=False,
                    unit_source=None,
                    net_value=None,
                    note=None,
                    action="create_new",
                    candidates=[],
                    candidate_master_product_id=None,
                    candidate_canonical_name=None,
                    score=None,
                    needs_attention=False,
                )
                for row in rows
            ]

        with patch("api.v1.catalog.tasks.resolve_master_product_import_rows", side_effect=fake_resolve):
            process_master_product_import_upload_task(
                upload_id=str(upload.id), owner_id=str(self.owner.id), rows=rows, parsed_meta=parsed_meta
            )

        upload.refresh_from_db()
        self.assertEqual(upload.status, MasterProductImportUploadStatus.READY)
        self.assertEqual(upload.rows_done, 5)
        self.assertEqual(upload.rows_total, 5)
        percent = MasterProductImportUploadStatusResponseSerializer(upload).data["percent"]
        self.assertEqual(percent, 100)

    def test_marks_failed_on_exception_and_reraises(self):
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        rows = [self._row(1)]
        parsed_meta = {"sheet_name": None, "header_row_number": None, "detected_columns": {}, "rows_without_name": 0}

        with patch("api.v1.catalog.tasks.resolve_master_product_import_rows", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                process_master_product_import_upload_task(
                    upload_id=str(self.upload.id), owner_id=str(self.owner.id), rows=rows, parsed_meta=parsed_meta
                )

        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, MasterProductImportUploadStatus.FAILED)
        self.assertIn("boom", self.upload.error_message)
        self.assertIsNotNone(self.upload.processed_at)

    def test_failed_error_message_unwraps_drf_validation_error(self):
        """Fix 2b (defense in depth, final-review finding): if a DRF
        ValidationError still reaches the task (e.g. from a code path Fix 2a
        doesn't cover), error_message must be a readable string, not Python's
        raw repr of the exception (which would contain "ErrorDetail" and
        curly braces)."""
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        rows = [self._row(1)]
        parsed_meta = {"sheet_name": None, "header_row_number": None, "detected_columns": {}, "rows_without_name": 0}
        error = ValidationError({"file": ["Строка 5: некорректное значение нетто: abc."]})

        with patch("api.v1.catalog.tasks.resolve_master_product_import_rows", side_effect=error):
            with self.assertRaises(ValidationError):
                process_master_product_import_upload_task(
                    upload_id=str(self.upload.id), owner_id=str(self.owner.id), rows=rows, parsed_meta=parsed_meta
                )

        self.upload.refresh_from_db()
        self.assertEqual(self.upload.error_message, "Строка 5: некорректное значение нетто: abc.")
        self.assertNotIn("ErrorDetail", self.upload.error_message)
        self.assertNotIn("{", self.upload.error_message)

    def test_admin_reset_during_processing_is_not_overwritten(self):
        """Fix 4 (final-review finding): the admin's reset_stuck_processing
        action (catalog/admin.py) can flip a processing upload to failed
        mid-run, believing the worker died. If the worker is actually still
        alive and finishes after that manual reset, the task's own final
        write must not silently overwrite the admin's intervention. This
        targets the race where the reset happens *after* the task's entry
        check already passed (a reset before entry is already handled by
        that check) — simulated here as a side effect of the mocked
        resolve_master_product_import_rows call, since that's the only
        place to inject work "during" the task's single-batch run."""
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        rows = [self._row(1)]
        parsed_meta = {"sheet_name": None, "header_row_number": None, "detected_columns": {}, "rows_without_name": 0}

        def fake_resolve_and_reset(*, owner, rows):
            MasterProductImportUpload.objects.filter(pk=self.upload.id).update(
                status=MasterProductImportUploadStatus.FAILED, error_message="reset by admin"
            )
            return [
                SimpleNamespace(
                    row_number=1,
                    raw_name="x",
                    unit_label=None,
                    unit_id=None,
                    unit_unrecognized=False,
                    unit_source=None,
                    net_value=None,
                    note=None,
                    action="create_new",
                    candidates=[],
                    candidate_master_product_id=None,
                    candidate_canonical_name=None,
                    score=None,
                    needs_attention=False,
                )
            ]

        with patch("api.v1.catalog.tasks.resolve_master_product_import_rows", side_effect=fake_resolve_and_reset):
            process_master_product_import_upload_task(
                upload_id=str(self.upload.id), owner_id=str(self.owner.id), rows=rows, parsed_meta=parsed_meta
            )

        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, MasterProductImportUploadStatus.FAILED)
        self.assertEqual(self.upload.error_message, "reset by admin")

    def test_skips_processing_when_status_is_not_processing(self):
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        self.upload.status = MasterProductImportUploadStatus.READY
        self.upload.result = {"rows": []}
        self.upload.save(update_fields=["status", "result"])

        with patch("api.v1.catalog.tasks.resolve_master_product_import_rows") as mock_resolve:
            result = process_master_product_import_upload_task(
                upload_id=str(self.upload.id),
                owner_id=str(self.owner.id),
                rows=[self._row(1)],
                parsed_meta={
                    "sheet_name": None,
                    "header_row_number": None,
                    "detected_columns": {},
                    "rows_without_name": 0,
                },
            )
            mock_resolve.assert_not_called()

        self.assertTrue(result["skipped"])


class StartMasterProductImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="import-start-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )

    def test_creates_processing_upload_and_enqueues_task(self):
        from api.v1.catalog.services import start_master_product_import

        with patch("api.v1.catalog.tasks.process_master_product_import_upload_task.delay") as mock_delay:
            upload = start_master_product_import(
                owner=self.owner,
                source_file_name="lir.xlsx",
                rows=[{"row_number": 1, "name": "A"}],
                parsed_meta={
                    "sheet_name": "S1",
                    "header_row_number": 1,
                    "detected_columns": {},
                    "rows_without_name": 0,
                },
            )

        self.assertEqual(upload.owner_id, self.owner.id)
        self.assertEqual(upload.status, MasterProductImportUploadStatus.PROCESSING)
        self.assertEqual(upload.rows_total, 1)
        mock_delay.assert_called_once_with(
            upload_id=str(upload.id),
            owner_id=str(self.owner.id),
            rows=[{"row_number": 1, "name": "A"}],
            parsed_meta={"sheet_name": "S1", "header_row_number": 1, "detected_columns": {}, "rows_without_name": 0},
        )


class CatalogImportSessionAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="cis-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(
            email="cis-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.unit = Unit.objects.create(code="kg", name="Килограмм", kind="weight", status=EntityStatus.ACTIVE)
        self.session = CatalogImportSession.objects.create(
            owner=self.owner, domain=CatalogImportDomain.MASTER, source_file_name="m.xlsx"
        )
        self.item = CatalogImportItem.objects.create(
            session=self.session,
            row_number=2,
            raw_values={"row_number": 2, "raw_name": "Мука"},
            normalized_values={"row_number": 2, "raw_name": "Мука"},
            action=CatalogImportItemAction.CREATE_NEW,
        )
        self.url = f"/api/v1/catalog/import-sessions/{self.session.id}"
        self.item_url = f"{self.url}/items/{self.item.id}"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def test_get_returns_session_with_items(self):
        self.auth(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["id"], str(self.session.id))
        self.assertEqual((payload["domain"], payload["status"]), ("master", "open"))
        self.assertEqual(payload["source_file_name"], "m.xlsx")
        self.assertEqual([item["id"] for item in payload["items"]], [str(self.item.id)])
        self.assertEqual(payload["items"][0]["action"], "create_new")

    def test_get_of_another_users_session_is_404(self):
        self.auth(self.stranger)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_patch_changes_action(self):
        self.auth(self.owner)
        response = self.client.patch(self.item_url, {"action": "skip"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.action, "skip")

    def test_patch_rejects_unknown_action(self):
        self.auth(self.owner)
        response = self.client.patch(self.item_url, {"action": "explode"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)

    def test_commit_with_unreviewed_rows_is_400(self):
        CatalogImportItem.objects.filter(pk=self.item.pk).update(action=CatalogImportItemAction.REVIEW)
        self.auth(self.owner)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), {"items": "Сначала примите решение по всем строкам."})

    def test_get_exposes_lifecycle_fields_for_a_ready_session(self):
        self.auth(self.owner)
        payload = self.client.get(self.url).json()
        self.assertEqual(payload["processing_status"], "ready")
        self.assertEqual((payload["rows_total"], payload["rows_done"], payload["percent"]), (0, 0, 100))
        self.assertIsNone(payload["error_message"])
        self.assertEqual(payload["meta"], {})

    def test_percent_follows_rows_done_while_processing(self):
        CatalogImportSession.objects.filter(pk=self.session.pk).update(
            processing_status="processing", rows_total=200, rows_done=50
        )
        self.auth(self.owner)
        payload = self.client.get(self.url).json()
        self.assertEqual((payload["processing_status"], payload["percent"]), ("processing", 25))

    def test_commit_and_patch_are_409_until_the_session_is_ready(self):
        self.auth(self.owner)
        for state, message in (
            ("pending", "Импорт ещё обрабатывается."),
            ("processing", "Импорт ещё обрабатывается."),
            ("failed", "Обработка импорта завершилась ошибкой."),
        ):
            CatalogImportSession.objects.filter(pk=self.session.pk).update(processing_status=state)
            commit = self.client.post(self.url, {}, format="json")
            patch_response = self.client.patch(self.item_url, {"action": "skip"}, format="json")
            for response in (commit, patch_response):
                self.assertEqual(response.status_code, 409, (state, response.content))
                self.assertEqual(response.json(), {"detail": message})

    def test_failed_session_shows_error_message(self):
        CatalogImportSession.objects.filter(pk=self.session.pk).update(
            processing_status="failed", error_message="Не удалось прочитать файл."
        )
        self.auth(self.owner)
        payload = self.client.get(self.url).json()
        self.assertEqual(
            (payload["processing_status"], payload["error_message"]), ("failed", "Не удалось прочитать файл.")
        )

    def test_anonymous_request_is_rejected_not_asked_for_a_service_token(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401, response.content)

    def _master(self, name="Мука", owner=None):
        return MasterProduct.objects.create(owner=owner or self.owner, canonical_name=name, unit=self.unit)

    def test_patch_target_captures_expected_updated_at_from_the_chosen_card(self):
        first, second = self._master("Мука А"), self._master("Мука Б")
        CatalogImportItem.objects.filter(pk=self.item.pk).update(
            target_id=first.id, target_expected_updated_at=first.updated_at
        )
        self.auth(self.owner)
        response = self.client.patch(
            self.item_url, {"action": "update_existing", "target_id": str(second.id)}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.target_id, second.id)
        self.assertEqual(self.item.target_expected_updated_at, second.updated_at)

    def test_changing_target_does_not_cause_a_false_conflict_on_commit(self):
        first, second = self._master("Мука А"), self._master("Мука Б")
        CatalogImportItem.objects.filter(pk=self.item.pk).update(
            target_id=first.id, target_expected_updated_at=first.updated_at, action="update_existing"
        )
        self.auth(self.owner)
        self.client.patch(self.item_url, {"action": "update_existing", "target_id": str(second.id)}, format="json")
        with patch("api.v1.catalog.views.import_sessions.apply_master_product_import", return_value=[]) as mock_apply:
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        sent = mock_apply.call_args.kwargs["decisions"][0]
        self.assertEqual(sent["master_product_id"], second.id)

    def test_create_new_and_skip_clear_the_target(self):
        card = self._master()
        CatalogImportItem.objects.filter(pk=self.item.pk).update(
            target_id=card.id, target_expected_updated_at=card.updated_at, action="update_existing"
        )
        self.auth(self.owner)
        self.client.patch(self.item_url, {"action": "create_new"}, format="json")
        self.item.refresh_from_db()
        self.assertIsNone(self.item.target_id)
        self.assertIsNone(self.item.target_expected_updated_at)

    def test_target_of_another_owner_is_rejected(self):
        foreign = self._master("Чужая", owner=self.stranger)
        self.auth(self.owner)
        response = self.client.patch(
            self.item_url, {"action": "update_existing", "target_id": str(foreign.id)}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), {"target_id": "Цель не найдена, чужая или архивная."})

    def test_unknown_target_is_400(self):
        self.auth(self.owner)
        response = self.client.patch(
            self.item_url, {"action": "update_existing", "target_id": str(uuid.uuid4())}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_malformed_target_id_is_400_not_500(self):
        self.auth(self.owner)
        response = self.client.patch(self.item_url, {"target_id": "not-a-uuid"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)

    def test_update_existing_requires_a_target(self):
        self.auth(self.owner)
        response = self.client.patch(self.item_url, {"action": "update_existing"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), {"target_id": "Для update_existing нужна цель."})

    def test_commit_of_finished_session_is_400(self):
        CatalogImportSession.objects.filter(pk=self.session.pk).update(status=CatalogImportSessionStatus.COMMITTED)
        self.auth(self.owner)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), {"session": "Сессия уже завершена."})


class TemporaryMediaRootMixin:
    @classmethod
    def setUpClass(cls):
        cls._temporary_media_root = tempfile.mkdtemp(prefix="autosnab-test-media-")
        cls._temporary_media_override = override_settings(MEDIA_ROOT=cls._temporary_media_root)
        cls._temporary_media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._temporary_media_override.disable()
        shutil.rmtree(cls._temporary_media_root, ignore_errors=True)


@override_settings(
    IMPORT_FILE_STORAGE_ENABLED=True,
    VENUE_IMPORT_ASYNC_ENABLED=True,
    VENUE_IMPORT_ASYNC_THRESHOLD_ROWS=2,
    VENUE_IMPORT_BATCH_SIZE=2,
)
class VenueImportAsyncPreviewTests(TemporaryMediaRootMixin, APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="venue-async@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Async")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/file/preview"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        patcher = patch("api.v1.catalog.tasks.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _upload(self, names):
        return _xlsx_upload([["Название"], *[[name] for name in names]], name="venue.xlsx")

    def _start(self, names):
        with patch("api.v1.catalog.tasks.process_venue_import_session_task.delay"):
            return self.client.post(self.url, {"file": self._upload(names)}, format="multipart")

    @patch("api.v1.catalog.tasks.process_venue_import_session_task.delay")
    def test_large_file_returns_202_and_creates_pending_session(self, mock_delay):
        response = self.client.post(self.url, {"file": self._upload(["Мука", "Сахар", "Соль"])}, format="multipart")
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        session = CatalogImportSession.objects.get(pk=body["session_id"])
        self.assertEqual((body["status"], session.processing_status), ("pending", "pending"))
        self.assertEqual(body["status_url"], f"/api/v1/catalog/import-sessions/{session.id}")
        self.assertEqual((session.domain, session.venue_id, session.rows_total), ("venue", self.venue.id, 3))
        self.assertIsNotNone(session.import_file)
        self.assertEqual(session.items.count(), 0)
        mock_delay.assert_called_once_with(session_id=str(session.id))

    @patch("api.v1.catalog.tasks.process_venue_import_session_task.delay")
    def test_small_file_keeps_the_synchronous_200(self, mock_delay):
        response = self.client.post(self.url, {"file": self._upload(["Мука", "Сахар"])}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["rows"]), 2)
        mock_delay.assert_not_called()

    @override_settings(VENUE_IMPORT_ASYNC_ENABLED=False)
    def test_flag_off_keeps_the_old_one_thousand_row_ceiling(self):
        names = [f"Товар {i}" for i in range(1001)]
        response = self.client.post(self.url, {"file": self._upload(names)}, format="multipart")
        self.assertEqual(response.status_code, 400, response.content)

    def test_task_creates_rows_in_batches_and_marks_ready(self):
        from api.v1.catalog.tasks import process_venue_import_session_task

        response = self._start(["Мука", "Сахар", "Соль", "Перец", "Чай"])
        session_id = response.json()["session_id"]
        process_venue_import_session_task.apply(kwargs={"session_id": session_id}, throw=True)

        session = CatalogImportSession.objects.get(pk=session_id)
        self.assertEqual((session.processing_status, session.rows_done, session.rows_total), ("ready", 5, 5))
        self.assertEqual(
            list(session.items.order_by("row_number").values_list("raw_values__raw_name", flat=True)),
            ["Мука", "Сахар", "Соль", "Перец", "Чай"],
        )
        self.assertEqual(session.meta["detected_columns"]["name"], "Название")

    def test_ready_session_is_reviewable_through_the_session_api(self):
        from api.v1.catalog.tasks import process_venue_import_session_task

        session_id = self._start(["Мука", "Сахар", "Соль"]).json()["session_id"]
        process_venue_import_session_task.apply(kwargs={"session_id": session_id}, throw=True)
        payload = self.client.get(f"/api/v1/catalog/import-sessions/{session_id}").json()
        self.assertEqual((payload["processing_status"], payload["percent"], len(payload["items"])), ("ready", 100, 3))

    def test_task_retry_does_not_duplicate_rows(self):
        from api.v1.catalog.tasks import process_venue_import_session_task

        session_id = self._start(["Мука", "Сахар", "Соль"]).json()["session_id"]
        process_venue_import_session_task.apply(kwargs={"session_id": session_id}, throw=True)
        CatalogImportSession.objects.filter(pk=session_id).update(processing_status="processing")
        process_venue_import_session_task.apply(kwargs={"session_id": session_id}, throw=True)
        self.assertEqual(CatalogImportItem.objects.filter(session_id=session_id).count(), 3)

    def test_expired_file_marks_session_failed(self):
        from api.v1.catalog.tasks import process_venue_import_session_task

        session = CatalogImportSession.objects.get(pk=self._start(["Мука", "Сахар", "Соль"]).json()["session_id"])
        ImportFile.objects.filter(pk=session.import_file_id).update(expires_at=timezone.now() - timedelta(seconds=1))
        process_venue_import_session_task.apply(kwargs={"session_id": str(session.id)}, throw=True)
        session.refresh_from_db()
        self.assertEqual(session.processing_status, "failed")
        self.assertEqual(
            session.error_message, "Исходный файл недоступен: срок хранения истёк. Загрузите файл ещё раз."
        )

    def test_unexpected_crash_marks_session_failed_and_reraises(self):
        from api.v1.catalog.tasks import process_venue_import_session_task

        session_id = self._start(["Мука", "Сахар", "Соль"]).json()["session_id"]
        with patch("api.v1.catalog.tasks.resolve_venue_import_rows", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                process_venue_import_session_task.apply(kwargs={"session_id": session_id}, throw=True).get()
        session = CatalogImportSession.objects.get(pk=session_id)
        self.assertEqual((session.processing_status, session.error_message), ("failed", "Не удалось обработать файл."))

    def test_broker_down_gives_503_and_failed_session(self):
        with patch("api.v1.catalog.tasks.process_venue_import_session_task.delay", side_effect=OSError("redis")):
            response = self.client.post(self.url, {"file": self._upload(["Мука", "Сахар", "Соль"])}, format="multipart")
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(CatalogImportSession.objects.get().processing_status, "failed")

    def test_task_is_routed_to_the_worker_queue(self):
        from django.conf import settings

        from api.v1.catalog.tasks import process_venue_import_session_task

        self.assertEqual(
            settings.CELERY_TASK_ROUTES[process_venue_import_session_task.name]["queue"], settings.AI_CLEANUP_QUEUE
        )


def _fake_master_resolve(*, owner, rows):
    return [
        SimpleNamespace(
            row_number=row["row_number"],
            raw_name=row["name"],
            unit_label=None,
            unit_id=None,
            unit_unrecognized=False,
            unit_source=None,
            net_value=None,
            note=None,
            action="create_new",
            candidates=[],
            candidate_master_product_id=None,
            candidate_canonical_name=None,
            score=None,
            needs_attention=False,
        )
        for row in rows
    ]


class ProcessMasterImportFromStoredFileTests(TemporaryMediaRootMixin, TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create(
            email="import-stored-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER
        )
        self.parsed_meta = {
            "sheet_name": "Sheet1",
            "header_row_number": 1,
            "detected_columns": {"name": "Название"},
            "rows_without_name": 0,
        }

    def _run(self, upload, rows):
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        with patch("api.v1.catalog.tasks.resolve_master_product_import_rows", side_effect=_fake_master_resolve):
            process_master_product_import_upload_task(
                upload_id=str(upload.id), owner_id=str(self.owner.id), rows=rows, parsed_meta=self.parsed_meta
            )
        upload.refresh_from_db()

    def test_task_reads_rows_from_the_stored_file_and_creates_a_review_session(self):
        from file_imports.models import ImportFileScope
        from file_imports.services import store_import_file

        stored = store_import_file(
            uploaded_file=_xlsx_upload([["Название"], ["AAAA"], ["BBBB"]], name="big.xlsx"),
            scope_type=ImportFileScope.OWNER,
            scope_id=self.owner.id,
        )
        upload = MasterProductImportUpload.objects.create(
            owner=self.owner, source_file_name="big.xlsx", rows_total=2, import_file=stored
        )

        self._run(upload, None)

        self.assertEqual(upload.status, "ready")
        session = CatalogImportSession.objects.get(owner=self.owner, domain="master")
        self.assertEqual(upload.result["session_id"], str(session.id))
        self.assertEqual(session.items.count(), 2)
        self.assertEqual(session.import_file_id, stored.id)
        self.assertEqual(session.meta["detected_columns"], {"name": "Название"})

    def test_task_with_rows_argument_also_creates_a_session(self):
        upload = MasterProductImportUpload.objects.create(owner=self.owner, rows_total=2)
        rows = [
            {"row_number": 1, "name": "Товар 1", "unit": None, "net": None, "note": None},
            {"row_number": 2, "name": "Товар 2", "unit": None, "net": None, "note": None},
        ]

        self._run(upload, rows)

        session = CatalogImportSession.objects.get(owner=self.owner, domain="master")
        self.assertEqual(upload.result["session_id"], str(session.id))
        self.assertEqual(session.items.count(), 2)
        self.assertIsNone(session.import_file)

    def test_expired_stored_file_marks_the_upload_failed_with_a_readable_message(self):
        from file_imports.models import ImportFile, ImportFileScope
        from file_imports.services import store_import_file

        stored = store_import_file(
            uploaded_file=_xlsx_upload([["Название"], ["AAAA"]], name="old.xlsx"),
            scope_type=ImportFileScope.OWNER,
            scope_id=self.owner.id,
        )
        ImportFile.objects.filter(pk=stored.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        upload = MasterProductImportUpload.objects.create(owner=self.owner, rows_total=1, import_file=stored)

        from api.v1.catalog.tasks import process_master_product_import_upload_task

        with self.assertRaises(Exception):  # noqa: B017 - задача помечает upload failed и пробрасывает ошибку
            process_master_product_import_upload_task(
                upload_id=str(upload.id), owner_id=str(self.owner.id), rows=None, parsed_meta=self.parsed_meta
            )
        upload.refresh_from_db()
        self.assertEqual(upload.status, "failed")
        self.assertEqual(upload.error_message, "Исходный файл недоступен: срок хранения истёк. Загрузите файл ещё раз.")


class CatalogImportFileStorageTests(TemporaryMediaRootMixin, APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="file-store@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe File Store")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.unit = Unit.objects.create(code="kg", name="Килограмм", kind="weight", status=EntityStatus.ACTIVE)
        self.master_url = "/api/v1/catalog/master-products/import/preview"
        self.venue_url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/file/preview"
        patcher_single = patch("api.v1.catalog.selectors.master_catalog.create_embedding", side_effect=_fake_embedding)
        patcher_batch = patch("api.v1.catalog.selectors.master_catalog.create_embeddings", side_effect=_fake_embeddings)
        patcher_single.start()
        patcher_batch.start()
        self.addCleanup(patcher_single.stop)
        self.addCleanup(patcher_batch.stop)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _master(self):
        upload = _xlsx_upload([["Название"], ["AAAA"]], name="m.xlsx")
        return self.client.post(self.master_url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")

    def _venue(self):
        upload = _xlsx_upload([["Название"], ["Мука"]], name="v.xlsx")
        return self.client.post(self.venue_url, {"file": upload}, format="multipart")

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True)
    def test_master_preview_stores_file_and_links_session(self):
        response = self._master()
        self.assertEqual(response.status_code, 200, response.content)
        session = CatalogImportSession.objects.get(pk=response.json()["session_id"])
        self.assertEqual(session.import_file.scope_type, "owner")
        self.assertEqual(session.import_file.scope_id, self.owner.id)
        self.assertTrue(default_storage.exists(session.import_file.storage_key))

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True)
    def test_venue_preview_stores_file_and_links_session(self):
        response = self._venue()
        self.assertEqual(response.status_code, 200, response.content)
        session = CatalogImportSession.objects.get(pk=response.json()["session_id"])
        self.assertEqual(session.import_file.scope_type, "venue")
        self.assertEqual(session.import_file.scope_id, self.venue.id)

    def test_flag_off_keeps_old_behaviour(self):
        response = self._master()
        session = CatalogImportSession.objects.get(pk=response.json()["session_id"])
        self.assertIsNone(session.import_file)
        self.assertEqual(ImportFile.objects.count(), 0)

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True)
    def test_storage_failure_gives_503_and_no_session(self):
        with patch("file_imports.services.default_storage.save", side_effect=OSError("s3 down")):
            master = self._master()
            venue = self._venue()
        for response in (master, venue):
            self.assertEqual(response.status_code, 503, response.content)
            self.assertEqual(
                response.json(), {"detail": "Не удалось сохранить исходный файл. Загрузите файл ещё раз чуть позже."}
            )
        self.assertEqual(CatalogImportSession.objects.count(), 0)

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True, MASTER_PRODUCT_IMPORT_ASYNC_THRESHOLD_ROWS=1)
    @patch("api.v1.catalog.services.master_catalog.process_master_product_import_upload_task.delay")
    def test_large_file_sends_no_rows_to_celery(self, mock_delay):
        upload = _xlsx_upload([["Название"], ["AAAA"], ["BBBB"]], name="big.xlsx")
        response = self.client.post(
            self.master_url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        kwargs = mock_delay.call_args.kwargs
        self.assertIsNone(kwargs["rows"])
        self.assertEqual(set(kwargs), {"upload_id", "owner_id", "rows", "parsed_meta"})

    @override_settings(MASTER_PRODUCT_IMPORT_ASYNC_THRESHOLD_ROWS=1)
    @patch("api.v1.catalog.services.master_catalog.process_master_product_import_upload_task.delay")
    def test_without_stored_file_rows_still_go_in_task_arguments(self, mock_delay):
        upload = _xlsx_upload([["Название"], ["AAAA"], ["BBBB"]], name="big.xlsx")
        self.client.post(self.master_url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart")
        self.assertEqual(len(mock_delay.call_args.kwargs["rows"]), 2)

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True, MASTER_PRODUCT_IMPORT_ASYNC_THRESHOLD_ROWS=1)
    @patch("api.v1.catalog.services.master_catalog.process_master_product_import_upload_task.delay")
    def test_large_master_file_is_stored_and_linked_to_the_upload(self, _delay):
        upload = _xlsx_upload([["Название"], ["AAAA"], ["BBBB"]], name="big.xlsx")
        response = self.client.post(
            self.master_url, {"owner_id": str(self.owner.id), "file": upload}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        record = MasterProductImportUpload.objects.get(pk=response.json()["upload_id"])
        self.assertEqual(record.import_file.scope_id, self.owner.id)
