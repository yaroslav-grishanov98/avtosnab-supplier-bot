"""
Extended tests for api/v1/suppliers/services.py — covering uncovered branches.
Complements the ApplySupplierPriceAIResult/Archive/Update/Text tests in tests.py.
"""

import importlib
import threading
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError

from api.v1.suppliers.services import (
    AI_CLEANUP_SCOPE_ALL,
    AI_CLEANUP_SCOPE_AMBIGUOUS,
    AI_CLEANUP_SCOPE_NEEDS_REVIEW,
    AI_CLEANUP_STATUS_APPLIED,
    SupplierPriceUploadConflict,
    SupplierPriceUploadEnqueueError,
    SupplierPriceUploadLoadShedding,
    _ai_cleanup_max_in_flight,
    _ai_cleanup_payload,
    _ai_cleanup_progress_callback,
    _ai_cleanup_queue,
    _ai_item_needs_review,
    _apply_price,
    _availability_status_value,
    _currency_value,
    _ensure_upload_source_file,
    _generate_system_sku,
    _get_existing_supplier_product,
    _get_existing_upload_for_fingerprint,
    _looks_like_valid_brand_name,
    _mark_ai_cleanup_failed,
    _mark_upload_failed,
    _next_system_sku_batch,
    _package_value,
    _prepare_upload_for_ai_cleanup,
    _prepare_upload_for_normalization,
    _resolve_brand,
    _resolve_category_path,
    _rows_for_upload,
    _run_supplier_price_upload_ai_cleanup,
    _run_supplier_price_upload_normalization,
    _same_decimal,
    _same_optional_id,
    _selected_ai_cleanup_products,
    _supplier_product_matches_payload,
    _system_sku_stream,
    apply_supplier_price_ai_result,
    start_supplier_price_upload_ai_cleanup,
    start_supplier_price_upload_normalization,
    update_supplier_product,
)
from catalog.models import Brand, ProductCategory
from core.models import EntityStatus, Unit, UnitKind
from suppliers.models import (
    PriceStatus,
    PriceType,
    SourceChannel,
    Supplier,
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    SupplierProduct,
    SupplierProductPrice,
    UploadItemStatus,
    UploadStatus,
)

# ---------------------------------------------------------------------------
# DB helpers shared across test classes
# ---------------------------------------------------------------------------


def _mk_supplier(name="Test Supplier"):
    return Supplier.objects.create(legal_name=name, display_name=name)


def _mk_unit(code="pcs", kind=UnitKind.PIECE):
    return Unit.objects.create(code=code, name=code, kind=kind)


def _mk_text_upload(supplier, *, text="Болт М8 100 руб шт", status=UploadStatus.RECEIVED):
    sha = "abc123"
    return SupplierPriceUpload.objects.create(
        supplier=supplier,
        source_channel=SourceChannel.TELEGRAM,
        status=status,
        raw_payload={"kind": "text", "text": text, "metadata": {"text_sha256": sha}},
    )


def _mk_applied_upload(supplier, *, needs_review=False):
    status = UploadStatus.NEEDS_REVIEW if needs_review else UploadStatus.APPLIED
    upload = _mk_text_upload(supplier, status=status)
    # Add a minimal normalization result so cached path is possible
    upload.raw_payload = {
        **upload.raw_payload,
        "normalization": {
            "status": status,
            "result": {
                "upload_id": str(upload.id),
                "status": status,
                "items_total": 1,
                "items_created": 1,
                "products_created": 1,
                "products_updated": 0,
                "prices_created": 1,
                "items_matched": 0,
                "needs_review": 1 if needs_review else 0,
            },
        },
    }
    upload.save()
    return upload


def _mk_supplier_product(supplier, unit, *, name="Болт М8", norm="болт м8"):
    return SupplierProduct.objects.create(
        supplier=supplier,
        unit=unit,
        supplier_name=name,
        normalized_name=norm,
        status=EntityStatus.ACTIVE,
    )


def _minimal_product(unit, *, raw_name="Болт М8", norm="болт м8", price="100.00"):
    return {
        "row_number": 1,
        "raw_name": raw_name,
        "normalized_name": norm,
        "price": Decimal(price),
        "unit_code": unit.code,
        "confidence": Decimal("1.0000"),
    }


def _full_payload(supplier, upload, products):
    return {
        "schema_version": 1,
        "processing_version": 1,
        "supplier_id": supplier.id,
        "upload_id": upload.id,
        "source_channel": upload.source_channel,
        "supplier_name": supplier.display_name,
        "products": products,
    }


# ---------------------------------------------------------------------------
# Pure-logic helpers (SimpleTestCase, no DB)
# ---------------------------------------------------------------------------


class AvailabilityStatusValueTests(SimpleTestCase):
    def _av(self, value):
        return _availability_status_value({"availability_status": value})

    def test_archived_returns_archived(self):
        self.assertEqual(self._av("archived"), EntityStatus.ARCHIVED)

    def test_deleted_returns_archived(self):
        self.assertEqual(self._av("deleted"), EntityStatus.ARCHIVED)

    def test_removed_returns_archived(self):
        self.assertEqual(self._av("removed"), EntityStatus.ARCHIVED)

    def test_unavailable_returns_inactive(self):
        self.assertEqual(self._av("unavailable"), EntityStatus.INACTIVE)

    def test_out_of_stock_returns_inactive(self):
        self.assertEqual(self._av("out_of_stock"), EntityStatus.INACTIVE)

    def test_inactive_returns_inactive(self):
        self.assertEqual(self._av("inactive"), EntityStatus.INACTIVE)

    def test_available_returns_active(self):
        self.assertEqual(self._av("available"), EntityStatus.ACTIVE)

    def test_none_key_returns_active(self):
        self.assertEqual(_availability_status_value({}), EntityStatus.ACTIVE)

    def test_strips_whitespace_and_lowercases(self):
        self.assertEqual(self._av("  Archived  "), EntityStatus.ARCHIVED)


