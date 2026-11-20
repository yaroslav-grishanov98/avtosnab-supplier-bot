"""
Tests for api/v1/suppliers/tasks.py.

Tasks are run synchronously via task.apply() — no broker needed.
Heavy service functions (_run_supplier_price_upload_normalization,
_run_supplier_price_upload_ai_cleanup) are mocked so tests don't need
AI credentials or file parsing infrastructure.
"""

from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from api.v1.suppliers.tasks import (
    ai_cleanup_supplier_price_upload_task,
    normalize_supplier_price_upload_task,
    sync_supplier_catalog_to_qdrant_task,
)
from suppliers.models import (
    SourceChannel,
    Supplier,
    SupplierPriceUpload,
    UploadStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_supplier():
    return Supplier.objects.create(
        legal_name="Task Test LLC",
        display_name="Task Test",
    )


def _make_upload(supplier, *, status=UploadStatus.PROCESSING):
    return SupplierPriceUpload.objects.create(
        supplier=supplier,
        source_channel=SourceChannel.TELEGRAM,
        status=status,
        raw_payload={
            "kind": "text",
            "text": "Болт М8 150 руб шт",
            "metadata": {"text_sha256": "abc"},
        },
    )


def _norm_result(upload, supplier):
    return {
        "upload_id": str(upload.id),
        "supplier_id": str(supplier.id),
        "supplier_name": supplier.display_name,
        "status": UploadStatus.APPLIED,
        "items_total": 1,
        "items_created": 1,
        "products_created": 1,
        "products_updated": 0,
        "prices_created": 1,
        "items_matched": 0,
        "needs_review": 0,
        "normalization": {},
    }


def _cleanup_result(upload):
    return {
        "upload_id": str(upload.id),
        "status": UploadStatus.APPLIED,
        "items_total": 1,
        "items_created": 0,
        "products_created": 0,
        "products_updated": 1,
        "prices_created": 0,
        "items_matched": 1,
        "needs_review": 0,
    }


# ---------------------------------------------------------------------------
# normalize_supplier_price_upload_task
# ---------------------------------------------------------------------------

# close_old_connections() called inside tasks closes the test DB connection.
# Patch it out at the module level for all task tests.
_patch_close = patch("api.v1.suppliers.tasks.close_old_connections")


class NormalizeSupplierPriceUploadTaskTests(TestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.upload = _make_upload(self.supplier)
        _patch_close.start()

    def tearDown(self):
        _patch_close.stop()

    def _apply(self, **kwargs):
        return normalize_supplier_price_upload_task.apply(
            kwargs={"upload_id": str(self.upload.id), **kwargs},
            throw=False,
        )

    def _patch_run(self, **kwargs):
        result = _norm_result(self.upload, self.supplier)
        result.update(kwargs)
        return patch(
            "api.v1.suppliers.services._run_supplier_price_upload_normalization",
            return_value=result,
        )

    # --- skip / missing ---

    def test_skips_upload_not_in_processing_status(self):
        self.upload.status = UploadStatus.RECEIVED
        self.upload.save()
        result = self._apply()
        self.assertTrue(result.result["skipped"])
        self.assertEqual(result.result["status"], UploadStatus.RECEIVED)

    def test_skip_returns_upload_id(self):
        self.upload.status = UploadStatus.APPLIED
        self.upload.save()
        result = self._apply()
        self.assertEqual(result.result["upload_id"], str(self.upload.id))

    def test_missing_upload_returns_missing_flag(self):
        result = normalize_supplier_price_upload_task.apply(
            kwargs={"upload_id": str(uuid4())},
            throw=False,
        )
        self.assertTrue(result.result["missing"])

    def test_missing_upload_does_not_raise(self):
        result = normalize_supplier_price_upload_task.apply(
            kwargs={"upload_id": str(uuid4())},
            throw=False,
        )
        self.assertFalse(result.failed())

    # --- normal execution ---

    def test_calls_run_normalization_for_processing_upload(self):
        with self._patch_run() as mock_run:
            self._apply()
        mock_run.assert_called_once()
        called_kwargs = mock_run.call_args.kwargs
        self.assertEqual(called_kwargs["upload"], self.upload)

    def test_passes_use_ai_to_normalization(self):
        with self._patch_run() as mock_run:
            self._apply(use_ai=False)
        self.assertFalse(mock_run.call_args.kwargs["use_ai"])

    def test_passes_ai_mode_to_normalization(self):
        with self._patch_run() as mock_run:
            self._apply(ai_mode="ambiguous")
        self.assertEqual(mock_run.call_args.kwargs["ai_mode"], "ambiguous")

    def test_passes_confirm_full_to_normalization(self):
        with self._patch_run() as mock_run:
            self._apply(confirm_full=True)
        self.assertTrue(mock_run.call_args.kwargs["confirm_full"])

    def test_result_not_marked_skipped_or_missing(self):
        with self._patch_run():
            result = self._apply()
        self.assertFalse(result.result.get("skipped"))
        self.assertFalse(result.result.get("missing"))

    def test_reraises_exception_from_normalization(self):
        with patch(
            "api.v1.suppliers.services._run_supplier_price_upload_normalization",
            side_effect=ValueError("normalization exploded"),
        ):
            result = self._apply()
        self.assertTrue(result.failed())

    # --- run_ai_cleanup ---

    def test_run_ai_cleanup_triggers_cleanup_after_applied(self):
        with self._patch_run():
            with patch(
                "api.v1.suppliers.services.start_supplier_price_upload_ai_cleanup",
                return_value={"accepted": True, "ai_cleanup": {"status": "queued"}},
            ) as mock_cleanup:
                result = self._apply(run_ai_cleanup=True)
        mock_cleanup.assert_called_once_with(
            upload_id=str(self.upload.id),
            scope="all",
        )
        self.assertTrue(result.result["ai_cleanup_accepted"])

    def test_run_ai_cleanup_uses_custom_scope(self):
        with self._patch_run():
            with patch(
                "api.v1.suppliers.services.start_supplier_price_upload_ai_cleanup",
                return_value={"accepted": True, "ai_cleanup": {}},
            ) as mock_cleanup:
                self._apply(run_ai_cleanup=True, ai_cleanup_scope="ambiguous")
        self.assertEqual(mock_cleanup.call_args.kwargs["scope"], "ambiguous")

    def test_run_ai_cleanup_false_does_not_call_cleanup(self):
        with self._patch_run():
            with patch(
                "api.v1.suppliers.services.start_supplier_price_upload_ai_cleanup",
            ) as mock_cleanup:
                self._apply(run_ai_cleanup=False)
        mock_cleanup.assert_not_called()

    def test_run_ai_cleanup_not_triggered_when_needs_review(self):
        # status=NEEDS_REVIEW → ai_cleanup should still be triggered
        with self._patch_run(status=UploadStatus.NEEDS_REVIEW):
            with patch(
                "api.v1.suppliers.services.start_supplier_price_upload_ai_cleanup",
                return_value={"accepted": True, "ai_cleanup": {}},
            ) as mock_cleanup:
                self._apply(run_ai_cleanup=True)
        mock_cleanup.assert_called_once()

    def test_ai_cleanup_enqueue_error_is_captured_not_raised(self):
        with self._patch_run():
            with patch(
                "api.v1.suppliers.services.start_supplier_price_upload_ai_cleanup",
                side_effect=Exception("queue overloaded"),
            ):
                result = self._apply(run_ai_cleanup=True)
        self.assertFalse(result.failed())
        self.assertFalse(result.result["ai_cleanup_accepted"])
        self.assertIn("queue overloaded", result.result["ai_cleanup_enqueue_error"])


# ---------------------------------------------------------------------------
# ai_cleanup_supplier_price_upload_task
# ---------------------------------------------------------------------------


class AICleanupSupplierPriceUploadTaskTests(TestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.upload = _make_upload(self.supplier, status=UploadStatus.APPLIED)
        _patch_close.start()

    def tearDown(self):
        _patch_close.stop()

    def _apply(self, **kwargs):
        return ai_cleanup_supplier_price_upload_task.apply(
            kwargs={"upload_id": str(self.upload.id), **kwargs},
            throw=False,
        )

    def _patch_cleanup(self):
        return patch(
            "api.v1.suppliers.services._run_supplier_price_upload_ai_cleanup",
            return_value=_cleanup_result(self.upload),
        )

    # --- missing ---

    def test_missing_upload_returns_missing_flag(self):
        result = ai_cleanup_supplier_price_upload_task.apply(
            kwargs={"upload_id": str(uuid4())},
            throw=False,
        )
        self.assertTrue(result.result["missing"])

    def test_missing_upload_does_not_raise(self):
        result = ai_cleanup_supplier_price_upload_task.apply(
            kwargs={"upload_id": str(uuid4())},
            throw=False,
        )
        self.assertFalse(result.failed())

    # --- normal execution ---

    def test_calls_run_ai_cleanup(self):
        with self._patch_cleanup() as mock_run:
            result = self._apply()
        mock_run.assert_called_once()
        self.assertFalse(result.failed())

    def test_passes_upload_object_to_cleanup(self):
        with self._patch_cleanup() as mock_run:
            self._apply()
        self.assertEqual(mock_run.call_args.kwargs["upload"], self.upload)

    def test_passes_scope_to_cleanup(self):
        with self._patch_cleanup() as mock_run:
            self._apply(scope="ambiguous")
        self.assertEqual(mock_run.call_args.kwargs["scope"], "ambiguous")

    def test_passes_force_to_cleanup(self):
        with self._patch_cleanup() as mock_run:
            self._apply(force=True)
        self.assertTrue(mock_run.call_args.kwargs["force"])

    def test_default_scope_is_all(self):
        with self._patch_cleanup() as mock_run:
            self._apply()
        self.assertEqual(mock_run.call_args.kwargs["scope"], "all")

    def test_default_force_is_false(self):
        with self._patch_cleanup() as mock_run:
            self._apply()
        self.assertFalse(mock_run.call_args.kwargs["force"])

    def test_reraises_exception_from_cleanup(self):
        with patch(
            "api.v1.suppliers.services._run_supplier_price_upload_ai_cleanup",
            side_effect=RuntimeError("cleanup failed"),
        ):
            result = self._apply()
        self.assertTrue(result.failed())

    def test_result_serialized_as_json_safe(self):
        from decimal import Decimal

        cleanup_result = {**_cleanup_result(self.upload), "some_decimal": Decimal("99.99")}
        with patch(
            "api.v1.suppliers.services._run_supplier_price_upload_ai_cleanup",
            return_value=cleanup_result,
        ):
            result = self._apply()
        self.assertEqual(result.result["some_decimal"], "99.99")


class SyncSupplierCatalogToQdrantTaskTests(TestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        _patch_close.start()

    def tearDown(self):
        _patch_close.stop()

    @patch("api.v1.catalog.embeddings.sync_supplier_products_to_qdrant", return_value=(0, 0))
    def test_syncs_only_the_requested_suppliers_active_products(self, sync_mock):
        result = sync_supplier_catalog_to_qdrant_task.apply(
            kwargs={"supplier_id": str(self.supplier.id), "upload_id": str(uuid4())},
            throw=False,
        )

        self.assertFalse(result.failed())
        self.assertEqual(result.result["processed"], 0)
        self.assertEqual(result.result["skipped"], 0)
        sync_mock.assert_called_once_with([], batch_size=128)
