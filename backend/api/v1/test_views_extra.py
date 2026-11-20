"""
Extra tests for api/v1/views.py — covers branches missing from the main test suite.

Covered:
- supplier_products_page: has_prev=True (previous_price not None), leaf categories
- _cabinet_uploads_data: uploads with normalization in raw_payload
- supplier_product_update_proxy: exception from update service
- supplier_product_archive_proxy: exception from archive service
- supplier_upload_text_proxy: exception from create text upload service
- supplier_upload_apply_proxy: exception from apply AI result service
- _is_rate_limited: actual return True branch
- _notify_admin_new_claim: mail_admins exception handler
"""

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.test import TestCase as DjangoTestCase

from catalog.models import ProductCategory
from core.models import EntityStatus, Unit, UnitKind
from suppliers.models import (
    PriceStatus,
    PriceType,
    SourceChannel,
    Supplier,
    SupplierPriceUpload,
    SupplierProduct,
    SupplierProductPrice,
    UploadStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_supplier(display_name="Test Supplier", status=EntityStatus.ACTIVE, inn=None):
    return Supplier.objects.create(
        display_name=display_name,
        legal_name=f"{display_name} LLC",
        status=status,
        inn=inn,
    )


def _make_unit():
    return Unit.objects.create(code="pcs", name="штука", kind=UnitKind.PIECE)


def _make_product(supplier, unit, name="Болт М8", category=None, price=None):
    from catalog.models import Product

    product = Product.objects.create(canonical_name=name, normalized_name=name.lower(), base_unit=unit)
    sp = SupplierProduct.objects.create(
        supplier=supplier,
        product=product,
        unit=unit,
        supplier_name=name,
        normalized_name=name.lower(),
        status=EntityStatus.ACTIVE,
        category=category,
    )
    if price is not None:
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal(str(price)),
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )
    return sp


def _make_upload(supplier, status=UploadStatus.APPLIED):
    return SupplierPriceUpload.objects.create(
        supplier=supplier,
        source_channel=SourceChannel.TELEGRAM,
        status=status,
        raw_payload={"kind": "text", "text": "test", "metadata": {"text_sha256": "abc"}},
    )


# ---------------------------------------------------------------------------
# supplier_products_page: has_prev=True
# ---------------------------------------------------------------------------