class AiCleanupMaxInFlightTests(SimpleTestCase):
    @override_settings(AI_CLEANUP_MAX_IN_FLIGHT=5)
    def test_returns_configured_value(self):
        self.assertEqual(_ai_cleanup_max_in_flight(), 5)

    @override_settings(AI_CLEANUP_MAX_IN_FLIGHT="not-an-int")
    def test_returns_default_2_on_invalid_int(self):
        self.assertEqual(_ai_cleanup_max_in_flight(), 2)

    @override_settings(AI_CLEANUP_MAX_IN_FLIGHT=None)
    def test_returns_default_2_on_none(self):
        self.assertEqual(_ai_cleanup_max_in_flight(), 2)

    @override_settings(AI_CLEANUP_MAX_IN_FLIGHT=0)
    def test_zero_is_valid(self):
        self.assertEqual(_ai_cleanup_max_in_flight(), 0)

    @override_settings(AI_CLEANUP_MAX_IN_FLIGHT=-3)
    def test_negative_clamped_to_zero(self):
        self.assertEqual(_ai_cleanup_max_in_flight(), 0)


class AiCleanupQueueTests(SimpleTestCase):
    def test_default_is_ai_normalization(self):
        self.assertEqual(_ai_cleanup_queue(), "ai_normalization")

    @override_settings(AI_CLEANUP_QUEUE="custom_queue")
    def test_returns_custom_queue(self):
        self.assertEqual(_ai_cleanup_queue(), "custom_queue")

    @override_settings(AI_CLEANUP_QUEUE=None)
    def test_none_falls_back_to_default(self):
        self.assertEqual(_ai_cleanup_queue(), "ai_normalization")


class PureHelperTests(SimpleTestCase):
    def test_package_value_prefers_package(self):
        self.assertEqual(_package_value({"package": "box"}), "box")

    def test_package_value_uses_package_info(self):
        self.assertEqual(_package_value({"package_info": "pallet"}), "pallet")

    def test_package_value_returns_none_when_missing(self):
        self.assertIsNone(_package_value({}))

    def test_currency_value_uppercases(self):
        self.assertEqual(_currency_value({"currency": "usd"}), "USD")

    def test_currency_value_truncates_to_3(self):
        self.assertEqual(_currency_value({"currency": "USDT"}), "USD")

    def test_currency_value_defaults_to_rub(self):
        self.assertEqual(_currency_value({}), "RUB")

    def test_same_decimal_both_none(self):
        self.assertTrue(_same_decimal(None, None))

    def test_same_decimal_one_none_is_false(self):
        self.assertFalse(_same_decimal(None, Decimal("1")))

    def test_same_decimal_equal(self):
        self.assertTrue(_same_decimal(Decimal("1.50"), Decimal("1.50")))

    def test_same_decimal_not_equal(self):
        self.assertFalse(_same_decimal(Decimal("1"), Decimal("2")))

    def test_same_optional_id_equal_ints(self):
        self.assertTrue(_same_optional_id(1, 1))

    def test_same_optional_id_uses_id_attr(self):
        obj = MagicMock()
        obj.id = 42
        self.assertTrue(_same_optional_id(obj, 42))

    def test_supplier_product_matches_payload_false_when_none(self):
        self.assertFalse(
            _supplier_product_matches_payload(
                supplier_product=None,
                product_data={},
                unit=None,
                net_value=None,
                net_unit=None,
            )
        )

    def test_ai_item_needs_review_low_confidence(self):
        product = {"confidence": Decimal("0.5"), "raw_name": "Болт", "normalized_name": "болт", "price": Decimal("10")}
        self.assertTrue(_ai_item_needs_review(product, MagicMock()))

    def test_ai_item_needs_review_missing_price(self):
        product = {"confidence": Decimal("0.9500"), "raw_name": "Болт", "normalized_name": "болт", "price": None}
        self.assertTrue(_ai_item_needs_review(product, MagicMock()))

    def test_ai_item_needs_review_false_when_complete(self):
        product = {
            "confidence": Decimal("0.9500"),
            "raw_name": "Болт",
            "normalized_name": "болт",
            "price": Decimal("10"),
        }
        self.assertFalse(_ai_item_needs_review(product, MagicMock()))

    def test_apply_price_returns_false_when_price_is_none(self):
        mock_product = MagicMock()
        self.assertFalse(_apply_price(supplier_product=mock_product, price=None))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


class EnsureUploadSourceFileTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()

    def test_returns_existing_uri_without_persisting(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.RECEIVED)
        upload.source_file_uri = "media/existing.csv"
        upload.save()
        result = _ensure_upload_source_file(
            upload=upload,
            source_file=MagicMock(),
            source_file_name="existing.csv",
            content_sha256="abc",
        )
        self.assertEqual(result, "media/existing.csv")

    def test_persists_file_when_no_uri(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.RECEIVED)
        source_file = BytesIO(b"col1,col2\nval1,val2")
        source_file.name = "test.csv"
        source_file.size = len(b"col1,col2\nval1,val2")
        with patch(
            "api.v1.suppliers.services.price_uploads.persist_supplier_price_source_file",
            return_value="media/stored.csv",
        ) as mock_persist:
            result = _ensure_upload_source_file(
                upload=upload,
                source_file=source_file,
                source_file_name="test.csv",
                content_sha256="deadbeef",
            )
        mock_persist.assert_called_once()
        self.assertEqual(result, "media/stored.csv")
        upload.refresh_from_db()
        self.assertEqual(upload.source_file_uri, "media/stored.csv")
        self.assertEqual(upload.raw_payload["metadata"]["source_file_uri"], "media/stored.csv")


class GetExistingUploadForFingerprintTests(TestCase):
    def test_returns_none_when_fingerprint_is_empty(self):
        supplier = _mk_supplier()
        result = _get_existing_upload_for_fingerprint(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            kind="text",
            fingerprint_field="text_sha256",
            fingerprint=None,
        )
        self.assertIsNone(result)

    def test_returns_none_when_fingerprint_is_empty_string(self):
        supplier = _mk_supplier()
        result = _get_existing_upload_for_fingerprint(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            kind="text",
            fingerprint_field="text_sha256",
            fingerprint="",
        )
        self.assertIsNone(result)


