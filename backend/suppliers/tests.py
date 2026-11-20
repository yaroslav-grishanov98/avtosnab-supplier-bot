from decimal import Decimal

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.utils import timezone

from catalog.models import GoogleSheetSyncLog, GoogleSheetSyncStatus, ProductMapping
from core.models import ExternalSystem
from suppliers.admin import SupplierPriceUploadAdmin, SupplierProductImportSessionAdmin
from suppliers.models import (
    PriceStatus,
    PriceType,
    SourceChannel,
    Supplier,
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    SupplierProduct,
    SupplierProductImportSession,
    SupplierProductPrice,
    UploadItemStatus,
    UploadStatus,
)


class SupplierPriceUploadAdminGetDeletedObjectsTests(TestCase):
    """Regression guard for the 2026-08-16 admin-delete hang: Django's
    delete-confirmation preview walks the whole FK graph via
    NestedObjects.collect() (previously also str()-formatted every object) —
    for a real 834-item upload this was slow enough in pure Python to hang
    the gunicorn worker past even its own 120s timeout, with no traceback
    (CPU-bound, not blocked on I/O — SIGABRT didn't interrupt it). Above
    SupplierPriceUploadAdmin.DELETE_PREVIEW_MAX_RELATED_OBJECTS, the whole
    NestedObjects walk is skipped in favor of two direct COUNT queries;
    below it, behavior is unchanged (Django's own accurate preview)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="admin-test", email="admin-test@example.com", password="x"
        )
        self.supplier = Supplier.objects.create(legal_name="Delete Preview LLC", display_name="Delete Preview")
        self.model_admin = SupplierPriceUploadAdmin(SupplierPriceUpload, admin.site)

    def _request(self):
        request = self.factory.get("/admin/suppliers/supplierpriceupload/")
        request.user = self.superuser
        return request

    def _make_upload_with_items(self, item_count):
        upload = SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={"kind": "text"},
        )
        SupplierPriceUploadItem.objects.bulk_create(
            [
                SupplierPriceUploadItem(
                    upload=upload,
                    row_number=i,
                    raw_name=f"Row {i}",
                    status=UploadItemStatus.MATCHED,
                )
                for i in range(item_count)
            ]
        )
        return upload

    def test_small_upload_uses_django_default_detailed_preview(self):
        upload = self._make_upload_with_items(3)

        to_delete, model_count, perms_needed, protected = self.model_admin.get_deleted_objects(
            [upload], self._request()
        )

        self.assertEqual(perms_needed, set())
        self.assertEqual(protected, [])
        flattened = str(to_delete)
        self.assertEqual(
            flattened.count("Supplier price upload item"),
            3,
            "expected Django's normal per-object preview (one entry per item) for a small delete",
        )

    def test_large_upload_skips_detailed_preview(self):
        threshold = SupplierPriceUploadAdmin.DELETE_PREVIEW_MAX_RELATED_OBJECTS
        upload = self._make_upload_with_items(threshold + 1)

        to_delete, model_count, perms_needed, protected = self.model_admin.get_deleted_objects(
            [upload], self._request()
        )

        flattened = str(to_delete)
        self.assertEqual(
            flattened.count("Supplier price upload item:"),
            0,
            "detailed per-object preview should be skipped above the threshold",
        )
        self.assertIn(str(threshold), flattened)
        self.assertTrue(
            any(count == threshold + 1 for count in model_count.values()),
            f"expected an accurate item count in model_count, got {model_count}",
        )


class SupplierPriceUploadAdminFastDeleteTests(TestCase):
    """The get_deleted_objects fix above only fixes the *preview*. The
    equivalent problem exists in the actual deletion (obj.delete()/
    queryset.delete() both run the same Python-level Collector.collect())
    — fast_delete_bypassing_orm_cascade sidesteps it with raw SQL instead.

    First attempt at this raw SQL assumed Postgres's own FK constraints
    would cascade natively — wrong, caught by
    test_cascades_correctly_via_raw_sql before shipping: Django's on_delete
    is enforced ENTIRELY at the ORM level, the real DB constraints are plain
    NO ACTION (confirmed via pg_constraint.confdeltype). The SQL now
    manually replicates the current on_delete graph for both models
    instead. test_covers_every_fk_pointing_at_these_models cross-checks
    that replicated list against Django's actual model graph, so a FK added
    later (schema drift) fails this test loudly instead of silently leaving
    orphaned/stale references in production."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="admin-test-2", email="admin-test-2@example.com", password="x"
        )
        self.supplier = Supplier.objects.create(legal_name="Fast Delete LLC", display_name="Fast Delete")
        self.model_admin = SupplierPriceUploadAdmin(SupplierPriceUpload, admin.site)

    def _request_with_messages(self):
        request = self.factory.post("/admin/suppliers/supplierpriceupload/")
        request.user = self.superuser
        request.session = {}
        messages = FallbackStorage(request)
        request._messages = messages
        return request

    def test_cascades_correctly_via_raw_sql(self):
        upload = SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={"kind": "text"},
        )
        item = SupplierPriceUploadItem.objects.create(
            upload=upload, row_number=1, raw_name="Row 1", status=UploadItemStatus.MATCHED
        )
        supplier_product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Row 1",
            normalized_name="row 1",
            last_upload=upload,
        )
        price = SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            upload_item=item,
            price_type=PriceType.PRICE_LIST,
            price=Decimal("10.00"),
            currency="RUB",
            status=PriceStatus.ACTIVE,
            valid_from=timezone.now(),
        )
        sync_log = GoogleSheetSyncLog.objects.create(
            spreadsheet_id="sheet-1",
            status=GoogleSheetSyncStatus.SUCCESS,
            supplier=self.supplier,
            upload=upload,
        )
        mapping = ProductMapping.objects.create(
            supplier_product=supplier_product,
            source_upload_item=item,
            external_system=ExternalSystem.GOOGLE_SHEETS,
            external_product_id="ext-1",
        )

        self.model_admin.fast_delete_bypassing_orm_cascade(
            self._request_with_messages(), SupplierPriceUpload.objects.filter(pk=upload.pk)
        )

        self.assertFalse(SupplierPriceUpload.objects.filter(pk=upload.pk).exists())
        self.assertFalse(SupplierPriceUploadItem.objects.filter(pk=item.pk).exists(), "item should CASCADE-delete")
        price.refresh_from_db()
        self.assertIsNone(price.upload_item_id, "price should survive with upload_item SET NULL, not be deleted")
        supplier_product.refresh_from_db()
        self.assertIsNone(supplier_product.last_upload_id, "product should survive with last_upload SET NULL")
        sync_log.refresh_from_db()
        self.assertIsNone(sync_log.upload_id, "sync log should survive with upload SET NULL")
        mapping.refresh_from_db()
        self.assertIsNone(mapping.source_upload_item_id, "mapping should survive with source_upload_item SET NULL")

    def test_empty_queryset_does_not_error(self):
        self.model_admin.fast_delete_bypassing_orm_cascade(
            self._request_with_messages(), SupplierPriceUpload.objects.none()
        )

    def test_covers_every_fk_pointing_at_these_models(self):
        """If a future migration adds a new FK to SupplierPriceUpload or
        SupplierPriceUploadItem, this fails loudly instead of
        fast_delete_bypassing_orm_cascade silently leaving it un-nulled/
        pointing at a deleted row in production."""
        handled = {
            (SupplierPriceUploadItem, "upload"),
            (SupplierProduct, "last_upload"),
            (GoogleSheetSyncLog, "upload"),
            (SupplierProductPrice, "upload_item"),
            (ProductMapping, "source_upload_item"),
        }
        actual = set()
        for model in (SupplierPriceUpload, SupplierPriceUploadItem):
            for rel in model._meta.related_objects:
                actual.add((rel.related_model, rel.field.name))

        self.assertEqual(
            actual,
            handled,
            "SupplierPriceUpload/SupplierPriceUploadItem's FK graph changed — update "
            "fast_delete_bypassing_orm_cascade (and this test) to match",
        )


class SupplierProductImportSessionAdminCleanupTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="admin-cleanup", email="admin-cleanup@example.com", password="x"
        )
        self.supplier = Supplier.objects.create(legal_name="Cleanup LLC", display_name="Cleanup")
        self.model_admin = SupplierProductImportSessionAdmin(SupplierProductImportSession, admin.site)

    def _request(self):
        request = self.factory.post("/admin/suppliers/supplierproductimportsession/")
        request.user = self.superuser
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def _session(self, sha, done, total, **kw):
        return SupplierProductImportSession.objects.create(
            supplier=self.supplier,
            source_file_name="f.xlsx",
            content_sha256=sha,
            rows_done=done,
            rows_total=total,
            **kw,
        )

    def test_dedup_keeps_complete_then_newest(self):
        full_old = self._session("a", 10, 10)
        self._session("a", 0, 10)  # newer but 0%
        other = self._session("b", 5, 5)
        self.model_admin.delete_duplicate_open_sessions(self._request(), SupplierProductImportSession.objects.all())
        self.assertEqual(
            set(SupplierProductImportSession.objects.values_list("pk", flat=True)), {full_old.pk, other.pk}
        )

    def test_delete_incomplete(self):
        full = self._session("a", 10, 10)
        self._session("b", 0, 10)
        self.model_admin.delete_incomplete_open_sessions(self._request(), SupplierProductImportSession.objects.all())
        self.assertEqual(list(SupplierProductImportSession.objects.values_list("pk", flat=True)), [full.pk])

    def test_action_choices_format_without_error(self):
        # get_action_choices форматирует описания через `%` — голый «%» в описании action даёт ValueError
        choices = self.model_admin.get_action_choices(self._request())
        self.assertTrue(any("100%" in label for _, label in choices))