class SupplierProductsPageHasPrevTests(DjangoTestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.url = f"/supplier/{self.supplier.pk}/"

    def test_has_prev_true_when_previous_price_exists(self):
        sp = _make_product(self.supplier, self.unit, price="100.00")

        upload = _make_upload(self.supplier)

        # valid_to must be > valid_from (constraint: supplier_product_prices_period)
        valid_from = upload.uploaded_at - timedelta(hours=1)
        valid_to = upload.uploaded_at
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("80.00"),
            price_type=PriceType.PRICE_LIST,
            status=PriceStatus.SUPERSEDED,
            valid_from=valid_from,
            valid_to=valid_to,
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('"changed": true', content)

    def test_page_renders_without_products(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# supplier_products_page: category tree (leaf categories)
# ---------------------------------------------------------------------------


class SupplierProductsPageCategoryTreeTests(DjangoTestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.url = f"/supplier/{self.supplier.pk}/"

    def test_category_tree_built_for_leaf_categories(self):
        group = ProductCategory.objects.create(name="Крепёж", slug="krepezh-tree")
        subgroup = ProductCategory.objects.create(name="Болты", slug="bolty-tree", parent=group)
        leaf = ProductCategory.objects.create(name="Болты М8", slug="bolty-m8-tree", parent=subgroup, status="active")
        _make_product(self.supplier, self.unit, category=leaf, price="50")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Крепёж", content)
        self.assertIn("Болты М8", content)


# ---------------------------------------------------------------------------
# _cabinet_uploads_data: normalization result in raw_payload
# ---------------------------------------------------------------------------


class CabinetUploadsDataTests(DjangoTestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.url = f"/cabinet/supplier/{self.supplier.pk}/"

    def test_uploads_with_normalization_result_render(self):
        SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={
                "kind": "text",
                "text": "test",
                "metadata": {"text_sha256": "abc"},
                "normalization": {
                    "result": {
                        "items_total": 5,
                        "products_created": 3,
                        "prices_created": 5,
                    }
                },
            },
        )

        response = self.client.get(self.url)
        # Cabinet renders the uploads with normalization data
        self.assertEqual(response.status_code, 200)

    def test_uploads_without_normalization_render(self):
        SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={"kind": "text", "text": "test", "metadata": {"text_sha256": "abc"}},
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# supplier_product_update_proxy: exception → 400
# ---------------------------------------------------------------------------


class SupplierProductUpdateProxyExceptionTests(DjangoTestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.sp = _make_product(self.supplier, self.unit, price="100.00")

    def _url(self):
        return f"/supplier/{self.supplier.pk}/api/products/{self.sp.pk}/"

    def test_update_exception_returns_400(self):
        with patch(
            "api.v1.views.update_supplier_product",
            side_effect=ValueError("Cannot update archived product"),
        ):
            response = self.client.patch(
                self._url(),
                data=json.dumps({"supplier_raw_name": "Болт М12"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Cannot update archived product", response.json()["detail"])


# ---------------------------------------------------------------------------
# supplier_product_archive_proxy: exception → 400
# ---------------------------------------------------------------------------


class SupplierProductArchiveProxyExceptionTests(DjangoTestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.sp = _make_product(self.supplier, self.unit, price="100.00")

    def _url(self):
        return f"/supplier/{self.supplier.pk}/api/products/{self.sp.pk}/archive/"

    def test_archive_exception_returns_400(self):
        with patch(
            "api.v1.views.archive_supplier_product",
            side_effect=RuntimeError("Product already archived"),
        ):
            response = self.client.post(self._url())

        self.assertEqual(response.status_code, 400)
        self.assertIn("Product already archived", response.json()["detail"])


# ---------------------------------------------------------------------------
# supplier_upload_text_proxy: exception → 400
# ---------------------------------------------------------------------------


class SupplierUploadTextProxyExceptionTests(DjangoTestCase):
    def setUp(self):
        self.supplier = _make_supplier()

    def _url(self):
        return f"/supplier/{self.supplier.pk}/api/uploads/text/"

    def test_create_exception_returns_400(self):
        with patch(
            "api.v1.views.create_supplier_price_upload_from_text",
            side_effect=Exception("Storage unavailable"),
        ):
            response = self.client.post(
                self._url(),
                data=json.dumps(
                    {
                        "text": "Болт М8 100 руб",
                        "source_channel": "web",
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Storage unavailable", response.json()["detail"])


# ---------------------------------------------------------------------------
# supplier_upload_apply_proxy: exception → 400
# ---------------------------------------------------------------------------


class SupplierUploadApplyProxyExceptionTests(DjangoTestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.upload = _make_upload(self.supplier, status=UploadStatus.PROCESSING)

    def _url(self):
        return f"/supplier/{self.supplier.pk}/api/uploads/{self.upload.pk}/apply/"

    def test_apply_exception_returns_400(self):
        with patch(
            "api.v1.views.apply_supplier_price_ai_result",
            side_effect=ValueError("Invalid AI payload"),
        ):
            response = self.client.post(
                self._url(),
                data=json.dumps(
                    {
                        "schema_version": "supplier_price_ai.v1",
                        "processing_version": "1",
                        "supplier_id": str(self.supplier.id),
                        "upload_id": str(self.upload.id),
                        "source_channel": "telegram",
                        "supplier_name": "Test",
                        "products": [],
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid AI payload", response.json()["detail"])


# ---------------------------------------------------------------------------
# _is_rate_limited: actual return True branch (line 396)
# ---------------------------------------------------------------------------


class IsRateLimitedFunctionTests(TestCase):
    def setUp(self):
        cache.clear()

    def _fake_request(self, ip="1.2.3.4"):
        factory = RequestFactory()
        request = factory.get("/")
        request.META["REMOTE_ADDR"] = ip
        return request

    def test_returns_false_on_first_call(self):
        from api.v1.views import _is_rate_limited

        request = self._fake_request()
        result = _is_rate_limited(request, scope="test_scope_A", limit=5, window=60)
        self.assertFalse(result)

    def test_returns_true_when_limit_exceeded(self):
        from api.v1.views import _is_rate_limited

        request = self._fake_request(ip="9.9.9.9")
        for _ in range(5):
            _is_rate_limited(request, scope="test_scope_B", limit=5, window=60)
        result = _is_rate_limited(request, scope="test_scope_B", limit=5, window=60)
        self.assertTrue(result)

    def test_different_ips_do_not_share_limit(self):
        from api.v1.views import _is_rate_limited

        req_a = self._fake_request(ip="10.0.0.1")
        req_b = self._fake_request(ip="10.0.0.2")
        for _ in range(5):
            _is_rate_limited(req_a, scope="scope_ip_C", limit=5, window=60)
        result = _is_rate_limited(req_b, scope="scope_ip_C", limit=5, window=60)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# _notify_admin_new_claim: mail_admins exception is swallowed (lines 419-420)
# ---------------------------------------------------------------------------


class NotifyAdminNewClaimExceptionTests(TestCase):
    def test_exception_from_mail_admins_is_logged_not_raised(self):
        from api.v1.views import _notify_admin_new_claim

        supplier = Supplier.objects.create(display_name="TestNotify", legal_name="TestNotify LLC")

        with patch("api.v1.views.mail_admins", side_effect=Exception("SMTP unreachable")):
            try:
                _notify_admin_new_claim(supplier, "Иван", "ivan@test.ru", "owner")
            except Exception:
                self.fail("_notify_admin_new_claim raised an exception")


# ---------------------------------------------------------------------------
# supplier_products_list_proxy: has_prev=True (line 648 in views.py)
# ---------------------------------------------------------------------------


class SupplierProductsListProxyHasPrevTests(DjangoTestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.url = f"/supplier/{self.supplier.pk}/api/products/"

    def test_changed_true_when_previous_price_exists(self):
        sp = _make_product(self.supplier, self.unit, price="120.00")

        upload = _make_upload(self.supplier)

        valid_from = upload.uploaded_at - timedelta(hours=1)
        valid_to = upload.uploaded_at
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("100.00"),
            price_type=PriceType.PRICE_LIST,
            status=PriceStatus.SUPERSEDED,
            valid_from=valid_from,
            valid_to=valid_to,
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["changed"])
        self.assertEqual(results[0]["prev_price"], "100.00")