class GenerateSystemSkuTests(TestCase):
    def _number(self, sku):
        self.assertRegex(sku, r"^AS-\d{6,}$")
        return int(sku[3:])

    def test_codes_increase(self):
        first = self._number(_generate_system_sku())
        second = self._number(_generate_system_sku())
        self.assertGreater(second, first)

    def test_batch_is_sorted_and_unique(self):
        batch = _next_system_sku_batch(5)
        numbers = [self._number(sku) for sku in batch]
        self.assertEqual(numbers, sorted(set(numbers)))
        self.assertEqual(len(numbers), 5)

    def test_does_not_reuse_deleted_code(self):
        product = SupplierProduct.objects.create(
            supplier=_mk_supplier(),
            unit=_mk_unit(),
            supplier_name="X",
            normalized_name="x",
            system_sku=_generate_system_sku(),
        )
        deleted = product.system_sku
        product.delete()
        self.assertNotEqual(_generate_system_sku(), deleted)

    def test_orders_numerically_past_six_digits(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT setval('suppliers_system_sku_seq', 999998, true)")
        self.assertEqual(_next_system_sku_batch(3), ["AS-999999", "AS-1000000", "AS-1000001"])
        self.assertGreater(self._number(_generate_system_sku()), 1000001)

    def test_stream_fetches_lazily(self):
        stream = _system_sku_stream(chunk_size=2)
        taken = [next(stream) for _ in range(3)]
        numbers = [self._number(sku) for sku in taken]
        self.assertEqual(numbers, sorted(set(numbers)))
        # 3 codes taken -> 2 chunks of 2 fetched: the next fresh code skips only one unused number.
        self.assertEqual(self._number(_generate_system_sku()), numbers[-1] + 2)

    def test_migration_starts_after_largest_numeric_code(self):
        migration = importlib.import_module("suppliers.migrations.0064_system_sku_sequence")
        supplier = _mk_supplier()
        unit = _mk_unit()
        for index, sku in enumerate(["AS-000005", "AS-1000000", "AS-999999", "BAD-FORMAT"]):
            SupplierProduct.objects.create(
                supplier=supplier, unit=unit, supplier_name=f"X{index}", normalized_name=f"x{index}", system_sku=sku
            )
        with connection.cursor() as cursor:
            cursor.execute(migration.CREATE_SEQUENCE)
        self.assertEqual(_generate_system_sku(), "AS-1000001")


class ConcurrentSystemSkuTests(TransactionTestCase):
    def test_parallel_imports_get_distinct_codes(self):
        # Two suppliers committing imports at the same time used to read the same "max code" and collide.
        barrier = threading.Barrier(2)
        results = []

        def allocate():
            try:
                with transaction.atomic():
                    batch = _next_system_sku_batch(20)
                    barrier.wait(timeout=10)
                    results.append(batch)
            finally:
                connection.close()

        threads = [threading.Thread(target=allocate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 2)
        self.assertFalse(set(results[0]) & set(results[1]))


class ResolveCategoryPathTests(TestCase):
    def test_returns_none_for_empty_path(self):
        self.assertIsNone(_resolve_category_path([]))

    def test_skips_empty_name_in_path(self):
        # "" element triggers the `continue` on line 1140
        cat = _resolve_category_path(["", "Инструменты"])
        self.assertIsNotNone(cat)
        self.assertEqual(cat.name, "Инструменты")

    def test_creates_single_category(self):
        cat = _resolve_category_path(["Инструменты"])
        self.assertIsNotNone(cat)
        self.assertEqual(cat.name, "Инструменты")

    def test_creates_nested_categories(self):
        cat = _resolve_category_path(["Инструменты", "Ручной инструмент"])
        self.assertIsNotNone(cat.parent)
        self.assertEqual(cat.parent.name, "Инструменты")

    def test_returns_existing_category(self):
        _resolve_category_path(["Крепёж"])
        _resolve_category_path(["Крепёж"])
        self.assertEqual(ProductCategory.objects.filter(name="Крепёж").count(), 1)

    def test_parent_slug_collision_uses_prefixed_slug(self):
        # Create a top-level cat with slug "болты"
        ProductCategory.objects.create(slug="болты", name="Болты")
        # Now create a nested one: ["Крепёж", "Болты"] — the child "болты" conflicts
        # → unique_slug should be "крепёж__болты"
        parent = _resolve_category_path(["Крепёж"])
        child = _resolve_category_path(["Крепёж", "Болты"])
        self.assertEqual(child.parent.slug, parent.slug)
        self.assertIn(parent.slug, child.slug)


class LooksLikeValidBrandNameTests(SimpleTestCase):
    """2026-09-18, Pavel: фильтр "Бренды" на маркетплейсе показывал целиком
    названия товаров/упаковки и тестовые записи вместо реальных брендов —
    воспроизведено на живом дев-стенде (19 из 55 значений явно не бренды)."""

    def test_rejects_percent(self):
        self.assertFalse(_looks_like_valid_brand_name("BURЁNKA СLUB 35% 125гр 1/9 БЗМЖ"))

    def test_rejects_fraction(self):
        self.assertFalse(_looks_like_valid_brand_name("Король сыров брус 1кг 1/10"))

    def test_rejects_double_fraction(self):
        self.assertFalse(_looks_like_valid_brand_name("Mildar мини сегмент 1//12"))

    def test_rejects_quantity_with_unit(self):
        self.assertFalse(_looks_like_valid_brand_name("NHAO 19 л"))

    def test_rejects_tilde_approx(self):
        self.assertFalse(_looks_like_valid_brand_name("La Paulina 1/2 ~6кг"))

    def test_rejects_test_word_cyrillic(self):
        self.assertFalse(_looks_like_valid_brand_name("тест1"))

    def test_rejects_test_word_latin_anywhere(self):
        self.assertFalse(_looks_like_valid_brand_name("ТестовыЙ БРЕНД"))

    def test_accepts_normal_brand(self):
        self.assertTrue(_looks_like_valid_brand_name("Aristokrat Old"))

    def test_accepts_cyrillic_brand(self):
        self.assertTrue(_looks_like_valid_brand_name("Молочный Мир"))


class ResolveBrandGuardTests(TestCase):
    """_resolve_brand — единственная точка создания Brand (и из AI-разбора
    прайса, и из ручного ввода в кабинете поставщика) — не должна создавать
    мусорные записи для явно невалидных "брендов"."""

    def test_returns_none_for_garbage_without_creating_brand(self):
        result = _resolve_brand("Радость Вкуса 2шт*~6кг")
        self.assertIsNone(result)
        self.assertFalse(Brand.objects.filter(normalized_name__icontains="радость вкуса").exists())

    def test_returns_none_for_test_placeholder(self):
        result = _resolve_brand("test12345")
        self.assertIsNone(result)
        self.assertFalse(Brand.objects.filter(normalized_name="test12345").exists())

    def test_still_creates_valid_brand(self):
        result = _resolve_brand("Bosch")
        self.assertIsNotNone(result)
        self.assertEqual(result.canonical_name, "Bosch")


class GetExistingSupplierProductTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()
        self.unit = _mk_unit()
        self.upload = _mk_text_upload(self.supplier, status=UploadStatus.PROCESSING)

    def test_returns_none_when_no_normalized_name(self):
        result = _get_existing_supplier_product(
            upload=self.upload,
            product_data={"raw_name": ""},
        )
        self.assertIsNone(result)

    def test_finds_by_supplier_sku(self):
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="X",
            normalized_name="x",
            supplier_sku="SKU-42",
        )
        result = _get_existing_supplier_product(
            upload=self.upload,
            product_data={"supplier_sku": "SKU-42"},
        )
        self.assertEqual(result.pk, sp.pk)

    def test_returns_none_when_candidate_raw_name_differs(self):
        SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Гайка М8",
            normalized_name="гайка м8",
        )
        # Same normalized_name but different raw_name
        result = _get_existing_supplier_product(
            upload=self.upload,
            product_data={
                "raw_name": "Болт М8",  # differs from saved supplier_name
                "normalized_name": "гайка м8",
            },
        )
        self.assertIsNone(result)

    def test_returns_candidate_when_no_raw_name(self):
        # When raw_name is absent, any candidate matching normalized_name is returned
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
        )
        result = _get_existing_supplier_product(
            upload=self.upload,
            product_data={"normalized_name": "болт м8"},
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, sp.pk)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


class MarkUploadFailedTests(TestCase):
    def test_sets_status_to_failed(self):
        supplier = _mk_supplier()
        upload = _mk_text_upload(supplier, status=UploadStatus.PROCESSING)
        _mark_upload_failed(upload, "test error")
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.FAILED)
        self.assertEqual(upload.error_message, "test error")

    def test_stores_error_in_normalization_payload(self):
        supplier = _mk_supplier()
        upload = _mk_text_upload(supplier, status=UploadStatus.PROCESSING)
        _mark_upload_failed(upload, "parse failed")
        upload.refresh_from_db()
        self.assertEqual(upload.raw_payload["normalization"]["error"], "parse failed")


class RowsForUploadTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()

    def test_text_kind_returns_rows_from_text(self):
        upload = _mk_text_upload(self.supplier, text="Болт М8 100 руб шт")
        rows = _rows_for_upload(upload)
        self.assertGreater(len(rows), 0)

    def test_empty_text_returns_empty(self):
        upload = _mk_text_upload(self.supplier, text="")
        rows = _rows_for_upload(upload)
        self.assertEqual(rows, [])

    def test_unknown_kind_returns_empty(self):
        upload = SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.RECEIVED,
            raw_payload={"kind": "unknown"},
        )
        self.assertEqual(_rows_for_upload(upload), [])

    def test_file_kind_no_uri_returns_empty(self):
        upload = SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.RECEIVED,
            raw_payload={"kind": "file", "extracted_rows": [], "metadata": {}},
        )
        self.assertEqual(_rows_for_upload(upload), [])


class PrepareUploadForNormalizationTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()

    def test_raises_validation_error_for_archived_upload(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.ARCHIVED)
        with self.assertRaises(ValidationError) as ctx:
            _prepare_upload_for_normalization(upload_id=upload.id)
        self.assertIn("status", ctx.exception.detail)

    def test_raises_validation_error_for_failed_without_force(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.FAILED)
        with self.assertRaises(ValidationError) as ctx:
            _prepare_upload_for_normalization(upload_id=upload.id, force=False)
        self.assertIn("status", ctx.exception.detail)

    def test_failed_upload_with_force_transitions_to_processing(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.FAILED)
        result_upload, cached = _prepare_upload_for_normalization(upload_id=upload.id, force=True)
        self.assertIsNone(cached)
        self.assertEqual(result_upload.status, UploadStatus.PROCESSING)

    def test_raises_conflict_when_already_processing(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.PROCESSING)
        with self.assertRaises(SupplierPriceUploadConflict):
            _prepare_upload_for_normalization(upload_id=upload.id)

    def test_applied_upload_returns_cached_response(self):
        upload = _mk_applied_upload(self.supplier)
        _, cached = _prepare_upload_for_normalization(upload_id=upload.id)
        self.assertIsNotNone(cached)
        self.assertTrue(cached.get("cached"))

    def test_applied_without_cached_result_raises_when_not_forced(self):
        # APPLIED status but no normalization.result → cannot serve cache → must use force
        upload = SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={"kind": "text", "text": "test", "metadata": {"text_sha256": "abc"}},
        )
        with self.assertRaises(ValidationError) as ctx:
            _prepare_upload_for_normalization(upload_id=upload.id, force=False)
        self.assertIn("status", ctx.exception.detail)

    def test_applied_with_force_but_no_cache_restarts_normalization(self):
        # force=True must actually restart normalization for an already-applied
        # upload, even without a cached result — not raise "cannot be normalized".
        upload = SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={"kind": "text", "text": "test", "metadata": {"text_sha256": "abc"}},
        )
        locked_upload, cached_response = _prepare_upload_for_normalization(upload_id=upload.id, force=True)
        self.assertIsNone(cached_response)
        self.assertEqual(locked_upload.status, UploadStatus.PROCESSING)


class RunSupplierPriceUploadNormalizationTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()
        self.unit = _mk_unit()

    def test_no_rows_marks_upload_failed_and_returns_failed_status(self):
        upload = _mk_text_upload(self.supplier, text="", status=UploadStatus.PROCESSING)
        result = _run_supplier_price_upload_normalization(upload=upload)
        self.assertEqual(result["status"], UploadStatus.FAILED)
        self.assertEqual(result["items_total"], 0)
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.FAILED)

    def test_general_exception_marks_upload_failed_and_reraises(self):
        upload = _mk_text_upload(self.supplier, text="Болт М8 100 руб шт", status=UploadStatus.PROCESSING)
        with patch(
            "api.v1.suppliers.services.price_normalization.build_normalized_payload",
            side_effect=RuntimeError("AI down"),
        ):
            with self.assertRaises(RuntimeError):
                _run_supplier_price_upload_normalization(upload=upload)
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.FAILED)

    def test_value_error_raises_validation_error(self):
        upload = _mk_text_upload(self.supplier, text="Болт М8 100 руб шт", status=UploadStatus.PROCESSING)
        with patch(
            "api.v1.suppliers.services.price_normalization.build_normalized_payload",
            side_effect=ValueError("bad schema"),
        ):
            with self.assertRaises(ValidationError):
                _run_supplier_price_upload_normalization(upload=upload)


# ---------------------------------------------------------------------------
# start_supplier_price_upload_normalization
# ---------------------------------------------------------------------------


class StartNormalizationTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()

    def test_returns_cached_response_for_applied_upload(self):
        upload = _mk_applied_upload(self.supplier)
        result = start_supplier_price_upload_normalization(upload_id=upload.id)
        self.assertFalse(result.get("accepted"))
        self.assertTrue(result.get("run_async"))

    def test_enqueue_failure_raises_enqueue_error(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.RECEIVED)
        with patch("api.v1.suppliers.tasks.normalize_supplier_price_upload_task") as mock_task:
            mock_task.delay.side_effect = Exception("broker down")
            with self.assertRaises(SupplierPriceUploadEnqueueError):
                start_supplier_price_upload_normalization(upload_id=upload.id)
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.FAILED)


# ---------------------------------------------------------------------------
# apply_supplier_price_ai_result — extra branches
# ---------------------------------------------------------------------------


class ApplyAIResultExtraTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()
        self.unit = _mk_unit()
        self.upload = _mk_text_upload(self.supplier, status=UploadStatus.PROCESSING)

    def _apply(self, products, *, extra_payload=None):
        payload = _full_payload(self.supplier, self.upload, products)
        if extra_payload:
            payload.update(extra_payload)
        return apply_supplier_price_ai_result(upload_id=self.upload.id, data=payload)

    def test_source_channel_mismatch_raises_validation_error(self):
        payload = _full_payload(self.supplier, self.upload, [])
        payload["source_channel"] = SourceChannel.WEB
        with self.assertRaises(ValidationError) as ctx:
            apply_supplier_price_ai_result(upload_id=self.upload.id, data=payload)
        self.assertIn("source_channel", ctx.exception.detail)

    def test_matched_product_brand_and_category_updated(self):
        # Create existing product with no brand/category
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            status=EntityStatus.ACTIVE,
        )
        # Create matching active price
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("100.00"),
            currency="RUB",
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )
        product = _minimal_product(self.unit)
        product["brand"] = "Метиз"
        product["category_path"] = ["Крепёж"]
        self._apply([product])
        sp.refresh_from_db()
        self.assertIsNotNone(sp.brand)
        self.assertIsNotNone(sp.category)

    def test_existing_product_gets_system_sku_if_missing(self):
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Гайка М8",
            normalized_name="гайка м8",
            system_sku=None,
            status=EntityStatus.ACTIVE,
        )
        product = _minimal_product(self.unit, raw_name="Гайка М8", norm="гайка м8")
        self._apply([product])
        sp.refresh_from_db()
        self.assertIsNotNone(sp.system_sku)

    def test_existing_product_image_url_updated(self):
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            status=EntityStatus.ACTIVE,
        )
        product = _minimal_product(self.unit)
        product["image_url"] = "https://cdn.example.com/bolt.jpg"
        self._apply([product])
        sp.refresh_from_db()
        self.assertEqual(sp.image_url, "https://cdn.example.com/bolt.jpg")

    def test_existing_product_category_set_during_update(self):
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            status=EntityStatus.ACTIVE,
        )
        product = _minimal_product(self.unit)
        product["category_path"] = ["Крепёж"]
        self._apply([product])
        sp.refresh_from_db()
        self.assertIsNotNone(sp.category)


# ---------------------------------------------------------------------------
# update_supplier_product — extra branches
# ---------------------------------------------------------------------------


class UpdateSupplierProductExtraTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()
        self.unit = _mk_unit()
        self.product = _mk_supplier_product(self.supplier, self.unit)

    def test_image_url_empty_string_sets_none(self):
        self.product.image_url = "https://old.example.com/img.jpg"
        self.product.save()
        update_supplier_product(
            supplier_product_id=self.product.id,
            data={"image_url": ""},
        )
        self.product.refresh_from_db()
        self.assertIsNone(self.product.image_url)

    def test_image_url_none_sets_none(self):
        self.product.image_url = "https://old.example.com/img.jpg"
        self.product.save()
        update_supplier_product(
            supplier_product_id=self.product.id,
            data={"image_url": None},
        )
        self.product.refresh_from_db()
        self.assertIsNone(self.product.image_url)


# ---------------------------------------------------------------------------
# AI cleanup service functions
# ---------------------------------------------------------------------------


class StartAICleanupTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()

    def test_invalid_scope_raises_validation_error(self):
        upload = _mk_applied_upload(self.supplier)
        with self.assertRaises(ValidationError) as ctx:
            start_supplier_price_upload_ai_cleanup(upload_id=upload.id, scope="invalid_scope")
        self.assertIn("scope", ctx.exception.detail)

    def test_processing_upload_raises_conflict(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.PROCESSING)
        with self.assertRaises(SupplierPriceUploadConflict):
            start_supplier_price_upload_ai_cleanup(upload_id=upload.id)

    def test_upload_not_applied_raises_validation_error(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.RECEIVED)
        with self.assertRaises(ValidationError):
            start_supplier_price_upload_ai_cleanup(upload_id=upload.id)

    @override_settings(AI_CLEANUP_MAX_IN_FLIGHT=1)
    def test_overloaded_raises_load_shedding(self):
        upload = _mk_applied_upload(self.supplier)
        # Put another upload in active AI cleanup state so in_flight ≥ max
        other_upload = _mk_applied_upload(_mk_supplier("Other"))
        other_upload.raw_payload["normalization"]["ai_cleanup"] = {"status": "queued"}
        other_upload.save()
        with self.assertRaises(SupplierPriceUploadLoadShedding):
            start_supplier_price_upload_ai_cleanup(upload_id=upload.id)

    def test_already_completed_cleanup_returns_cached(self):
        upload = _mk_applied_upload(self.supplier)
        upload.raw_payload["normalization"]["ai_cleanup"] = {
            "status": AI_CLEANUP_STATUS_APPLIED,
            "finished_at": "2026-01-01T00:00:00",
        }
        upload.save()
        result = start_supplier_price_upload_ai_cleanup(upload_id=upload.id, force=False)
        self.assertFalse(result["accepted"])
        self.assertTrue(result.get("cached"))

    def test_enqueue_failure_raises_enqueue_error(self):
        upload = _mk_applied_upload(self.supplier)
        with patch("api.v1.suppliers.tasks.ai_cleanup_supplier_price_upload_task") as mock_task:
            mock_task.apply_async.side_effect = Exception("redis down")
            with self.assertRaises(SupplierPriceUploadEnqueueError):
                start_supplier_price_upload_ai_cleanup(upload_id=upload.id)

    def test_successful_enqueue_returns_accepted(self):
        upload = _mk_applied_upload(self.supplier)
        mock_result = MagicMock()
        mock_result.id = "celery-task-id-123"
        with patch("api.v1.suppliers.tasks.ai_cleanup_supplier_price_upload_task") as mock_task:
            mock_task.apply_async.return_value = mock_result
            result = start_supplier_price_upload_ai_cleanup(upload_id=upload.id)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["celery_task_id"], "celery-task-id-123")


class MarkAICleanupFailedTests(TestCase):
    def test_sets_cleanup_status_to_failed(self):
        supplier = _mk_supplier()
        upload = _mk_applied_upload(supplier)
        _mark_ai_cleanup_failed(upload, "network timeout")
        upload.refresh_from_db()
        cleanup = _ai_cleanup_payload(upload)
        self.assertEqual(cleanup["status"], "failed")
        self.assertEqual(cleanup["error"], "network timeout")


class AICleanupProgressCallbackTests(TestCase):
    def test_exception_in_callback_is_swallowed(self):
        supplier = _mk_supplier()
        upload = _mk_applied_upload(supplier)
        callback = _ai_cleanup_progress_callback(upload.id)
        # Simulate exception by patching select_for_update to raise
        with patch.object(SupplierPriceUpload.objects, "select_for_update", side_effect=Exception("db gone")):
            # Should not raise
            callback(
                chunk_index=1,
                chunks_total=2,
                stats={
                    "ai_chunks_succeeded": 1,
                    "ai_chunks_failed": 0,
                    "ai_retries": 0,
                    "ai_error_codes": [],
                },
                resolved=1,
            )


class SelectedAICleanupProductsTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()
        self.unit = _mk_unit()
        self.upload = _mk_text_upload(self.supplier, text="Болт М8 100 руб шт", status=UploadStatus.APPLIED)

    def _rows(self):
        return _rows_for_upload(self.upload)

    def test_scope_all_includes_all_rows(self):
        rows = self._rows()
        parsed, selected = _selected_ai_cleanup_products(upload=self.upload, rows=rows, scope=AI_CLEANUP_SCOPE_ALL)
        self.assertEqual(len(selected), len(parsed))

    def test_scope_ambiguous_includes_low_confidence_rows(self):
        rows = self._rows()
        # Make all products look ambiguous (low confidence)
        with patch("api.v1.suppliers.services.price_normalization._needs_ai_fallback", return_value=True):
            _, selected = _selected_ai_cleanup_products(upload=self.upload, rows=rows, scope=AI_CLEANUP_SCOPE_AMBIGUOUS)
        self.assertGreater(len(selected), 0)

    def test_scope_ambiguous_excludes_confident_rows(self):
        rows = self._rows()
        with patch("api.v1.suppliers.services.price_normalization._needs_ai_fallback", return_value=False):
            _, selected = _selected_ai_cleanup_products(upload=self.upload, rows=rows, scope=AI_CLEANUP_SCOPE_AMBIGUOUS)
        self.assertEqual(len(selected), 0)

    def test_scope_needs_review_includes_needs_review_items(self):
        rows = self._rows()
        if not rows:
            self.skipTest("no rows extracted from text")
        row_number = rows[0].row_number
        SupplierPriceUploadItem.objects.create(
            upload=self.upload,
            row_number=row_number,
            raw_name="Болт М8",
            raw_price="100",
            status=UploadItemStatus.NEEDS_REVIEW,
        )
        _, selected = _selected_ai_cleanup_products(upload=self.upload, rows=rows, scope=AI_CLEANUP_SCOPE_NEEDS_REVIEW)
        self.assertEqual(len(selected), 1)

    def test_scope_needs_review_excludes_non_review_items(self):
        rows = self._rows()
        _, selected = _selected_ai_cleanup_products(upload=self.upload, rows=rows, scope=AI_CLEANUP_SCOPE_NEEDS_REVIEW)
        # No upload items in NEEDS_REVIEW → nothing selected
        self.assertEqual(len(selected), 0)

    def test_resolves_non_fixed_five_unit_code_from_live_unit_table(self):
        # Regression for the final-review Critical finding: parse_supplier_price_row
        # must resolve a unit code like "уп" (package) that only exists in the live
        # `Unit` table (seeded by core/migrations/0012_seed_extended_units.py), not in
        # the fixed 5-value UNIT_ALIASES_MAP (шт/кг/г/л/мл). _selected_ai_cleanup_products
        # must compute unit_aliases from the DB and thread it through
        # parse_supplier_price_row for this to work end to end.
        upload = SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={
                "kind": "file",
                "extracted_rows": [
                    {
                        "row_number": 1,
                        "cells": {"name": "Мешок сахара 50 кг", "price": "500", "unit": "уп"},
                    }
                ],
            },
        )
        rows = _rows_for_upload(upload)
        parsed, _selected = _selected_ai_cleanup_products(upload=upload, rows=rows, scope=AI_CLEANUP_SCOPE_ALL)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["unit_code"], "уп")
        self.assertNotIn("unit_unknown", parsed[0]["warnings"])


class RunAICleanupTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()
        self.unit = _mk_unit()

    def test_no_rows_marks_cleanup_failed_and_returns_error(self):
        upload = _mk_text_upload(self.supplier, text="", status=UploadStatus.APPLIED)
        # Set up queued cleanup payload
        upload.raw_payload["normalization"] = {"ai_cleanup": {"status": "queued"}}
        upload.save()
        result = _run_supplier_price_upload_ai_cleanup(upload=upload, scope=AI_CLEANUP_SCOPE_ALL)
        self.assertEqual(result["status"], "failed")

    def test_no_products_for_scope_returns_noop_result(self):
        upload = _mk_text_upload(self.supplier, text="Болт М8 100 руб шт", status=UploadStatus.APPLIED)
        upload.raw_payload["normalization"] = {"ai_cleanup": {"status": "queued"}}
        upload.save()
        # scope=needs_review with no NEEDS_REVIEW items → no products selected
        result = _run_supplier_price_upload_ai_cleanup(upload=upload, scope=AI_CLEANUP_SCOPE_NEEDS_REVIEW)
        self.assertEqual(result["items_total"], 0)

    def test_exception_from_normalize_products_marks_failed_and_reraises(self):
        upload = _mk_text_upload(self.supplier, text="Болт М8 100 руб шт", status=UploadStatus.APPLIED)
        upload.raw_payload["normalization"] = {"ai_cleanup": {"status": "queued"}}
        upload.save()
        with patch(
            "api.v1.suppliers.services.price_normalization.normalize_products_with_ai",
            side_effect=RuntimeError("AI quota"),
        ):
            with self.assertRaises(RuntimeError):
                _run_supplier_price_upload_ai_cleanup(upload=upload, scope=AI_CLEANUP_SCOPE_ALL)
        upload.refresh_from_db()
        cleanup = _ai_cleanup_payload(upload)
        self.assertEqual(cleanup["status"], "failed")

    def test_partial_ai_errors_add_warning_to_unresolved_rows(self):
        # normalize_products_with_ai returns with ai_errors and some rows unresolved
        # → lines 1003-1007: warning injected into product dict for unresolved rows
        supplier = _mk_supplier("AI Error Supplier")
        _mk_unit("pieces")
        upload = _mk_text_upload(supplier, text="Болт М8 100 руб шт", status=UploadStatus.APPLIED)
        upload.raw_payload["normalization"] = {"ai_cleanup": {"status": "queued"}}
        upload.save()

        def fake_normalize_with_ai(*, upload, rows, products, **kwargs):
            # Return zero ai_products (none resolved), but with an error
            stats = {
                "ai_chunks_total": 1,
                "ai_chunks_failed": 1,
                "ai_chunks_succeeded": 0,
                "ai_retries": 0,
                "ai_errors": ["API timeout"],
                "ai_error_codes": ["timeout"],
                "resolved_row_numbers": set(),
            }
            return [], stats

        with patch(
            "api.v1.suppliers.services.price_normalization.normalize_products_with_ai",
            side_effect=fake_normalize_with_ai,
        ):
            with patch(
                "api.v1.suppliers.services.price_normalization.apply_supplier_price_ai_result",
                return_value={
                    "upload_id": upload.id,
                    "status": UploadStatus.APPLIED,
                    "items_total": 0,
                    "items_created": 0,
                    "products_created": 0,
                    "products_updated": 0,
                    "prices_created": 0,
                    "items_matched": 0,
                    "needs_review": 0,
                },
            ):
                result = _run_supplier_price_upload_ai_cleanup(upload=upload, scope=AI_CLEANUP_SCOPE_ALL)
        # Result should come back even with partial errors
        self.assertIsNotNone(result)


class PrepareUploadForAICleanupTests(TestCase):
    def setUp(self):
        self.supplier = _mk_supplier()

    def test_processing_upload_raises_conflict(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.PROCESSING)
        with self.assertRaises(SupplierPriceUploadConflict):
            _prepare_upload_for_ai_cleanup(upload_id=upload.id, scope=AI_CLEANUP_SCOPE_ALL)

    def test_received_upload_raises_validation_error(self):
        upload = _mk_text_upload(self.supplier, status=UploadStatus.RECEIVED)
        with self.assertRaises(ValidationError):
            _prepare_upload_for_ai_cleanup(upload_id=upload.id, scope=AI_CLEANUP_SCOPE_ALL)

    def test_completed_cleanup_without_force_returns_cached(self):
        upload = _mk_applied_upload(self.supplier)
        upload.raw_payload["normalization"]["ai_cleanup"] = {
            "status": AI_CLEANUP_STATUS_APPLIED,
        }
        upload.save()
        _, result = _prepare_upload_for_ai_cleanup(upload_id=upload.id, scope=AI_CLEANUP_SCOPE_ALL, force=False)
        self.assertIsNotNone(result)
        self.assertFalse(result["accepted"])
        self.assertTrue(result.get("cached"))

    @override_settings(AI_CLEANUP_MAX_IN_FLIGHT=1)
    def test_overloaded_returns_rejected_response(self):
        upload = _mk_applied_upload(self.supplier)
        other_upload = _mk_applied_upload(_mk_supplier("Other"))
        other_upload.raw_payload["normalization"]["ai_cleanup"] = {"status": "queued"}
        other_upload.save()
        _, result = _prepare_upload_for_ai_cleanup(upload_id=upload.id, scope=AI_CLEANUP_SCOPE_ALL)
        self.assertIsNotNone(result)
        self.assertTrue(result.get("overloaded"))
