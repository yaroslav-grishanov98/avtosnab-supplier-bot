import hashlib
import hmac
import json
import shutil
import tempfile
import threading
import time
import urllib.error
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections, transaction
from django.db.models.deletion import RestrictedError
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from openpyxl import Workbook
from rest_framework.test import APITestCase

from api.llm import LLMConnectorError
from api.v1.catalog.selectors import find_candidate_catalog_products
from api.v1.suppliers.normalization import (
    AI_ERROR_CREDENTIALS_NOT_CONFIGURED,
    AI_ERROR_FORBIDDEN_403,
    AI_ERROR_INVALID_JSON,
    AI_ERROR_TIMEOUT,
    AI_ERROR_UNAUTHORIZED_401,
    SupplierPriceAIError,
    _ai_authorization_header,
    _detect_header,
    _request_ai_normalization,
)
from catalog.models import (
    Brand,
    Product,
    ProductCategory,
    ProductEmbedding,
    SupplierProductVenuePrice,
    SupplierProductVenuePriceSourceType,
    SupplierProductVenuePriceStatus,
    VenueProductMatrixItem,
    VenueSupplierProduct,
)
from core.models import (
    AuditActorType,
    AuditLog,
    AuditLogAction,
    AuditLogEntityType,
    EntityStatus,
    Order,
    OrderItem,
    OrderTemplate,
    OrderTemplateItem,
    Receiving,
    ReceivingDocument,
    Unit,
    UnitKind,
    User,
    UserRole,
    Venue,
    VenueCart,
    VenueCartItem,
)
from file_imports.models import ImportFile
from suppliers.models import (
    BotPendingAction,
    BotUserBinding,
    BotUserState,
    CabinetSession,
    PriceStatus,
    PriceType,
    ProductAvailability,
    SourceChannel,
    Supplier,
    SupplierAccountTransfer,
    SupplierAccountTransferStatus,
    SupplierAddress,
    SupplierBankAccount,
    SupplierBotLinkToken,
    SupplierContact,
    SupplierInviteLink,
    SupplierJoinRequest,
    SupplierJoinRequestStatus,
    SupplierOwnershipRequest,
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    SupplierProduct,
    SupplierProductImage,
    SupplierProductImportAction,
    SupplierProductImportItem,
    SupplierProductImportProcessingStatus,
    SupplierProductImportSession,
    SupplierProductImportSessionStatus,
    SupplierProductPrice,
    SupplierScheduleRule,
    SupplierUser,
    SupplierUserInvite,
    SupplierUserInviteStatus,
    SupplierUserRole,
    UploadItemStatus,
    UploadStatus,
    WorkflowEvent,
)


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


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierUnitListAPITests(APITestCase):
    """2026-08-09: "где брать net_unit_id?" — раньше неоткуда,
    справочник существовал только как management-команда, которую забыли
    прогнать (перенесена в data-migration 0011_seed_base_units). 2026-08-12:
    core/0012_seed_extended_units добавил ещё 33 единицы (справочник Павла) —
    больше не exact-equality с изначальными 5, проверяем, что они входят в
    более широкий набор."""

    url = "/api/v1/suppliers/units"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_rejects_request_without_auth(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)

    def test_returns_seeded_units(self):
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        codes = {u["code"] for u in response.json()}
        self.assertTrue({"кг", "г", "л", "мл", "шт"}.issubset(codes))
        self.assertEqual(len(codes), 38)

    def test_filters_by_kind(self):
        self.authenticate()

        response = self.client.get(self.url, {"kind": "weight"})

        codes = {u["code"] for u in response.json()}
        self.assertEqual(codes, {"кг", "г"})

    def test_excludes_inactive_units(self):
        Unit.objects.filter(code="шт").update(status=EntityStatus.INACTIVE)
        self.authenticate()

        response = self.client.get(self.url)

        codes = {u["code"] for u in response.json()}
        self.assertNotIn("шт", codes)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierCountryListAPITests(APITestCase):
    """2026-08-11: созвон 2026-08-10 — country_of_origin в форме товара было
    свободным текстом без источника вариантов."""

    url = "/api/v1/suppliers/countries"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_rejects_request_without_auth(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)

    def test_returns_sorted_full_country_list(self):
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        names = [c["name"] for c in response.json()]
        self.assertIn("Россия", names)
        # 2026-10-17: территории GS1 (Косово, Гонконг, Макао) добавлены для
        # распознавания страны происхождения в прайсах.
        for territory in ("Косово", "Гонконг", "Макао", "Тайвань"):
            self.assertIn(territory, names)
        self.assertEqual(names, sorted(names))
        self.assertGreater(len(names), 150)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierBrandListAPITests(APITestCase):
    """2026-08-11: созвон 2026-08-10 — подсказка по бренду при вводе, чтобы
    снизить дубли с опечатками. Не векторный поиск, обычный icontains по уже
    накопленным Brand (наполняется через _resolve_brand()'s get_or_create)."""

    url = "/api/v1/suppliers/brands"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_rejects_request_without_auth(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)

    def test_returns_up_to_50_brands_without_query(self):
        """2026-08-12: раньше проверялось exact-equality со ""всеми"" брендами —
        после catalog/0007_seed_brand_reference_data (132 реальных бренда с
        GFC) таблица больше не пустая, без query возвращается лимит 50 из
        всей таблицы, не только из того, что создал тест. Проверяем структуру
        ответа и лимит, не конкретные имена."""
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertLessEqual(len(body), 50)
        self.assertGreater(len(body), 0)
        self.assertIn("name", body[0])

    def test_newly_created_brand_appears_without_query(self):
        """2026-08-29: сортировка "без q" была по алфавиту — при 130+ брендах
        в системе только что созданный ("Ярославский бренд") физически не
        попадал в первые 50 по алфавиту, выглядело как "бренд не добавляется".
        Теперь без q сортировка по created_at (новые сверху) — свежий бренд
        обязан быть виден сразу, без ввода текста в поиск."""
        Brand.objects.create(canonical_name="Ярославский тест-бренд", normalized_name="ярославский тест-бренд")
        self.authenticate()

        response = self.client.get(self.url)

        names = [b["name"] for b in response.json()]
        self.assertEqual(names[0], "Ярославский тест-бренд")

    def test_filters_by_query_substring(self):
        Brand.objects.create(canonical_name="Тест-бренд Молочный мир", normalized_name="тест-бренд молочный мир")
        Brand.objects.create(canonical_name="Тест-бренд Молочный дом", normalized_name="тест-бренд молочный дом")
        Brand.objects.create(canonical_name="Тест-бренд Сырный край", normalized_name="тест-бренд сырный край")
        self.authenticate()

        response = self.client.get(self.url, {"q": "Тест-бренд Молоч"})

        names = {b["name"] for b in response.json()}
        self.assertEqual(names, {"Тест-бренд Молочный мир", "Тест-бренд Молочный дом"})


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierCategoryListAPITests(APITestCase):
    """2026-08-11: созвон 2026-08-10 — Витя, "мне список категорий неоткуда
    взять". В отличие от каталожного GET /catalog/categories отдаёт категории
    даже без единого товара с ценой пока (нужно для формы добавления первого
    товара). 2026-08-12: по просьбе Вити ответ сгруппирован по уровню —
    {"0": [...], "1": [...], "2": [...]} — вместо плоского списка с полем
    depth на каждой записи."""

    url = "/api/v1/suppliers/categories"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_rejects_request_without_auth(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)

    def test_returns_tree_grouped_by_depth(self):
        group = ProductCategory.objects.create(name="Продукты питания", slug="cat-test-products")
        subgroup = ProductCategory.objects.create(name="Молочная продукция", slug="cat-test-dairy", parent=group)
        view = ProductCategory.objects.create(name="Молоко", slug="cat-test-milk", parent=subgroup)
        self.authenticate()

        response = self.client.get(self.url)

        body = response.json()
        self.assertEqual(set(body.keys()), {"0", "1", "2"})
        groups_by_id = {c["id"]: c for c in body["0"]}
        subgroups_by_id = {c["id"]: c for c in body["1"]}
        views_by_id = {c["id"]: c for c in body["2"]}
        self.assertNotIn("depth", groups_by_id[str(group.id)])
        self.assertIsNone(groups_by_id[str(group.id)]["parent_id"])
        self.assertEqual(subgroups_by_id[str(subgroup.id)]["parent_id"], str(group.id))
        self.assertEqual(views_by_id[str(view.id)]["parent_id"], str(subgroup.id))

    def test_excludes_inactive_categories(self):
        ProductCategory.objects.create(
            name="Архивная категория", slug="cat-test-archived", status=EntityStatus.ARCHIVED
        )
        self.authenticate()

        response = self.client.get(self.url)

        body = response.json()
        all_names = {c["name"] for group in body.values() for c in group}
        self.assertNotIn("Архивная категория", all_names)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductListAPITests(APITestCase):
    url = "/api/v1/suppliers/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier(self, display_name="Supplier"):
        return Supplier.objects.create(
            legal_name=f"{display_name} LLC",
            display_name=display_name,
        )

    def create_unit(self, code="kg"):
        return Unit.objects.create(
            code=code,
            name=code,
            kind=UnitKind.WEIGHT,
        )

    def create_supplier_product(
        self,
        *,
        supplier=None,
        supplier_name="Raw apple",
        normalized_name="apple",
        status=EntityStatus.ACTIVE,
    ):
        supplier = supplier or self.create_supplier()
        unit = self.create_unit(code=f"kg-{Unit.objects.count()}")
        product = Product.objects.create(
            canonical_name=f"Canonical {normalized_name}",
            normalized_name=normalized_name,
            base_unit=unit,
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=supplier_name,
            normalized_name=normalized_name,
            package="box",
            min_order_qty=Decimal("2.5000"),
            status=status,
        )

    def test_rejects_request_without_service_token(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)

    def test_allows_request_with_service_token(self):
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_returns_empty_list(self):
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_supplier_cabinet_jwt_scoped_to_own_products(self):
        own = self.create_supplier("Own")
        other = self.create_supplier("Other")
        own_product = self.create_supplier_product(supplier=own, normalized_name="own")
        self.create_supplier_product(supplier=other, normalized_name="other")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([r["id"] for r in results], [str(own_product.id)])

    def test_supplier_cabinet_jwt_ignores_supplier_id_query_param(self):
        own = self.create_supplier("Own")
        other = self.create_supplier("Other")
        self.create_supplier_product(supplier=own, normalized_name="own")
        other_product = self.create_supplier_product(supplier=other, normalized_name="other")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.get(self.url, {"supplier_id": str(other.id)})

        self.assertEqual(response.status_code, 200)
        result_ids = {r["id"] for r in response.json()["results"]}
        self.assertNotIn(str(other_product.id), result_ids)

    def test_filters_by_supplier_id(self):
        first_supplier = self.create_supplier("First")
        second_supplier = self.create_supplier("Second")
        first_product = self.create_supplier_product(supplier=first_supplier, normalized_name="first")
        self.create_supplier_product(supplier=second_supplier, normalized_name="second")
        self.authenticate()

        response = self.client.get(self.url, {"supplier_id": str(first_supplier.id)})

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], str(first_product.id))
        self.assertEqual(results[0]["supplier_id"], str(first_supplier.id))

    def test_filters_by_status(self):
        active_product = self.create_supplier_product(normalized_name="active", status=EntityStatus.ACTIVE)
        self.create_supplier_product(normalized_name="archived", status=EntityStatus.ARCHIVED)
        self.authenticate()

        response = self.client.get(self.url, {"status": EntityStatus.ACTIVE})

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], str(active_product.id))
        self.assertEqual(results[0]["status"], EntityStatus.ACTIVE)

    def test_searches_by_raw_and_normalized_name(self):
        raw_match = self.create_supplier_product(
            supplier_name="Golden Pear box",
            normalized_name="pear",
        )
        normalized_match = self.create_supplier_product(
            supplier_name="Green fruit",
            normalized_name="golden kiwi",
        )
        self.create_supplier_product(
            supplier_name="Apple box",
            normalized_name="apple",
        )
        self.authenticate()

        response = self.client.get(self.url, {"q": "golden"})

        self.assertEqual(response.status_code, 200)
        result_ids = {item["id"] for item in response.json()["results"]}
        self.assertEqual(result_ids, {str(raw_match.id), str(normalized_match.id)})

    def test_returns_current_active_price(self):
        supplier_product = self.create_supplier_product(normalized_name="milk")
        old_time = timezone.now() - timezone.timedelta(days=1)
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price=Decimal("100.00"),
            status=PriceStatus.ACTIVE,
            valid_from=old_time,
        )
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price=Decimal("125.50"),
            status=PriceStatus.ACTIVE,
            valid_from=timezone.now(),
        )
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price=Decimal("99.00"),
            status=PriceStatus.SUPERSEDED,
            valid_from=timezone.now(),
        )
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        result = response.json()["results"][0]
        self.assertEqual(result["current_price"], "125.50")
        self.assertEqual(result["unit"], supplier_product.unit.code)
        self.assertEqual(result["product_name"], supplier_product.product.canonical_name)

    def test_filters_by_availability(self):
        self.create_supplier_product(normalized_name="in-stock")
        out_of_stock = self.create_supplier_product(normalized_name="out-of-stock")
        out_of_stock.availability = ProductAvailability.OUT_OF_STOCK
        out_of_stock.save(update_fields=["availability"])
        self.authenticate()

        response = self.client.get(self.url, {"availability": ProductAvailability.OUT_OF_STOCK})

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([r["id"] for r in results], [str(out_of_stock.id)])
        self.assertEqual(results[0]["availability"], ProductAvailability.OUT_OF_STOCK)

    def test_filters_by_brand_id(self):
        matching = self.create_supplier_product(normalized_name="branded")
        brand = Brand.objects.create(canonical_name="Acme", normalized_name="acme")
        matching.brand = brand
        matching.save(update_fields=["brand"])
        self.create_supplier_product(normalized_name="unbranded")
        self.authenticate()

        response = self.client.get(self.url, {"brand_id": str(brand.id)})

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([r["id"] for r in results], [str(matching.id)])
        self.assertEqual(results[0]["brand"], "Acme")

    def test_filters_by_category_id(self):
        matching = self.create_supplier_product(normalized_name="categorized")
        category = ProductCategory.objects.create(name="Dairy", slug="dairy")
        matching.category = category
        matching.save(update_fields=["category"])
        self.create_supplier_product(normalized_name="uncategorized")
        self.authenticate()

        response = self.client.get(self.url, {"category_id": str(category.id)})

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([r["id"] for r in results], [str(matching.id)])
        self.assertEqual(results[0]["category_id"], str(category.id))
        self.assertEqual(results[0]["category_name"], "Dairy")

    def test_filters_by_price_range(self):
        cheap = self.create_supplier_product(normalized_name="cheap")
        SupplierProductPrice.objects.create(
            supplier_product=cheap, price=Decimal("50.00"), status=PriceStatus.ACTIVE, valid_from=timezone.now()
        )
        mid = self.create_supplier_product(normalized_name="mid")
        SupplierProductPrice.objects.create(
            supplier_product=mid, price=Decimal("150.00"), status=PriceStatus.ACTIVE, valid_from=timezone.now()
        )
        expensive = self.create_supplier_product(normalized_name="expensive")
        SupplierProductPrice.objects.create(
            supplier_product=expensive, price=Decimal("500.00"), status=PriceStatus.ACTIVE, valid_from=timezone.now()
        )
        self.authenticate()

        response = self.client.get(self.url, {"price_min": "100", "price_max": "200"})

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([r["id"] for r in results], [str(mid.id)])

    def _create_product_with_price_history(self, *, normalized_name, old_price, new_price):
        """2026-08-03: Витя, Figma-макет — фильтр "Изменение цены" в таблице
        товаров. old_price -> SUPERSEDED, new_price -> ACTIVE, без
        зависимости от last_upload_since (иначе фильтр был бы бесполезен
        без лишнего, никак не связанного параметра)."""
        product = self.create_supplier_product(normalized_name=normalized_name)
        now = timezone.now()
        SupplierProductPrice.objects.create(
            supplier_product=product,
            price=old_price,
            price_type=PriceType.PRICE_LIST,
            status=PriceStatus.SUPERSEDED,
            valid_from=now - timezone.timedelta(days=2),
            valid_to=now - timezone.timedelta(days=1),
        )
        SupplierProductPrice.objects.create(
            supplier_product=product,
            price=new_price,
            price_type=PriceType.PRICE_LIST,
            status=PriceStatus.ACTIVE,
            valid_from=now - timezone.timedelta(days=1),
        )
        return product

    def test_filters_by_price_change_increased(self):
        increased = self._create_product_with_price_history(
            normalized_name="increased", old_price=Decimal("100.00"), new_price=Decimal("150.00")
        )
        self._create_product_with_price_history(
            normalized_name="decreased", old_price=Decimal("100.00"), new_price=Decimal("50.00")
        )
        self.create_supplier_product(normalized_name="no-history")
        self.authenticate()

        response = self.client.get(self.url, {"price_change": "increased"})

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([r["id"] for r in results], [str(increased.id)])
        self.assertEqual(results[0]["current_price"], "150.00")
        self.assertEqual(results[0]["previous_price"], "100.00")

    def test_filters_by_price_change_decreased(self):
        self._create_product_with_price_history(
            normalized_name="increased", old_price=Decimal("100.00"), new_price=Decimal("150.00")
        )
        decreased = self._create_product_with_price_history(
            normalized_name="decreased", old_price=Decimal("100.00"), new_price=Decimal("50.00")
        )
        self.authenticate()

        response = self.client.get(self.url, {"price_change": "decreased"})

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([r["id"] for r in results], [str(decreased.id)])

    def test_filters_by_price_change_unchanged(self):
        unchanged = self._create_product_with_price_history(
            normalized_name="unchanged", old_price=Decimal("100.00"), new_price=Decimal("100.00")
        )
        self._create_product_with_price_history(
            normalized_name="increased", old_price=Decimal("100.00"), new_price=Decimal("150.00")
        )
        self.authenticate()

        response = self.client.get(self.url, {"price_change": "unchanged"})

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([r["id"] for r in results], [str(unchanged.id)])

    def test_price_change_filter_excludes_products_without_history(self):
        self.create_supplier_product(normalized_name="no-history")
        self.authenticate()

        response = self.client.get(self.url, {"price_change": "increased"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_previous_price_populated_without_last_upload_since(self):
        """Регресс: раньше previous_price был null, пока не передашь
        last_upload_since — фильтр price_change на этом ничего бы не нашёл."""
        product = self._create_product_with_price_history(
            normalized_name="always-populated", old_price=Decimal("10.00"), new_price=Decimal("20.00")
        )
        self.authenticate()

        response = self.client.get(self.url)

        result = next(r for r in response.json()["results"] if r["id"] == str(product.id))
        self.assertEqual(result["previous_price"], "10.00")

    def test_returns_category_group_and_subgroup_names(self):
        group = ProductCategory.objects.create(name="Продукты питания", slug="products-food")
        subgroup = ProductCategory.objects.create(name="Фрукты", slug="fruits", parent=group)
        kind = ProductCategory.objects.create(name="Свежие фрукты", slug="fresh-fruits", parent=subgroup)
        product = self.create_supplier_product(normalized_name="orange")
        product.category = kind
        product.save(update_fields=["category"])
        self.authenticate()

        response = self.client.get(self.url)

        result = next(r for r in response.json()["results"] if r["id"] == str(product.id))
        self.assertEqual(result["category_name"], "Свежие фрукты")
        self.assertEqual(result["category_subgroup_name"], "Фрукты")
        self.assertEqual(result["category_group_name"], "Продукты питания")

    def test_category_group_and_subgroup_none_for_shallow_category(self):
        top_level = ProductCategory.objects.create(name="Без подкатегорий", slug="no-subcategories")
        product = self.create_supplier_product(normalized_name="plain")
        product.category = top_level
        product.save(update_fields=["category"])
        self.authenticate()

        response = self.client.get(self.url)

        result = next(r for r in response.json()["results"] if r["id"] == str(product.id))
        self.assertEqual(result["category_name"], "Без подкатегорий")
        self.assertIsNone(result["category_subgroup_name"])
        self.assertIsNone(result["category_group_name"])

    def test_category_group_and_subgroup_none_without_category(self):
        product = self.create_supplier_product(normalized_name="no-category")
        self.authenticate()

        response = self.client.get(self.url)

        result = next(r for r in response.json()["results"] if r["id"] == str(product.id))
        self.assertIsNone(result["category_name"])
        self.assertIsNone(result["category_subgroup_name"])
        self.assertIsNone(result["category_group_name"])

    def test_category_group_populated_subgroup_none_for_one_ancestor_level(self):
        """Exactly one ancestor (category.parent exists, no grandparent) —
        that single ancestor is the group (root-most), not the subgroup."""
        group = ProductCategory.objects.create(name="Напитки", slug="drinks")
        kind = ProductCategory.objects.create(name="Соки", slug="juices", parent=group)
        product = self.create_supplier_product(normalized_name="apple-juice")
        product.category = kind
        product.save(update_fields=["category"])
        self.authenticate()

        response = self.client.get(self.url)

        result = next(r for r in response.json()["results"] if r["id"] == str(product.id))
        self.assertEqual(result["category_name"], "Соки")
        self.assertEqual(result["category_group_name"], "Напитки")
        self.assertIsNone(result["category_subgroup_name"])

    def test_query_count_does_not_scale_with_product_count(self):
        """Regression guard for the 2026-08-15 N+1 fix —
        _category_group_and_subgroup_names() used to read one level past
        select_related("category__parent__parent")'s depth, firing one lazy
        query per product row (measured live: 50 extra queries for a
        50-item page, ~47ms round-trip each). Query count for the list
        endpoint must not grow with the number of returned products."""
        from django.test.utils import CaptureQueriesContext

        group = ProductCategory.objects.create(name="Бакалея", slug="grocery-scale-test")
        subgroup = ProductCategory.objects.create(name="Крупы", slug="grains-scale-test", parent=group)
        kind = ProductCategory.objects.create(name="Рис", slug="rice-scale-test", parent=subgroup)
        supplier = Supplier.objects.create(legal_name="Scale Query LLC", display_name="Scale Query")
        for i in range(6):
            product = self.create_supplier_product(
                supplier=supplier, supplier_name=f"Raw rice {i}", normalized_name=f"rice-{i}"
            )
            product.category = kind
            product.save(update_fields=["category"])
        self.authenticate()

        with CaptureQueriesContext(connections["default"]) as small_ctx:
            self.client.get(self.url, {"limit": 2})
        with CaptureQueriesContext(connections["default"]) as large_ctx:
            self.client.get(self.url, {"limit": 6})

        self.assertEqual(len(small_ctx.captured_queries), len(large_ctx.captured_queries))

    def test_returns_country_of_origin_and_net_value(self):
        net_unit = Unit.objects.create(code="kg-w", name="kg-w", kind=UnitKind.WEIGHT)
        product = self.create_supplier_product(normalized_name="olive-oil")
        product.country_of_origin = "Италия"
        product.net_value = Decimal("0.9000")
        product.net_unit = net_unit
        product.save(update_fields=["country_of_origin", "net_value", "net_unit"])
        self.authenticate()

        response = self.client.get(self.url)

        result = next(r for r in response.json()["results"] if r["id"] == str(product.id))
        self.assertEqual(result["country_of_origin"], "Италия")
        self.assertEqual(result["net_value"], "0.9000")
        self.assertEqual(result["net_unit"], "kg-w")

    def test_country_of_origin_and_net_value_null_by_default(self):
        product = self.create_supplier_product(normalized_name="unspecified-origin")
        self.authenticate()

        response = self.client.get(self.url)

        result = next(r for r in response.json()["results"] if r["id"] == str(product.id))
        self.assertIsNone(result["country_of_origin"])
        self.assertIsNone(result["net_value"])
        self.assertIsNone(result["net_unit"])

    def test_created_at_always_populated_including_for_products_without_upload(self):
        """2026-08-09: Витя — "при создании дата загрузки не проставляется".
        uploaded_at=null для товаров без загрузки прайса — это правильно (нет
        загрузки, значит и нет её даты), но created_at должен быть заполнен
        всегда — раньше его вообще не было в ответе."""
        product = self.create_supplier_product(normalized_name="no-upload-date")
        self.authenticate()

        response = self.client.get(self.url)

        result = next(r for r in response.json()["results"] if r["id"] == str(product.id))
        self.assertIsNotNone(result["created_at"])
        self.assertIsNone(result["uploaded_at"])


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductOrderingAPITests(APITestCase):
    """2026-08-09: "у товаров компании сейчас нет сортировок — необходимо
    сделать по всем полям"."""

    url = "/api/v1/suppliers/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier(self, display_name="Supplier"):
        return Supplier.objects.create(legal_name=f"{display_name} LLC", display_name=display_name)

    def test_orders_by_plain_field_ascending_and_descending(self):
        supplier = self.create_supplier()
        low = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="A товар", normalized_name="a", min_order_qty=Decimal("1.0000")
        )
        mid = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="B товар", normalized_name="b", min_order_qty=Decimal("5.0000")
        )
        high = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="C товар", normalized_name="c", min_order_qty=Decimal("10.0000")
        )
        self.authenticate()

        asc = self.client.get(self.url, {"ordering": "min_order_qty"})
        self.assertEqual([r["id"] for r in asc.json()["results"]], [str(low.id), str(mid.id), str(high.id)])

        desc = self.client.get(self.url, {"ordering": "-min_order_qty"})
        self.assertEqual([r["id"] for r in desc.json()["results"]], [str(high.id), str(mid.id), str(low.id)])

    def test_orders_by_denormalized_current_price(self):
        supplier = self.create_supplier()
        cheap = SupplierProduct.objects.create(supplier=supplier, supplier_name="Cheap", normalized_name="cheap")
        SupplierProductPrice.objects.create(supplier_product=cheap, price=Decimal("10.00"))
        pricey = SupplierProduct.objects.create(supplier=supplier, supplier_name="Pricey", normalized_name="pricey")
        SupplierProductPrice.objects.create(supplier_product=pricey, price=Decimal("999.00"))
        self.authenticate()

        response = self.client.get(self.url, {"ordering": "-current_price"})

        self.assertEqual([r["id"] for r in response.json()["results"]], [str(pricey.id), str(cheap.id)])

    def test_orders_by_related_field_brand_name(self):
        supplier = self.create_supplier()
        brand_a = Brand.objects.create(canonical_name="Ама", normalized_name="ama-order")
        brand_z = Brand.objects.create(canonical_name="Зета", normalized_name="zeta-order")
        p_z = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="P1", normalized_name="p1-order", brand=brand_z
        )
        p_a = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="P2", normalized_name="p2-order", brand=brand_a
        )
        self.authenticate()

        response = self.client.get(self.url, {"ordering": "brand"})

        self.assertEqual([r["id"] for r in response.json()["results"]], [str(p_a.id), str(p_z.id)])

    def test_orders_by_category_group_name(self):
        supplier = self.create_supplier()
        group_a = ProductCategory.objects.create(name="А-группа", slug="a-group-order")
        group_z = ProductCategory.objects.create(name="Я-группа", slug="z-group-order")
        subgroup_a = ProductCategory.objects.create(name="Sub A", slug="sub-a-order", parent=group_a)
        subgroup_z = ProductCategory.objects.create(name="Sub Z", slug="sub-z-order", parent=group_z)
        kind_a = ProductCategory.objects.create(name="Kind A", slug="kind-a-order", parent=subgroup_a)
        kind_z = ProductCategory.objects.create(name="Kind Z", slug="kind-z-order", parent=subgroup_z)
        p_z = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="PZ", normalized_name="pz-order", category=kind_z
        )
        p_a = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="PA", normalized_name="pa-order", category=kind_a
        )
        self.authenticate()

        response = self.client.get(self.url, {"ordering": "category_group_name"})

        self.assertEqual([r["id"] for r in response.json()["results"]], [str(p_a.id), str(p_z.id)])

    def test_rejects_unknown_ordering_field(self):
        self.authenticate()

        response = self.client.get(self.url, {"ordering": "nonexistent_field"})

        self.assertEqual(response.status_code, 400)

    def test_rejects_unknown_ordering_field_with_minus_prefix(self):
        self.authenticate()

        response = self.client.get(self.url, {"ordering": "-nonexistent_field"})

        self.assertEqual(response.status_code, 400)

    def test_omitted_ordering_keeps_default_behavior(self):
        self.create_supplier_product_for_default_check()
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def create_supplier_product_for_default_check(self):
        supplier = self.create_supplier()
        return SupplierProduct.objects.create(supplier=supplier, supplier_name="Default", normalized_name="default")


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductCreateAPITests(APITestCase):
    """2026-08-08: ручное добавление товара — раньше товары появлялись
    только через загрузку прайса."""

    url = "/api/v1/suppliers/products"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier(self, display_name="Supplier"):
        return Supplier.objects.create(legal_name=f"{display_name} LLC", display_name=display_name)

    def test_rejects_request_without_auth(self):
        response = self.client.post(self.url, {"supplier_raw_name": "Товар"}, format="json")

        self.assertEqual(response.status_code, 401)

    def test_service_token_requires_supplier_id(self):
        self.authenticate()

        response = self.client.post(self.url, {"supplier_raw_name": "Товар"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("supplier_id", response.json())

    def test_service_token_creates_with_explicit_supplier_id(self):
        supplier = self.create_supplier()
        self.authenticate()

        response = self.client.post(
            self.url,
            {"supplier_id": str(supplier.id), "supplier_raw_name": "Товар от сервиса"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["supplier_id"], str(supplier.id))

    def test_cabinet_jwt_creates_product_scoped_to_own_supplier(self):
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(self.url, {"supplier_raw_name": "Свой товар"}, format="json")

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["supplier_id"], str(supplier.id))
        self.assertEqual(body["supplier_raw_name"], "Свой товар")

    def test_cabinet_jwt_ignores_supplier_id_in_body(self):
        own = self.create_supplier("Own")
        other = self.create_supplier("Other")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))

        response = self.client.post(
            self.url,
            {"supplier_id": str(other.id), "supplier_raw_name": "Should belong to own"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["supplier_id"], str(own.id))

    def test_normalized_name_defaults_to_raw_name(self):
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(self.url, {"supplier_raw_name": "Только сырое имя"}, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["supplier_normalized_name"], "Только сырое имя")

    def test_raw_name_defaults_to_normalized_name(self):
        """2026-08-10: Витя — фронт при ручном добавлении присылает только
        supplier_normalized_name (у пользователя одно название в форме, не
        два), а supplier_raw_name был жёстко обязателен. Симметричный
        фолбэк normalized->raw, зеркально существующему raw->normalized."""
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(self.url, {"supplier_normalized_name": "Только норм. имя"}, format="json")

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["supplier_raw_name"], "Только норм. имя")
        self.assertEqual(body["supplier_normalized_name"], "Только норм. имя")

    def test_requires_at_least_one_name_field(self):
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("supplier_raw_name", response.json())

    def test_accepts_explicit_null_for_optional_fields(self):
        """2026-08-10: Витя — POST 400 с 'Это поле не может быть пустым.' на
        volume_value/volume_unit_id (до объединения в net_value/net_unit,
        2026-08-15), хотя товар (помидор) физически имеет только вес.
        required=False разрешает ОПУСТИТЬ поле, но не разрешает явный null без
        allow_null=True — а фронт шлёт null для незаполненных опциональных
        полей, а не опускает их. PATCH (Update serializer) уже делал
        allow_null=True везде; Create — нет. Проверяем набор nullable-полей
        тем же payload'ом, которым воспроизвёл баг Витя."""
        supplier = self.create_supplier()
        net_unit = Unit.objects.create(code="kg-null-create", name="kg", kind=UnitKind.WEIGHT)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(
            self.url,
            {
                "supplier_normalized_name": "помидор",
                "brand": "безос",
                "country_of_origin": "россия",
                "price": "100",
                "availability": "in_stock",
                "unit_id": None,
                "min_order_qty": "10",
                "delivery_multiple": "10",
                "net_value": "10",
                "net_unit_id": str(net_unit.id),
                "category_path": ["ыва", "sdf", "sdf"],
                "comment": "dsf",
                "package": None,
                "image_url": None,
                "price_comment": None,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsNone(body["package"])
        self.assertIsNone(body["image_url"])
        self.assertEqual(body["net_value"], "10.0000")

    def test_creates_with_all_table_columns(self):
        supplier = self.create_supplier()
        unit = Unit.objects.create(code="pcs-create", name="pcs-create", kind=UnitKind.PIECE)
        net_unit = Unit.objects.create(code="kg-create", name="kg-create", kind=UnitKind.WEIGHT)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(
            self.url,
            {
                "supplier_raw_name": "Оливковое масло",
                "brand": "Bertolli",
                "country_of_origin": "Италия",
                "unit_id": str(unit.id),
                "price": "450.00",
                "delivery_multiple": "6.0000",
                "net_value": "0.9000",
                "net_unit_id": str(net_unit.id),
                "min_order_qty": "1.0000",
                "availability": "in_stock",
                "category_path": ["Бакалея", "Масла", "Оливковое"],
                "comment": "Проверить срок годности при приёмке",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["supplier_id"], str(supplier.id))
        self.assertEqual(body["brand"], "Bertolli")
        self.assertEqual(body["country_of_origin"], "Италия")
        self.assertEqual(body["unit"], "pcs-create")
        self.assertEqual(body["current_price"], "450.00")
        self.assertEqual(body["delivery_multiple"], "6.0000")
        self.assertEqual(body["net_value"], "0.9000")
        self.assertEqual(body["net_unit"], "kg-create")
        self.assertEqual(body["min_order_qty"], "1.0000")
        self.assertEqual(body["availability"], "in_stock")
        self.assertEqual(body["category_name"], "Оливковое")
        self.assertEqual(body["category_subgroup_name"], "Масла")
        self.assertEqual(body["category_group_name"], "Бакалея")
        self.assertEqual(body["comment"], "Проверить срок годности при приёмке")

    def test_creates_with_existing_brand_id(self):
        """2026-08-28, Vitya: GET /suppliers/brands (автокомплит) не имело
        парного поля, чтобы отправить назад выбранный существующий бренд —
        было только текстовое brand (get_or_create)."""
        supplier = self.create_supplier()
        brand = Brand.objects.create(
            canonical_name="Тестовый Кетчуп Бренд XYZ", normalized_name="тестовый кетчуп бренд xyz"
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(
            self.url,
            {"supplier_raw_name": "Кетчуп", "brand_id": str(brand.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["brand"], "Тестовый Кетчуп Бренд XYZ")
        self.assertEqual(Brand.objects.filter(normalized_name="тестовый кетчуп бренд xyz").count(), 1)

    def test_comment_null_by_default(self):
        """2026-08-10: Витя — "нету поля комментария" в POST /suppliers/products."""
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(self.url, {"supplier_raw_name": "Без комментария"}, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(response.json()["comment"])

    def test_creates_with_package_and_image_url(self):
        """2026-08-10: сверка полей POST/PATCH — package и image_url были в
        PATCH и в списке, но не в POST."""
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(
            self.url,
            {
                "supplier_raw_name": "Товар с упаковкой",
                "package": "коробка 12 шт",
                "image_url": "https://example.com/photo.jpg",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["package"], "коробка 12 шт")
        self.assertEqual(response.json()["image_url"], "https://example.com/photo.jpg")

    def test_rejects_duplicate_name_for_same_supplier(self):
        supplier = self.create_supplier()
        SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Уже есть",
            normalized_name="уже есть",
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(self.url, {"supplier_raw_name": "Уже есть"}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_appears_in_subsequent_list(self):
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        create_response = self.client.post(self.url, {"supplier_raw_name": "Новый товар"}, format="json")
        new_id = create_response.json()["id"]

        list_response = self.client.get(self.url)

        result_ids = {item["id"] for item in list_response.json()["results"]}
        self.assertIn(new_id, result_ids)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductAutofillAPITests(APITestCase):
    """TASK-87/TASK-69 задача 6: рекомендация для карточки товара — только
    предлагает значения, ничего не создаёт/не сохраняет."""

    url = "/api/v1/suppliers/products/autofill"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier(self, display_name="Supplier"):
        return Supplier.objects.create(legal_name=f"{display_name} LLC", display_name=display_name)

    def create_unit(self, code=None):
        code = code or f"unit-{uuid4().hex[:8]}"
        return Unit.objects.create(code=code, name=code, kind=UnitKind.WEIGHT)

    def test_rejects_request_without_auth(self):
        response = self.client.post(self.url, {"name": "Молоко"}, format="json")

        self.assertEqual(response.status_code, 401)

    def test_service_token_requires_supplier_id(self):
        self.authenticate()

        response = self.client.post(self.url, {"name": "Молоко"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("supplier_id", response.json())

    def test_rejects_blank_name(self):
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.post(self.url, {"name": ""}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json())

    def test_service_token_requires_existing_supplier(self):
        self.authenticate()

        response = self.client.post(
            self.url, {"supplier_id": "00000000-0000-0000-0000-000000000000", "name": "Молоко"}, format="json"
        )

        self.assertEqual(response.status_code, 404)

    def test_returns_suggestion_from_llm(self):
        supplier = self.create_supplier()
        unit = self.create_unit()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        with patch("api.v1.suppliers.services.products.suggest_product_card") as mocked:
            mocked.return_value = SimpleNamespace(
                normalized_name="Молоко 3.2% 1л",
                suggested_unit_id=unit.id,
                candidate_product_ids=[],
            )
            response = self.client.post(self.url, {"name": "молоко"}, format="json")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["normalized_name"], "Молоко 3.2% 1л")
        self.assertEqual(body["suggested_unit"]["id"], str(unit.id))
        self.assertEqual(body["candidates"], [])

    def test_does_not_suggest_unit_when_already_provided(self):
        """product_card_llm.suggest_product_card само не заменяет unit_id,
        если он уже был на входе — но проверяем и на уровне эндпоинта, что
        уже выбранная единица уходит в вызов и не подменяется в ответе."""
        supplier = self.create_supplier()
        existing_unit = self.create_unit()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        with patch("api.v1.suppliers.services.products.suggest_product_card") as mocked:
            mocked.return_value = SimpleNamespace(
                normalized_name="Молоко 3.2% 1л",
                suggested_unit_id=None,
                candidate_product_ids=[],
            )
            response = self.client.post(self.url, {"name": "молоко", "unit_id": str(existing_unit.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["suggested_unit"])
        called_kwargs = mocked.call_args.kwargs
        self.assertEqual(called_kwargs["unit_id"], existing_unit.id)

    def test_returns_ranked_candidates(self):
        supplier = self.create_supplier()
        product = Product.objects.create(canonical_name="Молоко Домик в деревне 3.2% 1л", normalized_name="молоко")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        with patch("api.v1.suppliers.services.products.find_candidate_catalog_products", return_value=[product]):
            with patch("api.v1.suppliers.services.products.suggest_product_card") as mocked:
                mocked.return_value = SimpleNamespace(
                    normalized_name="Молоко",
                    suggested_unit_id=None,
                    candidate_product_ids=[product.id],
                )
                response = self.client.post(self.url, {"name": "молоко"}, format="json")

        self.assertEqual(response.status_code, 200)
        candidates = response.json()["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], str(product.id))
        self.assertEqual(candidates[0]["canonical_name"], product.canonical_name)

    def test_falls_back_when_llm_unavailable(self):
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        with patch("api.v1.suppliers.services.products.suggest_product_card", side_effect=LLMConnectorError("down")):
            response = self.client.post(self.url, {"name": "молоко"}, format="json")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["normalized_name"], "молоко")
        self.assertIsNone(body["suggested_unit"])
        self.assertEqual(body["candidates"], [])

    def test_falls_back_when_llm_returns_invalid_suggestion(self):
        """suggest_product_card подтверждено (product_card_llm.py) само
        бросает ValueError, если LLM вернула unit/candidate вне разрешённых
        списков — эндпоинт должен пережить это так же, как отказ провайдера."""
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        with patch(
            "api.v1.suppliers.services.products.suggest_product_card",
            side_effect=ValueError("LLM returned a unit outside allowed_units."),
        ):
            response = self.client.post(self.url, {"name": "молоко"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["available"])

    @override_settings(SUPPLIER_PRODUCT_CARD_LLM_ENABLED=False)
    def test_returns_unavailable_when_feature_flag_disabled(self):
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        with patch("api.v1.suppliers.services.products.suggest_product_card") as mocked:
            response = self.client.post(self.url, {"name": "молоко"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["available"])
        mocked.assert_not_called()


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductPriceHistoryAPITests(APITestCase):
    """2026-08-09: истории изменения цен для Вити — данные уже были
    (SupplierProductPrice копится на каждое изменение, не апдейтится на
    месте), не хватало только ручки чтения."""

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier(self, display_name="Supplier"):
        return Supplier.objects.create(legal_name=f"{display_name} LLC", display_name=display_name)

    def create_supplier_product(self, supplier=None):
        supplier = supplier or self.create_supplier()
        return SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Товар с историей цен",
            normalized_name="товар с историей цен",
        )

    def url(self, supplier_product):
        return f"/api/v1/suppliers/products/{supplier_product.id}/prices"

    def test_rejects_request_without_auth(self):
        supplier_product = self.create_supplier_product()

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.status_code, 401)

    def test_returns_empty_list_when_no_price_ever_set(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_cabinet_jwt_cannot_view_other_suppliers_product_history(self):
        own = self.create_supplier("Own")
        other_product = self.create_supplier_product(self.create_supplier("Other"))
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))

        response = self.client.get(self.url(other_product))

        self.assertEqual(response.status_code, 404)

    def test_price_change_creates_history_ordered_newest_first(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        self.client.patch(
            f"/api/v1/suppliers/products/{supplier_product.id}",
            {"price": "100.00"},
            format="json",
        )
        self.client.patch(
            f"/api/v1/suppliers/products/{supplier_product.id}",
            {"price": "150.00"},
            format="json",
        )

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.status_code, 200)
        prices = [row["price"] for row in response.json()]
        statuses = [row["status"] for row in response.json()]
        self.assertEqual(prices, ["150.00", "100.00"])
        self.assertEqual(statuses, ["active", "superseded"])

    def test_source_manual_for_price_set_via_api(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        self.client.patch(
            f"/api/v1/suppliers/products/{supplier_product.id}",
            {"price": "42.00"},
            format="json",
        )

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.json()[0]["source"], "manual")

    def test_source_upload_for_price_from_price_upload(self):
        supplier_product = self.create_supplier_product()
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier_product.supplier,
            source_channel=SourceChannel.WEB,
        )
        upload_item = SupplierPriceUploadItem.objects.create(
            upload=upload,
            supplier_product=supplier_product,
            raw_name=supplier_product.supplier_name,
        )
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            upload_item=upload_item,
            price=Decimal("77.00"),
        )
        self.authenticate()

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.json()[0]["source"], "upload")

    def test_archiving_marks_history_cancelled_not_deleted(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        self.client.patch(
            f"/api/v1/suppliers/products/{supplier_product.id}",
            {"price": "10.00"},
            format="json",
        )
        self.client.post(f"/api/v1/suppliers/products/{supplier_product.id}/archive")

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["status"], "cancelled")

    def test_previous_price_and_change_percent(self):
        """2026-08-10: Figma "История изменения цены" — переход было→стало
        с процентом. previous_price/change_percent вычисляются по соседней
        (более старой) записи в уже отсортированном списке."""
        supplier_product = self.create_supplier_product()
        self.authenticate()
        self.client.patch(f"/api/v1/suppliers/products/{supplier_product.id}", {"price": "100.00"}, format="json")
        self.client.patch(f"/api/v1/suppliers/products/{supplier_product.id}", {"price": "115.00"}, format="json")

        response = self.client.get(self.url(supplier_product))

        rows = response.json()
        newest, oldest = rows[0], rows[1]
        self.assertEqual(newest["price"], "115.00")
        self.assertEqual(newest["previous_price"], "100.00")
        self.assertEqual(newest["change_percent"], "15.00")
        self.assertIsNone(oldest["previous_price"])
        self.assertIsNone(oldest["change_percent"])

    def test_change_percent_negative_for_price_decrease(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        self.client.patch(f"/api/v1/suppliers/products/{supplier_product.id}", {"price": "200.00"}, format="json")
        self.client.patch(f"/api/v1/suppliers/products/{supplier_product.id}", {"price": "180.00"}, format="json")

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.json()[0]["change_percent"], "-10.00")

    def test_supplier_name_present_on_every_row(self):
        supplier = self.create_supplier("Известный поставщик")
        supplier_product = self.create_supplier_product(supplier)
        self.authenticate()
        self.client.patch(f"/api/v1/suppliers/products/{supplier_product.id}", {"price": "10.00"}, format="json")

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.json()[0]["supplier_name"], "Известный поставщик")

    def test_price_comment_stored_and_returned(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()

        self.client.patch(
            f"/api/v1/suppliers/products/{supplier_product.id}",
            {"price": "50.00", "price_comment": "Скидка при заказе от 50 кг"},
            format="json",
        )

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.json()[0]["comment"], "Скидка при заказе от 50 кг")

    def test_price_comment_null_when_not_provided(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()

        self.client.patch(f"/api/v1/suppliers/products/{supplier_product.id}", {"price": "50.00"}, format="json")

        response = self.client.get(self.url(supplier_product))

        self.assertIsNone(response.json()[0]["comment"])

    def test_price_comment_accepted_on_create(self):
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        create_response = self.client.post(
            "/api/v1/suppliers/products",
            {"supplier_raw_name": "Товар с ценой и причиной", "price": "99.00", "price_comment": "Стартовая цена"},
            format="json",
        )

        product_id = create_response.json()["id"]
        response = self.client.get(f"/api/v1/suppliers/products/{product_id}/prices")

        self.assertEqual(response.json()[0]["comment"], "Стартовая цена")


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductIndividualPriceAPITests(APITestCase):
    """2026-09-07 (Павел's "Поставщик Изменения цен - ТЗ"): actor is the
    supplier's own cabinet — the opposite side from SupplierProductVenuePrice
    Plan 2 (api.v1.catalog), which stayed as venue-side read-only
    (see api.v1.catalog.tests.SupplierProductVenuePriceListAPITests)."""

    def create_supplier(self, display_name="Supplier"):
        return Supplier.objects.create(legal_name=f"{display_name} LLC", display_name=display_name)

    def create_supplier_product(self, supplier=None, price="100.00"):
        supplier = supplier or self.create_supplier()
        return SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Сыр Пармезан",
            normalized_name="Сыр Пармезан",
            current_price=Decimal(price),
        )

    def url(self, supplier_product):
        return f"/api/v1/suppliers/products/{supplier_product.id}/individual-prices"

    def archive_url(self, supplier_product, price):
        return f"/api/v1/suppliers/products/{supplier_product.id}/individual-prices/{price.id}/archive"

    def test_create_requires_authentication(self):
        supplier_product = self.create_supplier_product()
        venue = Venue.objects.create(display_name="Cafe X")
        response = self.client.post(
            self.url(supplier_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_create_requires_active_membership_not_just_cabinet_session(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        # A bare cabinet session (no linked core.User, e.g. bot-issued) is
        # not an active member — same distinction IsSupplierCabinetActiveMember
        # already draws for delete/bank-account actions.
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.post(
            self.url(supplier_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_create_one_venue(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        response = self.client.post(
            self.url(supplier_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json"
        )

        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["price"], "90.00")
        self.assertEqual(data[0]["venue_id"], str(venue.id))
        self.assertEqual(data[0]["status"], "active")
        self.assertEqual(data[0]["source_type"], "manual")

    def test_create_several_venues_shares_one_group_id(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue_a = Venue.objects.create(display_name="Cafe A")
        venue_b = Venue.objects.create(display_name="Cafe B")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        response = self.client.post(
            self.url(supplier_product),
            {"venue_ids": [str(venue_a.id), str(venue_b.id)], "price": "90.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(len({row["group_id"] for row in data}), 1)

    def test_create_rejects_venue_belonging_to_nobody_in_particular(self):
        # There is no "does the supplier manage this venue" concept — the
        # doc explicitly wants supplier to look venues up by ИНН/name (an
        # existing venues/search-by-inn endpoint) and assign to any active
        # one. Only existence/активность is checked here.
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        response = self.client.post(
            self.url(supplier_product), {"venue_ids": [str(uuid4())], "price": "90.00"}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_create_denied_for_other_suppliers_product(self):
        own = self.create_supplier("Own")
        other_product = self.create_supplier_product(self.create_supplier("Other"))
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(own))

        response = self.client.post(
            self.url(other_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json"
        )

        self.assertEqual(response.status_code, 404)

    def test_conflict_returns_409_without_writing(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        self.client.post(self.url(supplier_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json")

        response = self.client.post(
            self.url(supplier_product), {"venue_ids": [str(venue.id)], "price": "80.00"}, format="json"
        )

        self.assertEqual(response.status_code, 409, response.content)
        conflicts = response.json()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["venue_id"], str(venue.id))
        self.assertEqual(conflicts[0]["price"], "90.00")
        self.assertEqual(
            SupplierProductVenuePrice.objects.filter(
                supplier_product=supplier_product, status=SupplierProductVenuePriceStatus.ACTIVE
            ).count(),
            1,
        )

    def test_confirm_replace_overrides_conflict(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        self.client.post(self.url(supplier_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json")

        response = self.client.post(
            self.url(supplier_product),
            {"venue_ids": [str(venue.id)], "price": "80.00", "confirm_replace": True},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            SupplierProductVenuePrice.objects.filter(
                supplier_product=supplier_product, status=SupplierProductVenuePriceStatus.ACTIVE
            ).count(),
            1,
        )
        self.assertEqual(
            SupplierProductVenuePrice.objects.get(status=SupplierProductVenuePriceStatus.ACTIVE).price,
            Decimal("80.00"),
        )

    def test_list_all_venues_for_product(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue_a = Venue.objects.create(display_name="Cafe A")
        venue_b = Venue.objects.create(display_name="Cafe B")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        self.client.post(self.url(supplier_product), {"venue_ids": [str(venue_a.id)], "price": "90.00"}, format="json")
        self.client.post(self.url(supplier_product), {"venue_ids": [str(venue_b.id)], "price": "95.00"}, format="json")

        response = self.client.get(self.url(supplier_product))

        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["venue_id"] for row in response.json()}, {str(venue_a.id), str(venue_b.id)})

    def test_list_filters_by_venue_id(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue_a = Venue.objects.create(display_name="Cafe A")
        venue_b = Venue.objects.create(display_name="Cafe B")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        self.client.post(self.url(supplier_product), {"venue_ids": [str(venue_a.id)], "price": "90.00"}, format="json")
        self.client.post(self.url(supplier_product), {"venue_ids": [str(venue_b.id)], "price": "95.00"}, format="json")

        response = self.client.get(self.url(supplier_product), {"venue_id": str(venue_a.id)})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["venue_id"], str(venue_a.id))

    def test_list_available_via_service_token_too(self):
        supplier_product = self.create_supplier_product()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.get(self.url(supplier_product))
        self.assertEqual(response.status_code, 200)

    def test_archive(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        create_response = self.client.post(
            self.url(supplier_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json"
        )
        price = SupplierProductVenuePrice.objects.get(pk=create_response.json()[0]["id"])

        response = self.client.post(self.archive_url(supplier_product, price))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "archived")

    def test_archive_denied_for_other_suppliers_price(self):
        own = self.create_supplier("Own")
        other_supplier = self.create_supplier("Other")
        other_product = self.create_supplier_product(other_supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(other_supplier))
        create_response = self.client.post(
            self.url(other_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json"
        )
        price = SupplierProductVenuePrice.objects.get(pk=create_response.json()[0]["id"])

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(own))
        response = self.client.post(self.archive_url(other_product, price))

        self.assertEqual(response.status_code, 404)

    # --- Задача 3 (§29 ТЗ): редактирование/удаление ещё не наступившей ручной цены ---

    def detail_url(self, supplier_product, price):
        return f"/api/v1/suppliers/products/{supplier_product.id}/individual-prices/{price.id}"

    def create_future_price(self, supplier, supplier_product, venue, price="90.00", days_ahead=3):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        valid_from = (timezone.now() + timedelta(days=days_ahead)).isoformat()
        create_response = self.client.post(
            self.url(supplier_product),
            {"venue_ids": [str(venue.id)], "price": price, "valid_from": valid_from},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201, create_response.content)
        return SupplierProductVenuePrice.objects.get(pk=create_response.json()[0]["id"])

    def test_patch_future_manual_price(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        price = self.create_future_price(supplier, supplier_product, venue)

        response = self.client.patch(
            self.detail_url(supplier_product, price), {"price": "120.00", "comment": "Уточнили"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["price"], "120.00")
        self.assertEqual(data["comment"], "Уточнили")
        price.refresh_from_db()
        self.assertEqual(price.price, Decimal("120.00"))

    def test_patch_rejects_already_active_price(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        create_response = self.client.post(
            self.url(supplier_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json"
        )
        price = SupplierProductVenuePrice.objects.get(pk=create_response.json()[0]["id"])

        response = self.client.patch(self.detail_url(supplier_product, price), {"price": "120.00"}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_patch_rejects_tender_sourced_price(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=supplier_product,
            venue=venue,
            price=Decimal("300.00"),
            valid_from=timezone.now() + timedelta(days=3),
            source_type=SupplierProductVenuePriceSourceType.TENDER,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        response = self.client.patch(self.detail_url(supplier_product, price), {"price": "320.00"}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_patch_returns_409_on_conflict_with_another_active_price(self):
        # 2026-09-21 final-review fix wave, Fix 1: moving a future price's
        # valid_from into an already-active sibling's window used to be
        # silently accepted (200) — the PATCH path never ran the create
        # path's conflict check. Now it's a 409, same shape as the create
        # endpoint's own conflict response, and nothing is written.
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        # Bounded so it doesn't itself conflict with the day-10 future price
        # created right below — only the later PATCH is expected to collide.
        active_valid_to = (timezone.now() + timedelta(days=5)).isoformat()
        active_response = self.client.post(
            self.url(supplier_product),
            {"venue_ids": [str(venue.id)], "price": "90.00", "valid_to": active_valid_to},
            format="json",
        )
        self.assertEqual(active_response.status_code, 201, active_response.content)

        future_price = self.create_future_price(supplier, supplier_product, venue, price="70.00", days_ahead=10)
        original_valid_from = future_price.valid_from

        new_valid_from = (timezone.now() + timedelta(hours=1)).isoformat()
        response = self.client.patch(
            self.detail_url(supplier_product, future_price), {"valid_from": new_valid_from}, format="json"
        )

        self.assertEqual(response.status_code, 409, response.content)
        conflicts = response.json()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["venue_id"], str(venue.id))
        self.assertEqual(conflicts[0]["price"], "90.00")
        future_price.refresh_from_db()
        self.assertEqual(future_price.valid_from, original_valid_from)
        self.assertEqual(future_price.price, Decimal("70.00"))

    def test_patch_rejects_valid_to_before_valid_from(self):
        # Regression: full_clean() inside update_supplier_product_venue_price validates the
        # sp_venue_prices_period CheckConstraint (valid_to > valid_from) and raises Django's
        # own ValidationError, a different class from rest_framework's — unless the view
        # catches both, this used to surface as an unhandled 500 instead of a 400.
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        price = self.create_future_price(supplier, supplier_product, venue, days_ahead=5)

        bad_valid_to = (timezone.now() + timedelta(days=1)).isoformat()
        response = self.client.patch(
            self.detail_url(supplier_product, price), {"valid_to": bad_valid_to}, format="json"
        )

        self.assertEqual(response.status_code, 400, response.content)
        price.refresh_from_db()
        self.assertIsNone(price.valid_to)

    def test_patch_denied_for_other_suppliers_price(self):
        own = self.create_supplier("Own")
        other_supplier = self.create_supplier("Other")
        other_product = self.create_supplier_product(other_supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        price = self.create_future_price(other_supplier, other_product, venue)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(own))
        response = self.client.patch(self.detail_url(other_product, price), {"price": "120.00"}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_patch_clears_valid_to_when_explicitly_set_to_null(self):
        # 2026-09-21 final-review fix wave, Fix 2: PATCH {"valid_to": null}
        # used to be silently ignored — the service couldn't tell "field
        # absent" from "field explicitly null" (both surfaced as None).
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        valid_from = (timezone.now() + timedelta(days=3)).isoformat()
        valid_to = (timezone.now() + timedelta(days=30)).isoformat()
        create_response = self.client.post(
            self.url(supplier_product),
            {"venue_ids": [str(venue.id)], "price": "90.00", "valid_from": valid_from, "valid_to": valid_to},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201, create_response.content)
        price = SupplierProductVenuePrice.objects.get(pk=create_response.json()[0]["id"])
        self.assertIsNotNone(price.valid_to)

        response = self.client.patch(self.detail_url(supplier_product, price), {"valid_to": None}, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["valid_to"])
        price.refresh_from_db()
        self.assertIsNone(price.valid_to)

    def test_delete_future_manual_price(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        price = self.create_future_price(supplier, supplier_product, venue)

        response = self.client.delete(self.detail_url(supplier_product, price))

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(SupplierProductVenuePrice.objects.filter(pk=price.pk).exists())

    def test_delete_rejects_already_active_price(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))
        create_response = self.client.post(
            self.url(supplier_product), {"venue_ids": [str(venue.id)], "price": "90.00"}, format="json"
        )
        price = SupplierProductVenuePrice.objects.get(pk=create_response.json()[0]["id"])

        response = self.client.delete(self.detail_url(supplier_product, price))

        self.assertEqual(response.status_code, 400)
        self.assertTrue(SupplierProductVenuePrice.objects.filter(pk=price.pk).exists())

    def test_delete_denied_for_other_suppliers_price(self):
        own = self.create_supplier("Own")
        other_supplier = self.create_supplier("Other")
        other_product = self.create_supplier_product(other_supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        price = self.create_future_price(other_supplier, other_product, venue)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(own))
        response = self.client.delete(self.detail_url(other_product, price))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(SupplierProductVenuePrice.objects.filter(pk=price.pk).exists())


class SupplierVenueSearchAPITests(APITestCase):
    """2026-09-23, по просьбе пользователя: поиск заведений по ИНН/названию
    для выбора venue_ids перед назначением индивидуальной цены."""

    url = "/api/v1/suppliers/venues/search"

    def create_supplier(self):
        return Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")

    def test_requires_authentication(self):
        response = self.client.get(self.url, {"q": "Кафе"})
        self.assertEqual(response.status_code, 401)

    def test_bare_cabinet_session_is_enough(self):
        """Read-only, same permission (IsSupplierCabinetOrHasServiceToken) as
        the individual-prices GET — no active-membership requirement."""
        supplier = self.create_supplier()
        Venue.objects.create(display_name="Кафе Ромашка", status=EntityStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get(self.url, {"q": "Кафе"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_service_token_allowed(self):
        with self.settings(SERVICE_API_TOKEN="test-token"):
            Venue.objects.create(display_name="Кафе Ромашка", status=EntityStatus.ACTIVE)
            self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
            response = self.client.get(self.url, {"q": "Кафе"})
        self.assertEqual(response.status_code, 200)

    def test_search_by_name(self):
        supplier = self.create_supplier()
        venue = Venue.objects.create(display_name="Кафе Ромашка", status=EntityStatus.ACTIVE)
        Venue.objects.create(display_name="Бар Полночь", status=EntityStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get(self.url, {"q": "Ромашка"})
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(venue.id))

    def test_search_by_legal_name(self):
        supplier = self.create_supplier()
        venue = Venue.objects.create(
            display_name="Точка на районе", legal_name="ООО Ромашка", status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get(self.url, {"q": "Ромашка"})
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(venue.id))

    def test_search_by_inn(self):
        supplier = self.create_supplier()
        venue = Venue.objects.create(display_name="Кафе Ромашка", inn="7701234567", status=EntityStatus.ACTIVE)
        Venue.objects.create(display_name="Бар Полночь", inn="7709876543", status=EntityStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get(self.url, {"q": "770123"})
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(venue.id))

    def test_excludes_inactive_venues(self):
        supplier = self.create_supplier()
        Venue.objects.create(display_name="Кафе Ромашка", status=EntityStatus.INACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get(self.url, {"q": "Ромашка"})
        self.assertEqual(response.json()["count"], 0)

    def test_q_too_short_rejected(self):
        supplier = self.create_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get(self.url, {"q": "К"})
        self.assertEqual(response.status_code, 400)

    def test_pagination(self):
        supplier = self.create_supplier()
        for i in range(3):
            Venue.objects.create(display_name=f"Кафе {i}", status=EntityStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get(self.url, {"q": "Кафе", "limit": "2", "offset": "0"})
        data = response.json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 2)
        response = self.client.get(self.url, {"q": "Кафе", "limit": "2", "offset": "2"})
        self.assertEqual(len(response.json()["results"]), 1)


class SupplierProductIndividualPriceOrderHistoryAPITests(APITestCase):
    """§26-27 ТЗ: постраничная история заявок по одной конкретной
    индивидуальной цене, с фильтрами по заведению/периоду/номеру заявки —
    поверх уже существующего confirmed_qty агрегата на
    SupplierProductVenuePriceSerializer (см. supplier_product_venue_price_confirmed_qty)."""

    def create_supplier(self, display_name="Supplier"):
        return Supplier.objects.create(legal_name=f"{display_name} LLC", display_name=display_name)

    def create_supplier_product(self, supplier=None, price="100.00", name="Сыр Пармезан"):
        supplier = supplier or self.create_supplier()
        return SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name=name,
            normalized_name=name,
            current_price=Decimal(price),
        )

    def url(self, supplier_product, price):
        return f"/api/v1/suppliers/products/{supplier_product.id}/individual-prices/{price.id}/orders"

    def test_requires_authentication(self):
        supplier_product = self.create_supplier_product()
        venue = Venue.objects.create(display_name="Cafe X")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=supplier_product, venue=venue, price=Decimal("305.00")
        )

        response = self.client.get(self.url(supplier_product, price))

        self.assertEqual(response.status_code, 401)

    def test_denied_for_other_suppliers_product(self):
        own = self.create_supplier("Own")
        other_supplier = self.create_supplier("Other")
        other_product = self.create_supplier_product(other_supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=other_product, venue=venue, price=Decimal("305.00")
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(own))

        response = self.client.get(self.url(other_product, price))

        self.assertEqual(response.status_code, 404)

    def test_404_when_price_belongs_to_a_different_product(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        other_product = self.create_supplier_product(supplier, price="50.00", name="Другой товар")
        venue = Venue.objects.create(display_name="Cafe X")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=other_product, venue=venue, price=Decimal("305.00")
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        response = self.client.get(self.url(supplier_product, price))

        self.assertEqual(response.status_code, 404)

    def test_lists_rows_with_amount_and_envelope(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Качели")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=supplier_product, venue=venue, price=Decimal("305.00")
        )
        order = Order.objects.create(venue=venue, order_number="1534")
        OrderItem.objects.create(
            order=order,
            supplier_product=supplier_product,
            requested_qty=Decimal("100"),
            confirmed_qty=Decimal("60"),
            applied_price=Decimal("305.00"),
            applied_price_type="individual",
            applied_individual_price=price,
        )
        # a different price on the same product must not leak into this listing
        other_price = SupplierProductVenuePrice.objects.create(
            supplier_product=supplier_product, venue=venue, price=Decimal("310.00")
        )
        OrderItem.objects.create(
            order=order,
            supplier_product=supplier_product,
            requested_qty=Decimal("10"),
            confirmed_qty=Decimal("10"),
            applied_price=Decimal("310.00"),
            applied_price_type="individual",
            applied_individual_price=other_price,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        response = self.client.get(self.url(supplier_product, price))

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["limit"], 50)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(len(data["results"]), 1)
        row = data["results"][0]
        self.assertEqual(row["order_id"], str(order.id))
        self.assertEqual(row["order_number"], "1534")
        self.assertEqual(row["venue_id"], str(venue.id))
        self.assertEqual(row["venue_name"], "Качели")
        self.assertEqual(row["requested_qty"], "100.0000")
        self.assertEqual(row["confirmed_qty"], "60.0000")
        self.assertEqual(row["amount"], "18300.00")
        self.assertEqual([r["order_id"] for r in data["results"]], [str(order.id)])

    def test_null_confirmed_qty_gives_null_amount(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=supplier_product, venue=venue, price=Decimal("305.00")
        )
        order = Order.objects.create(venue=venue, order_number="9001")
        OrderItem.objects.create(
            order=order,
            supplier_product=supplier_product,
            requested_qty=Decimal("40"),
            confirmed_qty=None,
            applied_price=Decimal("305.00"),
            applied_price_type="individual",
            applied_individual_price=price,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        response = self.client.get(self.url(supplier_product, price))

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertIsNone(row["confirmed_qty"])
        self.assertIsNone(row["amount"])

    def test_amount_serializes_as_quantized_decimal_string(self):
        # Regression: amount is a SerializerMethodField, which bypasses
        # DecimalField.to_representation() — a raw Decimal returned from
        # get_amount() would fall through to DRF's JSON encoder as a bare
        # float with full multiplication precision (e.g. 18357.477722)
        # instead of the documented quoted 2-decimal string. Non-round
        # inputs make that bug visible; round inputs (e.g. 305.00 * 60 =
        # 18300.00) don't, because float(Decimal("18300.00")) == 18300.0
        # looks identical to "18300.00" under a loose float() comparison.
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Cafe X")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=supplier_product, venue=venue, price=Decimal("305.33")
        )
        order = Order.objects.create(venue=venue, order_number="7001")
        OrderItem.objects.create(
            order=order,
            supplier_product=supplier_product,
            requested_qty=Decimal("70"),
            confirmed_qty=Decimal("60.1234"),
            applied_price=Decimal("305.33"),
            applied_price_type="individual",
            applied_individual_price=price,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        response = self.client.get(self.url(supplier_product, price))

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertIsInstance(row["amount"], str)
        self.assertEqual(row["amount"], "18357.48")

    def test_filters_by_venue_id(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Качели")
        other_venue = Venue.objects.create(display_name="Причал")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=supplier_product, venue=venue, price=Decimal("305.00")
        )
        order = Order.objects.create(venue=venue, order_number="1534")
        OrderItem.objects.create(
            order=order,
            supplier_product=supplier_product,
            requested_qty=Decimal("100"),
            confirmed_qty=Decimal("100"),
            applied_individual_price=price,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        matching = self.client.get(self.url(supplier_product, price), {"venue_id": str(venue.id)})
        self.assertEqual(matching.json()["count"], 1)

        non_matching = self.client.get(self.url(supplier_product, price), {"venue_id": str(other_venue.id)})
        self.assertEqual(non_matching.json()["count"], 0)

    def test_filters_by_order_number_substring(self):
        supplier = self.create_supplier()
        supplier_product = self.create_supplier_product(supplier)
        venue = Venue.objects.create(display_name="Качели")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=supplier_product, venue=venue, price=Decimal("305.00")
        )
        order = Order.objects.create(venue=venue, order_number="1534")
        OrderItem.objects.create(
            order=order,
            supplier_product=supplier_product,
            requested_qty=Decimal("100"),
            confirmed_qty=Decimal("100"),
            applied_individual_price=price,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(supplier))

        matching = self.client.get(self.url(supplier_product, price), {"order_number": "153"})
        self.assertEqual(matching.json()["count"], 1)

        non_matching = self.client.get(self.url(supplier_product, price), {"order_number": "9999"})
        self.assertEqual(non_matching.json()["count"], 0)

    def test_available_via_service_token_too(self):
        supplier_product = self.create_supplier_product()
        venue = Venue.objects.create(display_name="Cafe X")
        price = SupplierProductVenuePrice.objects.create(
            supplier_product=supplier_product, venue=venue, price=Decimal("305.00")
        )
        with override_settings(SERVICE_API_TOKEN="test-token"):
            self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
            response = self.client.get(self.url(supplier_product, price))
        self.assertEqual(response.status_code, 200)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierCatalogSearchAPITests(APITestCase):
    url = "/api/v1/suppliers/catalog-search"

    def setUp(self):
        super().setUp()
        # catalog-search кеширует выдачу на 60 с по параметрам запроса: тесты с одним и тем же q не должны делить кеш.
        cache.clear()

    def create_supplier(self, name):
        return Supplier.objects.create(legal_name=f"{name} LLC", display_name=name)

    def create_catalog_product(self, *, supplier, name, price, supplier_sku=None):
        unit = Unit.objects.create(
            code=f"pcs-{Unit.objects.count()}",
            name="штука",
            kind=UnitKind.PIECE,
        )
        product = Product.objects.create(canonical_name=name, normalized_name=name.lower(), base_unit=unit)
        supplier_product = SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=name,
            normalized_name=name.lower(),
            supplier_sku=supplier_sku,
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price=Decimal(price),
            status=PriceStatus.ACTIVE,
            valid_from=timezone.now(),
        )
        return supplier_product

    def test_requires_supplier_authentication(self):
        response = self.client.get(self.url, {"q": "apple"})

        self.assertEqual(response.status_code, 401)

    def test_requires_non_blank_query(self):
        supplier = self.create_supplier("Searching supplier")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 400)
        self.assertIn("q", response.json())

    # Совпадение по названию ("Green apple" по запросу "apple") отдаётся отдельным SQL-путём (_exact_name_results) и
    # векторный поиск не вызывает вовсе. Чтобы проверить слияние текстового и векторного результатов, текстовый
    # кандидат находится по артикулу, а не по названию. Порог пропуска вектора поднят, чтобы сильное совпадение
    # по артикулу не отключало вектор; пропуск вектора проверяет тест ниже.
    @override_settings(CATALOG_VECTOR_SKIP_MIN_LEXICAL_SCORE=1000)
    @patch("api.v1.catalog.search.vector_supplier_product_scores")
    def test_searches_all_suppliers_with_catalog_hybrid_pipeline(self, vector_scores_mock):
        searching_supplier = self.create_supplier("Searching supplier")
        lexical_supplier = self.create_supplier("Lexical supplier")
        vector_supplier = self.create_supplier("Vector supplier")
        lexical_product = self.create_catalog_product(
            supplier=lexical_supplier,
            name="Fresh fruit",
            price="120.00",
            supplier_sku="apple",
        )
        vector_product = self.create_catalog_product(
            supplier=vector_supplier,
            name="Industrial bolt",
            price="80.00",
        )
        vector_scores_mock.return_value = {str(vector_product.id): 0.91}
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(searching_supplier))

        response = self.client.get(self.url, {"q": "apple"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 2)
        result_ids = {item["id"] for item in response.json()["results"]}
        self.assertEqual(result_ids, {str(lexical_product.id), str(vector_product.id)})
        self.assertEqual(
            {item["supplier_id"] for item in response.json()["results"]},
            {str(lexical_supplier.id), str(vector_supplier.id)},
        )
        vector_scores_mock.assert_called_once_with("apple", limit=settings.CATALOG_VECTOR_SEARCH_TOP_K)

    @patch("api.v1.catalog.search.vector_supplier_product_scores")
    def test_strong_lexical_match_skips_vector_search(self, vector_scores_mock):
        searching_supplier = self.create_supplier("Searching supplier")
        lexical_supplier = self.create_supplier("Lexical supplier")
        vector_supplier = self.create_supplier("Vector supplier")
        lexical_product = self.create_catalog_product(supplier=lexical_supplier, name="Green apple", price="120.00")
        vector_product = self.create_catalog_product(supplier=vector_supplier, name="Industrial bolt", price="80.00")
        vector_scores_mock.return_value = {str(vector_product.id): 0.91}
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(searching_supplier))

        response = self.client.get(self.url, {"q": "apple"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual([item["id"] for item in response.json()["results"]], [str(lexical_product.id)])
        vector_scores_mock.assert_not_called()

    # Ключ поиска по названию приравнивает латиницу и кириллицу (латинская "a" == русская "а"), поэтому однобуквенный
    # хвост артикула ("A") совпал бы с названием "OEM 42 аналог" и артикульный кандидат не попал бы в выдачу.
    # Артикул с хвостом "AB" такого пересечения не даёт.
    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_exact_supplier_sku_has_priority_and_match_explanation(self, _vector_scores_mock):
        searching_supplier = self.create_supplier("Searching supplier")
        catalog_supplier = self.create_supplier("Catalog supplier")
        exact = self.create_catalog_product(
            supplier=catalog_supplier,
            name="Непохожее название",
            price="150.00",
            supplier_sku="OEM-42-AB",
        )
        self.create_catalog_product(supplier=catalog_supplier, name="OEM 42 аналог", price="100.00")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(searching_supplier))

        response = self.client.get(self.url, {"q": "OEM-42-AB"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(exact.id))
        self.assertEqual(response.json()["results"][0]["match"]["reason"], "exact_sku")
        self.assertEqual(response.json()["results"][0]["supplier_sku"], "OEM-42-AB")

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_corrects_english_keyboard_layout_for_russian_query(self, _vector_scores_mock):
        searching_supplier = self.create_supplier("Searching supplier")
        catalog_supplier = self.create_supplier("Catalog supplier")
        milk = self.create_catalog_product(supplier=catalog_supplier, name="молоко", price="90.00")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(searching_supplier))

        response = self.client.get(self.url, {"q": "vjkjrj"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(milk.id))
        self.assertEqual(response.json()["results"][0]["match"]["keyboard_corrected_query"], "молоко")

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_groups_same_product_offers_and_records_analytics(self, _vector_scores_mock):
        searching_supplier = self.create_supplier("Searching supplier")
        first_supplier = self.create_supplier("First supplier")
        second_supplier = self.create_supplier("Second supplier")
        self.create_catalog_product(supplier=first_supplier, name="Сахар", price="110.00")
        self.create_catalog_product(supplier=second_supplier, name="Сахар", price="95.00")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(searching_supplier))

        response = self.client.get(self.url, {"q": "сахар", "group_offers": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["group_count"], 1)
        self.assertEqual(len(response.json()["groups"][0]["offers"]), 2)
        self.assertEqual(response.json()["groups"][0]["best_price"], "95.00")
        search_event = WorkflowEvent.objects.get(event_type="supplier_catalog_search")
        self.assertEqual(search_event.payload_json["outcome"], "results_found")

        event_response = self.client.post(
            "/api/v1/suppliers/catalog-search/events",
            {"search_id": response.json()["search_id"], "event_type": "click", "position": 1},
            format="json",
        )
        self.assertEqual(event_response.status_code, 201)
        self.assertTrue(WorkflowEvent.objects.filter(event_type="supplier_catalog_search_click").exists())

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_returns_search_suggestions(self, _vector_scores_mock):
        searching_supplier = self.create_supplier("Searching supplier")
        catalog_supplier = self.create_supplier("Catalog supplier")
        self.create_catalog_product(supplier=catalog_supplier, name="Молоко пастеризованное", price="90.00")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(searching_supplier))

        response = self.client.get(f"{self.url}/suggestions", {"q": "мол"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["suggestions"][0]["label"], "молоко пастеризованное")


class FindCandidateCatalogProductsTests(TestCase):
    """TASK-87: кандидаты для автозаполнения карточки товара поставщика —
    дедуп до уникальных catalog.Product поверх уже существующего hybrid
    lexical/vector поиска (тот же пайплайн, что у SupplierCatalogSearchView)."""

    def create_supplier(self, name):
        return Supplier.objects.create(legal_name=f"{name} LLC", display_name=name)

    def create_catalog_product(self, *, supplier, name, price="100.00"):
        unit = Unit.objects.create(
            code=f"pcs-{Unit.objects.count()}-{uuid4().hex[:6]}", name="штука", kind=UnitKind.PIECE
        )
        product = Product.objects.create(canonical_name=name, normalized_name=name.lower(), base_unit=unit)
        supplier_product = SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=name,
            normalized_name=name.lower(),
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price=Decimal(price),
            status=PriceStatus.ACTIVE,
            valid_from=timezone.now(),
        )
        return product

    def test_returns_empty_list_for_blank_name(self):
        self.assertEqual(find_candidate_catalog_products(name=""), [])
        self.assertEqual(find_candidate_catalog_products(name="   "), [])

    def test_finds_matching_product_by_name(self):
        supplier = self.create_supplier("Supplier A")
        product = self.create_catalog_product(supplier=supplier, name="Молоко пастеризованное 3.2%")

        candidates = find_candidate_catalog_products(name="молоко")

        self.assertIn(product.id, [c.id for c in candidates])

    def test_dedupes_same_product_offered_by_several_suppliers(self):
        first_supplier = self.create_supplier("Supplier B")
        second_supplier = self.create_supplier("Supplier C")
        unit = Unit.objects.create(code=f"pcs-dedup-{uuid4().hex[:6]}", name="штука", kind=UnitKind.PIECE)
        product = Product.objects.create(
            canonical_name="Сахар песок 1кг", normalized_name="сахар песок 1кг", base_unit=unit
        )
        for supplier in (first_supplier, second_supplier):
            supplier_product = SupplierProduct.objects.create(
                supplier=supplier,
                product=product,
                unit=unit,
                supplier_name="Сахар песок 1кг",
                normalized_name="сахар песок 1кг",
                status=EntityStatus.ACTIVE,
            )
            SupplierProductPrice.objects.create(
                supplier_product=supplier_product,
                price=Decimal("70.00"),
                status=PriceStatus.ACTIVE,
                valid_from=timezone.now(),
            )

        candidates = find_candidate_catalog_products(name="сахар песок")

        matching = [c for c in candidates if c.id == product.id]
        self.assertEqual(len(matching), 1)

    def test_respects_limit(self):
        supplier = self.create_supplier("Supplier D")
        for i in range(5):
            self.create_catalog_product(supplier=supplier, name=f"Тестовый товар автозаполнения {i}")

        candidates = find_candidate_catalog_products(name="Тестовый товар автозаполнения", limit=2)

        self.assertLessEqual(len(candidates), 2)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductUpdateAPITests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier(self, display_name="Supplier"):
        return Supplier.objects.create(
            legal_name=f"{display_name} LLC",
            display_name=display_name,
        )

    def create_unit(self, code="kg"):
        return Unit.objects.create(
            code=code,
            name=code,
            kind=UnitKind.WEIGHT,
        )

    def create_supplier_product(self):
        supplier = self.create_supplier()
        unit = self.create_unit(f"kg-{Unit.objects.count()}")
        product = Product.objects.create(
            canonical_name="Canonical milk",
            normalized_name="milk",
            base_unit=unit,
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name="Milk raw",
            normalized_name="milk",
            package="box",
            min_order_qty=Decimal("1.0000"),
            status=EntityStatus.ACTIVE,
        )

    def detail_url(self, supplier_product):
        return f"/api/v1/suppliers/products/{supplier_product.id}"

    def test_rejects_patch_without_service_token(self):
        supplier_product = self.create_supplier_product()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {"supplier_raw_name": "Updated"},
            format="json",
        )

        self.assertEqual(response.status_code, 401)

    def test_allows_patch_with_valid_service_token(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {"comment": "operator update"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(supplier_product.id))

    def test_patch_brand_free_text_creates_it_if_missing(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        self.assertFalse(Brand.objects.filter(normalized_name="свежий текстовый бренд").exists())

        response = self.client.patch(
            self.detail_url(supplier_product),
            {"brand": "Свежий Текстовый Бренд"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["brand"], "Свежий Текстовый Бренд")
        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.brand.normalized_name, "свежий текстовый бренд")

    def test_patch_brand_id_attaches_existing_brand(self):
        """2026-08-28, Vitya: GET /suppliers/brands (автокомплит) не имело
        парного поля для отправки назад выбранного существующего бренда."""
        supplier_product = self.create_supplier_product()
        brand = Brand.objects.create(
            canonical_name="Тестовая Паста Бренд XYZ", normalized_name="тестовая паста бренд xyz"
        )
        self.authenticate()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {"brand_id": str(brand.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["brand"], "Тестовая Паста Бренд XYZ")
        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.brand_id, brand.id)

    def test_supplier_cabinet_jwt_can_patch_own_product(self):
        supplier_product = self.create_supplier_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier_product.supplier))
        response = self.client.patch(
            self.detail_url(supplier_product),
            {"comment": "self-service update"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)

    def test_supplier_cabinet_jwt_cannot_patch_other_suppliers_product(self):
        own_product = self.create_supplier_product()
        other_product = self.create_supplier_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_product.supplier))
        response = self.client.patch(
            self.detail_url(other_product),
            {"comment": "should not apply"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        other_product.refresh_from_db()
        self.assertNotEqual(other_product.comment, "should not apply")

    def test_updates_names_and_product_fields(self):
        supplier_product = self.create_supplier_product()
        new_unit = self.create_unit("pcs")
        self.authenticate()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {
                "supplier_raw_name": "Milk raw updated",
                "supplier_normalized_name": "milk normalized updated",
                "unit_id": str(new_unit.id),
                "package": "crate",
                "min_order_qty": "3.2500",
                "status": EntityStatus.INACTIVE,
                "comment": "checked",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.supplier_name, "Milk raw updated")
        self.assertEqual(supplier_product.normalized_name, "milk normalized updated")
        self.assertEqual(supplier_product.unit, new_unit)
        self.assertEqual(supplier_product.package, "crate")
        self.assertEqual(supplier_product.min_order_qty, Decimal("3.2500"))
        self.assertEqual(supplier_product.status, EntityStatus.INACTIVE)
        self.assertEqual(supplier_product.comment, "checked")
        self.assertEqual(response.json()["unit"], "pcs")

    def test_updates_country_of_origin_and_net_value(self):
        """2026-08-09: Витя — эти поля можно было задать только при создании
        (POST /suppliers/products), в PATCH их не было вообще."""
        supplier_product = self.create_supplier_product()
        net_unit = Unit.objects.create(code="kg-patch", name="kg-patch", kind=UnitKind.WEIGHT)
        self.authenticate()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {
                "country_of_origin": "Италия",
                "net_value": "0.9000",
                "net_unit_id": str(net_unit.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.country_of_origin, "Италия")
        self.assertEqual(supplier_product.net_value, Decimal("0.9000"))
        self.assertEqual(supplier_product.net_unit, net_unit)
        self.assertEqual(response.json()["country_of_origin"], "Италия")
        self.assertEqual(response.json()["net_unit"], "kg-patch")

    def test_price_update_creates_new_active_price(self):
        supplier_product = self.create_supplier_product()
        old_price = SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price=Decimal("100.00"),
            status=PriceStatus.ACTIVE,
            valid_from=timezone.now() - timezone.timedelta(days=1),
        )
        self.authenticate()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {"price": "125.50"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current_price"], "125.50")
        active_prices = SupplierProductPrice.objects.filter(
            supplier_product=supplier_product,
            status=PriceStatus.ACTIVE,
            valid_to__isnull=True,
        )
        self.assertEqual(active_prices.count(), 1)
        self.assertEqual(active_prices.get().price, Decimal("125.50"))
        old_price.refresh_from_db()
        self.assertEqual(old_price.status, PriceStatus.SUPERSEDED)
        self.assertIsNotNone(old_price.valid_to)

    def test_same_price_update_does_not_duplicate_active_price(self):
        supplier_product = self.create_supplier_product()
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price=Decimal("125.50"),
            status=PriceStatus.ACTIVE,
            valid_from=timezone.now() - timezone.timedelta(days=1),
        )
        self.authenticate()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {"price": "125.50"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current_price"], "125.50")
        self.assertEqual(SupplierProductPrice.objects.filter(supplier_product=supplier_product).count(), 1)

    def test_rejects_negative_price(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {"price": "-1.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SupplierProductPrice.objects.filter(supplier_product=supplier_product).exists())

    def test_rejects_unknown_unit(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {"unit_id": str(uuid4())},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_updates_brand(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()

        response = self.client.patch(
            self.detail_url(supplier_product),
            {"brand": "Молочный завод"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["brand"], "Молочный завод")
        supplier_product.refresh_from_db()
        self.assertIsNotNone(supplier_product.brand)
        self.assertEqual(supplier_product.brand.canonical_name, "Молочный завод")

    def test_clears_brand_with_empty_string(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        # First set a brand
        self.client.patch(self.detail_url(supplier_product), {"brand": "Бренд А"}, format="json")
        # Then clear it
        response = self.client.patch(self.detail_url(supplier_product), {"brand": ""}, format="json")
        self.assertEqual(response.status_code, 200)
        supplier_product.refresh_from_db()
        self.assertIsNone(supplier_product.brand)

    def test_patch_sets_and_clears_short_description(self):
        supplier = self.create_supplier()
        product = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Сливки 33%", normalized_name="Сливки 33%"
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.patch(
            f"/api/v1/suppliers/products/{product.id}",
            {"short_description": "Сливки для профессиональной кухни."},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["short_description"], "Сливки для профессиональной кухни.")
        product.refresh_from_db()
        self.assertEqual(product.short_description, "Сливки для профессиональной кухни.")

        response = self.client.patch(
            f"/api/v1/suppliers/products/{product.id}", {"short_description": ""}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertIsNone(product.short_description)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductArchiveAPITests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self):
        supplier = Supplier.objects.create(
            legal_name="Supplier LLC",
            display_name="Supplier",
        )
        unit = Unit.objects.create(
            code=f"kg-{Unit.objects.count()}",
            name="kg",
            kind=UnitKind.WEIGHT,
        )
        product = Product.objects.create(
            canonical_name="Canonical flour",
            normalized_name="flour",
            base_unit=unit,
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name="Flour raw",
            normalized_name="flour",
            package="bag",
            min_order_qty=Decimal("1.0000"),
            status=EntityStatus.ACTIVE,
        )

    def archive_url(self, supplier_product):
        return f"/api/v1/suppliers/products/{supplier_product.id}/archive"

    def test_rejects_archive_without_service_token(self):
        supplier_product = self.create_supplier_product()

        response = self.client.post(self.archive_url(supplier_product))

        self.assertEqual(response.status_code, 401)

    def test_archives_product_with_valid_service_token(self):
        supplier_product = self.create_supplier_product()
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price=Decimal("10.00"),
            status=PriceStatus.ACTIVE,
        )
        self.authenticate()

        response = self.client.post(self.archive_url(supplier_product))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], EntityStatus.ARCHIVED)
        self.assertIsNone(response.json()["current_price"])
        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.status, EntityStatus.ARCHIVED)
        closed_price = SupplierProductPrice.objects.get(supplier_product=supplier_product)
        self.assertEqual(closed_price.status, PriceStatus.CANCELLED)
        self.assertIsNotNone(closed_price.valid_to)

    def test_supplier_cabinet_jwt_can_archive_own_product(self):
        supplier_product = self.create_supplier_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier_product.supplier))
        response = self.client.post(self.archive_url(supplier_product))

        self.assertEqual(response.status_code, 200)

    def test_supplier_cabinet_jwt_cannot_archive_other_suppliers_product(self):
        own_product = self.create_supplier_product()
        other_product = self.create_supplier_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_product.supplier))
        response = self.client.post(self.archive_url(other_product))

        self.assertEqual(response.status_code, 404)
        other_product.refresh_from_db()
        self.assertEqual(other_product.status, EntityStatus.ACTIVE)

    def test_archive_is_idempotent(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()

        first_response = self.client.post(self.archive_url(supplier_product))
        second_response = self.client.post(self.archive_url(supplier_product))

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.json()["status"], EntityStatus.ARCHIVED)

    def test_archived_product_is_excluded_from_active_filter(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()

        archive_response = self.client.post(self.archive_url(supplier_product))
        list_response = self.client.get("/api/v1/suppliers/products", {"status": EntityStatus.ACTIVE})

        self.assertEqual(archive_response.status_code, 200)
        self.assertEqual(list_response.status_code, 200)
        result_ids = {item["id"] for item in list_response.json()["results"]}
        self.assertNotIn(str(supplier_product.id), result_ids)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductPublishAPITests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_draft_product(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        return SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Draft product",
            normalized_name="draft product",
            status=EntityStatus.DRAFT,
        )

    def publish_url(self, supplier_product):
        return f"/api/v1/suppliers/products/{supplier_product.id}/publish"

    def test_rejects_publish_without_service_token(self):
        supplier_product = self.create_draft_product()

        response = self.client.post(self.publish_url(supplier_product))

        self.assertEqual(response.status_code, 401)

    def test_publishes_draft_product_with_valid_service_token(self):
        supplier_product = self.create_draft_product()
        self.authenticate()

        response = self.client.post(self.publish_url(supplier_product))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], EntityStatus.ACTIVE)
        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.status, EntityStatus.ACTIVE)
        publish_log = AuditLog.objects.get(
            entity_type=AuditLogEntityType.SUPPLIER_PRODUCT,
            entity_id=supplier_product.id,
            action=AuditLogAction.PUBLISHED,
        )
        self.assertEqual(publish_log.actor_type, AuditActorType.SERVICE)

    def test_supplier_cabinet_jwt_can_publish_own_product(self):
        supplier_product = self.create_draft_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier_product.supplier))
        response = self.client.post(self.publish_url(supplier_product))

        self.assertEqual(response.status_code, 200)

    def test_supplier_cabinet_jwt_cannot_publish_other_suppliers_product(self):
        own_product = self.create_draft_product()
        other_product = self.create_draft_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_product.supplier))
        response = self.client.post(self.publish_url(other_product))

        self.assertEqual(response.status_code, 404)
        other_product.refresh_from_db()
        self.assertEqual(other_product.status, EntityStatus.DRAFT)

    def test_publish_is_idempotent_on_already_active_product(self):
        supplier_product = self.create_draft_product()
        self.authenticate()
        self.client.post(self.publish_url(supplier_product))

        second_response = self.client.post(self.publish_url(supplier_product))

        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(
            AuditLog.objects.filter(
                entity_type=AuditLogEntityType.SUPPLIER_PRODUCT,
                entity_id=supplier_product.id,
                action=AuditLogAction.PUBLISHED,
            ).count(),
            1,
        )

    def test_published_product_appears_in_buyer_catalog(self):
        from api.v1.catalog.selectors import catalog_products_queryset

        supplier_product = self.create_draft_product()
        supplier_product.supplier.is_published = True
        supplier_product.supplier.save(update_fields=["is_published"])
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product, price=Decimal("10.00"), status=PriceStatus.ACTIVE
        )
        supplier_product.current_price = Decimal("10.00")
        supplier_product.save(update_fields=["current_price"])
        self.authenticate()

        before_ids = set(catalog_products_queryset({}).values_list("id", flat=True))
        self.client.post(self.publish_url(supplier_product))
        after_ids = set(catalog_products_queryset({}).values_list("id", flat=True))

        self.assertNotIn(supplier_product.id, before_ids)
        self.assertIn(supplier_product.id, after_ids)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductBulkArchiveAPITests(APITestCase):
    """2026-08-02: Витя — таблица товаров во фронте не нашла ни одной ручки
    для мультивыбора (архивировать/изменить несколько сразу). Bulk-версии
    одиночных archive/update, тот же IDOR-скоупинг, всё-или-ничего."""

    url = "/api/v1/suppliers/products/bulk-archive"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier=None):
        supplier = supplier or Supplier.objects.create(legal_name="Bulk Supplier LLC", display_name="Bulk Supplier")
        unit = Unit.objects.create(code=f"kg-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        n = Unit.objects.count()
        product = Product.objects.create(
            canonical_name=f"Canonical bulk {n}", normalized_name=f"bulk-{n}", base_unit=unit
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=f"Bulk raw {n}",
            normalized_name=f"bulk-{n}",
            status=EntityStatus.ACTIVE,
        )

    def test_rejects_without_service_token(self):
        product = self.create_supplier_product()
        response = self.client.post(self.url, {"ids": [str(product.id)]}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_archives_multiple_products_with_service_token(self):
        supplier = Supplier.objects.create(legal_name="Bulk Supplier LLC", display_name="Bulk Supplier")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        SupplierProductPrice.objects.create(
            supplier_product=product_a, price=Decimal("10.00"), status=PriceStatus.ACTIVE
        )
        self.authenticate()

        response = self.client.post(self.url, {"ids": [str(product_a.id), str(product_b.id)]}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.status, EntityStatus.ARCHIVED)
        self.assertEqual(product_b.status, EntityStatus.ARCHIVED)
        self.assertEqual(SupplierProductPrice.objects.get(supplier_product=product_a).status, PriceStatus.CANCELLED)

    def test_supplier_cabinet_jwt_can_bulk_archive_own_products(self):
        supplier = Supplier.objects.create(legal_name="Own LLC", display_name="Own")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.post(self.url, {"ids": [str(product_a.id), str(product_b.id)]}, format="json")

        self.assertEqual(response.status_code, 200)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.status, EntityStatus.ARCHIVED)
        self.assertEqual(product_b.status, EntityStatus.ARCHIVED)

    def test_supplier_cabinet_jwt_all_or_nothing_across_suppliers(self):
        """Один чужой id в списке — весь batch отклоняется, ни один товар
        (включая свой) не архивируется."""
        own_supplier = Supplier.objects.create(legal_name="Own LLC 2", display_name="Own 2")
        own_product = self.create_supplier_product(own_supplier)
        other_product = self.create_supplier_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_supplier))
        response = self.client.post(self.url, {"ids": [str(own_product.id), str(other_product.id)]}, format="json")

        self.assertEqual(response.status_code, 404)
        own_product.refresh_from_db()
        other_product.refresh_from_db()
        self.assertEqual(own_product.status, EntityStatus.ACTIVE)
        self.assertEqual(other_product.status, EntityStatus.ACTIVE)

    def test_missing_id_rejects_whole_batch(self):
        product = self.create_supplier_product()
        self.authenticate()

        response = self.client.post(self.url, {"ids": [str(product.id), str(uuid4())]}, format="json")

        self.assertEqual(response.status_code, 404)
        product.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ACTIVE)

    def test_empty_ids_rejected(self):
        self.authenticate()
        response = self.client.post(self.url, {"ids": []}, format="json")
        self.assertEqual(response.status_code, 400)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductBulkPublishAPITests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_draft_product(self, supplier=None):
        supplier = supplier or Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        return SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name=f"Draft {SupplierProduct.objects.count()}",
            normalized_name="draft",
            status=EntityStatus.DRAFT,
        )

    def test_publishes_multiple_products_with_service_token(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        first = self.create_draft_product(supplier)
        second = self.create_draft_product(supplier)
        self.authenticate()

        response = self.client.post(
            "/api/v1/suppliers/products/bulk-publish", {"ids": [str(first.id), str(second.id)]}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, EntityStatus.ACTIVE)
        self.assertEqual(second.status, EntityStatus.ACTIVE)

    def test_supplier_cabinet_jwt_can_bulk_publish_own_products(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        product = self.create_draft_product(supplier)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.post(
            "/api/v1/suppliers/products/bulk-publish", {"ids": [str(product.id)]}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ACTIVE)

    def test_bulk_publish_rejects_batch_with_foreign_product(self):
        own = self.create_draft_product()
        foreign = self.create_draft_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own.supplier))
        response = self.client.post(
            "/api/v1/suppliers/products/bulk-publish", {"ids": [str(own.id), str(foreign.id)]}, format="json"
        )

        self.assertEqual(response.status_code, 404)
        own.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(own.status, EntityStatus.DRAFT)
        self.assertEqual(foreign.status, EntityStatus.DRAFT)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductArchiveAllAPITests(APITestCase):
    """2026-08-04: "Мои компании" — Витя нужна ручка archiveAll на товары
    компании прямо со списка компаний, без захода в таблицу товаров."""

    url = "/api/v1/suppliers/products/archive-all"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier, status=EntityStatus.ACTIVE):
        unit = Unit.objects.create(code=f"kg-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        n = Unit.objects.count()
        product = Product.objects.create(
            canonical_name=f"Canonical archive-all {n}", normalized_name=f"archive-all-{n}", base_unit=unit
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=f"Archive-all raw {n}",
            normalized_name=f"archive-all-{n}",
            status=status,
        )

    def test_rejects_without_service_token(self):
        supplier = Supplier.objects.create(legal_name="AA LLC", display_name="AA")
        response = self.client.post(self.url, {"supplier_id": str(supplier.id)}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_service_token_requires_supplier_id(self):
        self.authenticate()
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_service_token_archives_all_active_products(self):
        supplier = Supplier.objects.create(legal_name="AA LLC 2", display_name="AA 2")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        already_archived = self.create_supplier_product(supplier, status=EntityStatus.ARCHIVED)
        SupplierProductPrice.objects.create(
            supplier_product=product_a, price=Decimal("10.00"), status=PriceStatus.ACTIVE
        )
        self.authenticate()

        response = self.client.post(self.url, {"supplier_id": str(supplier.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["archived_count"], 2)
        self.assertEqual(str(response.data["supplier_id"]), str(supplier.id))
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        already_archived.refresh_from_db()
        self.assertEqual(product_a.status, EntityStatus.ARCHIVED)
        self.assertEqual(product_b.status, EntityStatus.ARCHIVED)
        self.assertEqual(already_archived.status, EntityStatus.ARCHIVED)
        self.assertEqual(SupplierProductPrice.objects.get(supplier_product=product_a).status, PriceStatus.CANCELLED)

    def test_service_token_404_for_unknown_supplier(self):
        self.authenticate()
        response = self.client.post(self.url, {"supplier_id": str(uuid4())}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_cabinet_jwt_archives_only_own_products_ignoring_supplier_id(self):
        own_supplier = Supplier.objects.create(legal_name="AA Own LLC", display_name="AA Own")
        other_supplier = Supplier.objects.create(legal_name="AA Other LLC", display_name="AA Other")
        own_product = self.create_supplier_product(own_supplier)
        other_product = self.create_supplier_product(other_supplier)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_supplier))
        response = self.client.post(self.url, {"supplier_id": str(other_supplier.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.data["supplier_id"]), str(own_supplier.id))
        self.assertEqual(response.data["archived_count"], 1)
        own_product.refresh_from_db()
        other_product.refresh_from_db()
        self.assertEqual(own_product.status, EntityStatus.ARCHIVED)
        self.assertEqual(other_product.status, EntityStatus.ACTIVE)

    def test_no_active_products_returns_zero(self):
        supplier = Supplier.objects.create(legal_name="AA Empty LLC", display_name="AA Empty")
        self.authenticate()

        response = self.client.post(self.url, {"supplier_id": str(supplier.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["archived_count"], 0)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductPriceSnapshotSyncTests(APITestCase):
    """2026-08-10: current_price/previous_price/price_changed_at перестали быть
    Subquery-аннотациями (сортировка по ним не могла использовать индекс —
    Postgres приходилось считать подзапрос на каждой строке перед сортировкой,
    см. docs/wiki/current-status.md) и стали обычными денормализованными
    колонками на SupplierProduct. Держит их в актуальном состоянии
    suppliers/signals.py (post_save на SupplierProductPrice, покрывает даже
    прямой ORM .create() в обход сервисного слоя — как ниже) + явные вызовы
    sync_supplier_product_price_snapshot() в api/v1/suppliers/services.py
    после bulk .update() статусов (сигнал на .update() не срабатывает)."""

    def create_supplier_product(self):
        supplier = Supplier.objects.create(legal_name="Snapshot LLC", display_name="Snapshot")
        unit = Unit.objects.create(code=f"kg-snap-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        return SupplierProduct.objects.create(
            supplier=supplier,
            unit=unit,
            supplier_name="Snapshot raw",
            normalized_name="snapshot",
            status=EntityStatus.ACTIVE,
        )

    def test_direct_price_create_outside_service_layer_syncs_via_signal(self):
        product = self.create_supplier_product()
        self.assertIsNone(product.current_price)

        SupplierProductPrice.objects.create(supplier_product=product, price=Decimal("77.00"), status=PriceStatus.ACTIVE)

        product.refresh_from_db()
        self.assertEqual(product.current_price, Decimal("77.00"))

    def test_archive_clears_current_price_but_keeps_previous_price(self):
        """2026-08-11: раньше проверялось через DELETE (тогда алиас archive) —
        с тех пор DELETE стал реальным удалением, archive теперь достижим
        только через POST .../archive (не тронут)."""
        product = self.create_supplier_product()
        SupplierProductPrice.objects.create(
            supplier_product=product,
            price=Decimal("50.00"),
            status=PriceStatus.SUPERSEDED,
            valid_from=timezone.now() - timezone.timedelta(days=2),
            valid_to=timezone.now() - timezone.timedelta(days=1),
        )
        SupplierProductPrice.objects.create(supplier_product=product, price=Decimal("60.00"), status=PriceStatus.ACTIVE)
        product.refresh_from_db()
        self.assertEqual(product.current_price, Decimal("60.00"))
        self.assertEqual(product.previous_price, Decimal("50.00"))

        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.post(f"/api/v1/suppliers/products/{product.id}/archive")

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertIsNone(product.current_price)
        self.assertEqual(product.previous_price, Decimal("50.00"))


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductDeleteHTTPMethodAPITests(APITestCase):
    """2026-08-10: Витя — "delete/bulk-delete/delete-all тоже нету у товаров
    поставщика". Изначально сделано мягким удалением (archive), т.к.
    SupplierProductPrice ссылалась на товар через on_delete=RESTRICT —
    реальный DELETE падал бы с ошибкой БД у любого товара с историей цен.

    2026-08-11: Витя прислал скриншот Swagger — "не должно быть архивации",
    хочет реальное удаление. FK на SupplierProductPrice/VenueSupplierProduct/
    WishlistItem переведены RESTRICT -> CASCADE (suppliers/0038, catalog/0006)
    — DELETE стал реально удалять строку, вместе с историей цен и
    избранным/списками покупок.

    2026-09-23: решение от 2026-08-11 отменено (ТЗ «Карточка товара
    поставщика» §32 требует, чтобы удаление сохраняло историю цен/заявок —
    backlog#task-69). DELETE снова полностью совпадает с POST .../archive:
    товар не удаляется физически, переводится в ARCHIVED, POST .../restore
    его возвращает. FK обратно RESTRICT (suppliers/0044, catalog/0029) —
    прямой ORM .delete() на товаре с историей цен/избранного теперь тоже
    падает на уровне БД, не только через сервисный слой."""

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier=None):
        supplier = supplier or Supplier.objects.create(legal_name="Delete Supplier LLC", display_name="Delete Supplier")
        unit = Unit.objects.create(code=f"kg-del-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        return SupplierProduct.objects.create(
            supplier=supplier,
            unit=unit,
            supplier_name=f"Delete raw {Unit.objects.count()}",
            normalized_name=f"delete-{Unit.objects.count()}",
            status=EntityStatus.ACTIVE,
        )

    def test_delete_single_archives_product_and_returns_204(self):
        product = self.create_supplier_product()
        self.authenticate()

        response = self.client.delete(f"/api/v1/suppliers/products/{product.id}")

        self.assertEqual(response.status_code, 204)
        product.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ARCHIVED)

    def test_delete_single_preserves_price_history_and_favorites(self):
        product = self.create_supplier_product()
        price = SupplierProductPrice.objects.create(
            supplier_product=product, price=Decimal("10.00"), status=PriceStatus.ACTIVE
        )
        venue = Venue.objects.create(display_name="Delete Cascade Venue")
        favorite = VenueSupplierProduct.objects.create(venue=venue, supplier_product=product, is_favorite=True)
        self.authenticate()

        response = self.client.delete(f"/api/v1/suppliers/products/{product.id}")

        self.assertEqual(response.status_code, 204)
        self.assertTrue(SupplierProductPrice.objects.filter(pk=price.id).exists())
        self.assertTrue(VenueSupplierProduct.objects.filter(pk=favorite.id).exists())

    def test_delete_single_scoped_to_own_supplier(self):
        own = Supplier.objects.create(legal_name="Delete Own LLC", display_name="Delete Own")
        other_product = self.create_supplier_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.delete(f"/api/v1/suppliers/products/{other_product.id}")

        self.assertEqual(response.status_code, 404)
        other_product.refresh_from_db()
        self.assertEqual(other_product.status, EntityStatus.ACTIVE)

    def test_delete_bulk_archives_products(self):
        supplier = Supplier.objects.create(legal_name="Delete Bulk LLC", display_name="Delete Bulk")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        self.authenticate()

        response = self.client.delete(
            "/api/v1/suppliers/products/bulk", {"ids": [str(product_a.id), str(product_b.id)]}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["deleted_count"], 2)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.status, EntityStatus.ARCHIVED)
        self.assertEqual(product_b.status, EntityStatus.ARCHIVED)

    def test_delete_all_archives_only_active_products(self):
        supplier = Supplier.objects.create(legal_name="Delete All LLC", display_name="Delete All")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        product_b.status = EntityStatus.ARCHIVED
        product_b.save(update_fields=["status"])
        self.authenticate()

        response = self.client.delete("/api/v1/suppliers/products", {"supplier_id": str(supplier.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["deleted_count"], 1)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.status, EntityStatus.ARCHIVED)
        self.assertEqual(product_b.status, EntityStatus.ARCHIVED)

    def test_delete_all_cabinet_jwt_ignores_body_supplier_id(self):
        own = Supplier.objects.create(legal_name="Delete All Own LLC", display_name="Delete All Own")
        other = Supplier.objects.create(legal_name="Delete All Other LLC", display_name="Delete All Other")
        own_product = self.create_supplier_product(own)
        other_product = self.create_supplier_product(other)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.delete("/api/v1/suppliers/products", {"supplier_id": str(other.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        own_product.refresh_from_db()
        other_product.refresh_from_db()
        self.assertEqual(own_product.status, EntityStatus.ARCHIVED)
        self.assertEqual(other_product.status, EntityStatus.ACTIVE)

    def test_direct_orm_delete_restricted_while_price_history_exists(self):
        product = self.create_supplier_product()
        SupplierProductPrice.objects.create(supplier_product=product, price=Decimal("10.00"), status=PriceStatus.ACTIVE)

        with self.assertRaises(RestrictedError):
            product.delete()

    def test_delete_already_archived_product_is_idempotent(self):
        product = self.create_supplier_product()
        product.status = EntityStatus.ARCHIVED
        product.save(update_fields=["status"])
        self.authenticate()

        response = self.client.delete(f"/api/v1/suppliers/products/{product.id}")

        self.assertEqual(response.status_code, 204)
        product.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ARCHIVED)

    def test_delete_nonexistent_product_returns_404(self):
        self.authenticate()

        response = self.client.delete(f"/api/v1/suppliers/products/{uuid4()}")

        self.assertEqual(response.status_code, 404)

    def test_restore_after_delete_full_round_trip(self):
        """2026-09-23: DELETE снова полностью совпадает с archive — значит
        POST .../restore должен уметь отменить именно DELETE, не только
        обычный archive, включая возврат отменённой цены."""
        product = self.create_supplier_product()
        SupplierProductPrice.objects.create(
            supplier_product=product,
            price=Decimal("42.00"),
            status=PriceStatus.ACTIVE,
            valid_from=timezone.now() - timezone.timedelta(days=1),
        )
        self.authenticate()

        delete_response = self.client.delete(f"/api/v1/suppliers/products/{product.id}")
        self.assertEqual(delete_response.status_code, 204)
        product.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ARCHIVED)

        restore_response = self.client.post(f"/api/v1/suppliers/products/{product.id}/restore")

        self.assertEqual(restore_response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ACTIVE)
        self.assertEqual(product.current_price, Decimal("42.00"))

    def test_delete_twice_is_idempotent_but_logs_an_audit_entry_each_call(self):
        """Характеризующий тест, не баг: archive_supplier_product() (внутрь
        которой теперь делегирует delete) вызывает record_audit_log() на
        каждый вызов независимо от того, менялся ли статус — второй DELETE
        уже архивированного товара пишет ещё одну запись action=ARCHIVED.
        Задокументировано намеренно, чтобы не удивлять будущего читателя
        audit log."""
        product = self.create_supplier_product()
        self.authenticate()

        self.client.delete(f"/api/v1/suppliers/products/{product.id}")
        self.client.delete(f"/api/v1/suppliers/products/{product.id}")

        entries = AuditLog.objects.filter(entity_type=AuditLogEntityType.SUPPLIER_PRODUCT, entity_id=product.id)
        self.assertEqual(entries.count(), 2)
        self.assertTrue(all(e.action == AuditLogAction.ARCHIVED for e in entries))
        product.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ARCHIVED)

    def test_delete_bulk_missing_id_rejects_whole_batch(self):
        product = self.create_supplier_product()
        self.authenticate()

        response = self.client.delete(
            "/api/v1/suppliers/products/bulk", {"ids": [str(product.id), str(uuid4())]}, format="json"
        )

        self.assertEqual(response.status_code, 404)
        product.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ACTIVE)

    def test_delete_bulk_empty_ids_rejected(self):
        self.authenticate()

        response = self.client.delete("/api/v1/suppliers/products/bulk", {"ids": []}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_delete_bulk_cross_supplier_all_or_nothing(self):
        own = Supplier.objects.create(legal_name="Delete Bulk Own LLC", display_name="Delete Bulk Own")
        own_product = self.create_supplier_product(own)
        other_product = self.create_supplier_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.delete(
            "/api/v1/suppliers/products/bulk", {"ids": [str(own_product.id), str(other_product.id)]}, format="json"
        )

        self.assertEqual(response.status_code, 404)
        own_product.refresh_from_db()
        other_product.refresh_from_db()
        self.assertEqual(own_product.status, EntityStatus.ACTIVE)
        self.assertEqual(other_product.status, EntityStatus.ACTIVE)

    def test_delete_all_requires_supplier_id_for_service_token(self):
        self.authenticate()

        response = self.client.delete("/api/v1/suppliers/products", {}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_delete_all_404_for_unknown_supplier(self):
        self.authenticate()

        response = self.client.delete("/api/v1/suppliers/products", {"supplier_id": str(uuid4())}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_delete_all_no_products_returns_zero(self):
        supplier = Supplier.objects.create(legal_name="Delete All Empty LLC", display_name="Delete All Empty")
        self.authenticate()

        response = self.client.delete("/api/v1/suppliers/products", {"supplier_id": str(supplier.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["deleted_count"], 0)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductRestoreAPITests(APITestCase):
    """2026-08-04: обратная операция к archive — "а ручка для восстановления?"
    По решению пользователя restore возвращает не только status=active, но и
    отменённую при archive цену (иначе товар был бы активным, но без цены)."""

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier=None, status=EntityStatus.ARCHIVED):
        supplier = supplier or Supplier.objects.create(legal_name="Restore LLC", display_name="Restore")
        unit = Unit.objects.create(code=f"kg-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        n = Unit.objects.count()
        product = Product.objects.create(
            canonical_name=f"Canonical restore {n}", normalized_name=f"restore-{n}", base_unit=unit
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=f"Restore raw {n}",
            normalized_name=f"restore-{n}",
            status=status,
        )

    def url(self, product_id):
        return f"/api/v1/suppliers/products/{product_id}/restore"

    def test_rejects_without_service_token(self):
        product = self.create_supplier_product()
        response = self.client.post(self.url(product.id))
        self.assertEqual(response.status_code, 401)

    def test_restores_status_and_cancelled_price(self):
        product = self.create_supplier_product()
        cancelled_price = SupplierProductPrice.objects.create(
            supplier_product=product,
            price=Decimal("42.00"),
            status=PriceStatus.CANCELLED,
            valid_from=timezone.now() - timezone.timedelta(days=1),
            valid_to=timezone.now(),
        )
        self.authenticate()

        response = self.client.post(self.url(product.id))

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        cancelled_price.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ACTIVE)
        self.assertEqual(cancelled_price.status, PriceStatus.ACTIVE)
        self.assertIsNone(cancelled_price.valid_to)
        self.assertEqual(product.current_price, Decimal("42.00"))

    def test_does_not_restore_price_when_one_already_active(self):
        """Защита от дублей: если у товара уже почему-то есть активная цена,
        отменённая (старая) не поднимается поверх неё."""
        product = self.create_supplier_product()
        active_price = SupplierProductPrice.objects.create(
            supplier_product=product, price=Decimal("50.00"), status=PriceStatus.ACTIVE
        )
        cancelled_price = SupplierProductPrice.objects.create(
            supplier_product=product,
            price=Decimal("42.00"),
            status=PriceStatus.CANCELLED,
            valid_from=timezone.now() - timezone.timedelta(days=1),
            valid_to=timezone.now(),
        )
        self.authenticate()

        response = self.client.post(self.url(product.id))

        self.assertEqual(response.status_code, 200)
        active_price.refresh_from_db()
        cancelled_price.refresh_from_db()
        self.assertEqual(active_price.status, PriceStatus.ACTIVE)
        self.assertEqual(cancelled_price.status, PriceStatus.CANCELLED)

    def test_restoring_already_active_product_is_a_noop(self):
        product = self.create_supplier_product(status=EntityStatus.ACTIVE)
        self.authenticate()

        response = self.client.post(self.url(product.id))

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.status, EntityStatus.ACTIVE)

    def test_supplier_cabinet_jwt_cannot_restore_other_suppliers_product(self):
        other_product = self.create_supplier_product()
        own_supplier = Supplier.objects.create(legal_name="Restore Own LLC", display_name="Restore Own")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_supplier))
        response = self.client.post(self.url(other_product.id))

        self.assertEqual(response.status_code, 404)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductBulkRestoreAPITests(APITestCase):
    url = "/api/v1/suppliers/products/bulk-restore"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier=None, status=EntityStatus.ARCHIVED):
        supplier = supplier or Supplier.objects.create(legal_name="Bulk Restore LLC", display_name="Bulk Restore")
        unit = Unit.objects.create(code=f"kg-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        n = Unit.objects.count()
        product = Product.objects.create(
            canonical_name=f"Canonical bulk-restore {n}", normalized_name=f"bulk-restore-{n}", base_unit=unit
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=f"Bulk restore raw {n}",
            normalized_name=f"bulk-restore-{n}",
            status=status,
        )

    def test_rejects_without_service_token(self):
        product = self.create_supplier_product()
        response = self.client.post(self.url, {"ids": [str(product.id)]}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_restores_multiple_products_with_service_token(self):
        supplier = Supplier.objects.create(legal_name="Bulk Restore LLC 2", display_name="Bulk Restore 2")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        self.authenticate()

        response = self.client.post(self.url, {"ids": [str(product_a.id), str(product_b.id)]}, format="json")

        self.assertEqual(response.status_code, 200)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.status, EntityStatus.ACTIVE)
        self.assertEqual(product_b.status, EntityStatus.ACTIVE)

    def test_all_or_nothing_across_suppliers(self):
        own_supplier = Supplier.objects.create(legal_name="Bulk Restore Own LLC", display_name="Bulk Restore Own")
        own_product = self.create_supplier_product(own_supplier)
        other_product = self.create_supplier_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_supplier))
        response = self.client.post(self.url, {"ids": [str(own_product.id), str(other_product.id)]}, format="json")

        self.assertEqual(response.status_code, 404)
        own_product.refresh_from_db()
        self.assertEqual(own_product.status, EntityStatus.ARCHIVED)

    def test_reactivates_each_products_own_cancelled_price(self):
        """2026-08-15: Витя — restore-all/bulk-restore на 380 товарах не
        укладывалось в таймаут гейтвея (~2 мин, ERR_CONNECTION_TIMED_OUT),
        хотя bulk-archive на том же объёме — 1.67с (тот же баг, что уже
        чинили для archive/delete 2026-08-12, просто не докатили до
        restore). Set-based переписывание должно восстанавливать именно
        последнюю отменённую цену КАЖДОГО товара, не путать цены между
        товарами при батче из нескольких id — та же гарантия, что даёт
        одиночный restore (test_restores_status_and_cancelled_price)."""
        supplier = Supplier.objects.create(legal_name="Bulk Restore Prices LLC", display_name="Bulk Restore Prices")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        price_a = SupplierProductPrice.objects.create(
            supplier_product=product_a,
            price=Decimal("42.00"),
            status=PriceStatus.CANCELLED,
            valid_from=timezone.now() - timezone.timedelta(days=1),
            valid_to=timezone.now(),
        )
        price_b = SupplierProductPrice.objects.create(
            supplier_product=product_b,
            price=Decimal("99.00"),
            status=PriceStatus.CANCELLED,
            valid_from=timezone.now() - timezone.timedelta(days=1),
            valid_to=timezone.now(),
        )
        self.authenticate()

        response = self.client.post(self.url, {"ids": [str(product_a.id), str(product_b.id)]}, format="json")

        self.assertEqual(response.status_code, 200)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        price_a.refresh_from_db()
        price_b.refresh_from_db()
        self.assertEqual(price_a.status, PriceStatus.ACTIVE)
        self.assertIsNone(price_a.valid_to)
        self.assertEqual(price_b.status, PriceStatus.ACTIVE)
        self.assertIsNone(price_b.valid_to)
        self.assertEqual(product_a.current_price, Decimal("42.00"))
        self.assertEqual(product_b.current_price, Decimal("99.00"))

    def test_query_count_does_not_scale_with_batch_size(self):
        """Regression guard for the 2026-08-15 fix — a Python loop calling
        restore_supplier_product() per id would make query count grow
        linearly with N; the set-based rewrite should not."""
        from django.test.utils import CaptureQueriesContext

        from api.v1.suppliers.services import bulk_restore_supplier_products

        supplier = Supplier.objects.create(legal_name="Bulk Restore Scale LLC", display_name="Bulk Restore Scale")
        small_batch = [self.create_supplier_product(supplier).id for _ in range(2)]
        large_batch = [self.create_supplier_product(supplier).id for _ in range(8)]

        with CaptureQueriesContext(connections["default"]) as small_ctx:
            bulk_restore_supplier_products(supplier_product_ids=small_batch)
        with CaptureQueriesContext(connections["default"]) as large_ctx:
            bulk_restore_supplier_products(supplier_product_ids=large_batch)

        self.assertEqual(len(small_ctx.captured_queries), len(large_ctx.captured_queries))


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductRestoreAllAPITests(APITestCase):
    url = "/api/v1/suppliers/products/restore-all"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier, status=EntityStatus.ARCHIVED):
        unit = Unit.objects.create(code=f"kg-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        n = Unit.objects.count()
        product = Product.objects.create(
            canonical_name=f"Canonical restore-all {n}", normalized_name=f"restore-all-{n}", base_unit=unit
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=f"Restore-all raw {n}",
            normalized_name=f"restore-all-{n}",
            status=status,
        )

    def test_rejects_without_service_token(self):
        supplier = Supplier.objects.create(legal_name="RA LLC", display_name="RA")
        response = self.client.post(self.url, {"supplier_id": str(supplier.id)}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_service_token_requires_supplier_id(self):
        self.authenticate()
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_service_token_restores_all_archived_products(self):
        supplier = Supplier.objects.create(legal_name="RA LLC 2", display_name="RA 2")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        already_active = self.create_supplier_product(supplier, status=EntityStatus.ACTIVE)
        self.authenticate()

        response = self.client.post(self.url, {"supplier_id": str(supplier.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["restored_count"], 2)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        already_active.refresh_from_db()
        self.assertEqual(product_a.status, EntityStatus.ACTIVE)
        self.assertEqual(product_b.status, EntityStatus.ACTIVE)
        self.assertEqual(already_active.status, EntityStatus.ACTIVE)

    def test_service_token_404_for_unknown_supplier(self):
        self.authenticate()
        response = self.client.post(self.url, {"supplier_id": str(uuid4())}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_cabinet_jwt_restores_only_own_products_ignoring_supplier_id(self):
        own_supplier = Supplier.objects.create(legal_name="RA Own LLC", display_name="RA Own")
        other_supplier = Supplier.objects.create(legal_name="RA Other LLC", display_name="RA Other")
        own_product = self.create_supplier_product(own_supplier)
        other_product = self.create_supplier_product(other_supplier)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_supplier))
        response = self.client.post(self.url, {"supplier_id": str(other_supplier.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["restored_count"], 1)
        own_product.refresh_from_db()
        other_product.refresh_from_db()
        self.assertEqual(own_product.status, EntityStatus.ACTIVE)
        self.assertEqual(other_product.status, EntityStatus.ARCHIVED)

    def test_no_archived_products_returns_zero(self):
        supplier = Supplier.objects.create(legal_name="RA Empty LLC", display_name="RA Empty")
        self.authenticate()

        response = self.client.post(self.url, {"supplier_id": str(supplier.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["restored_count"], 0)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductBulkUpdateAPITests(APITestCase):
    url = "/api/v1/suppliers/products/bulk"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier=None):
        supplier = supplier or Supplier.objects.create(legal_name="Bulk Upd LLC", display_name="Bulk Upd")
        unit = Unit.objects.create(code=f"kg-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        n = Unit.objects.count()
        product = Product.objects.create(
            canonical_name=f"Canonical bulk upd {n}", normalized_name=f"bulk-upd-{n}", base_unit=unit
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=f"Bulk upd raw {n}",
            normalized_name=f"bulk-upd-{n}",
            status=EntityStatus.ACTIVE,
            availability=ProductAvailability.IN_STOCK,
        )

    def test_rejects_without_service_token(self):
        product = self.create_supplier_product()
        response = self.client.patch(
            self.url, {"ids": [str(product.id)], "availability": "out_of_stock"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_updates_availability_on_multiple_products(self):
        supplier = Supplier.objects.create(legal_name="Bulk Upd LLC", display_name="Bulk Upd")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        self.authenticate()

        response = self.client.patch(
            self.url,
            {"ids": [str(product_a.id), str(product_b.id)], "availability": "out_of_stock"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.availability, ProductAvailability.OUT_OF_STOCK)
        self.assertEqual(product_b.availability, ProductAvailability.OUT_OF_STOCK)

    def test_supplier_cabinet_jwt_all_or_nothing_across_suppliers(self):
        own_supplier = Supplier.objects.create(legal_name="Own Upd LLC", display_name="Own Upd")
        own_product = self.create_supplier_product(own_supplier)
        other_product = self.create_supplier_product()

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_supplier))
        response = self.client.patch(
            self.url,
            {"ids": [str(own_product.id), str(other_product.id)], "availability": "out_of_stock"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        own_product.refresh_from_db()
        self.assertEqual(own_product.availability, ProductAvailability.IN_STOCK)

    def test_requires_at_least_one_field_besides_ids(self):
        product = self.create_supplier_product()
        self.authenticate()

        response = self.client.patch(self.url, {"ids": [str(product.id)]}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_empty_ids_rejected(self):
        self.authenticate()
        response = self.client.patch(self.url, {"ids": [], "comment": "x"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_bulk_updates_brand_creating_it_if_missing(self):
        """2026-08-27, Vitya — мультивыбор для смены бренда, с тем же
        get_or_create-по-названию поведением, что и в модалке создания
        товара (на случай, если такого бренда ещё нет в системе)."""
        supplier = Supplier.objects.create(legal_name="Bulk Brand LLC", display_name="Bulk Brand")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        self.authenticate()
        self.assertFalse(Brand.objects.filter(normalized_name="новый бренд").exists())

        response = self.client.patch(
            self.url,
            {"ids": [str(product_a.id), str(product_b.id)], "brand": "Новый бренд"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        brand = Brand.objects.get(normalized_name="новый бренд")
        self.assertEqual(brand.canonical_name, "Новый бренд")
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.brand_id, brand.id)
        self.assertEqual(product_b.brand_id, brand.id)

    def test_bulk_updates_existing_brand_via_brand_id(self):
        """2026-08-28, Vitya: тот же автокомплит-сценарий, что у одиночного
        PATCH, но мультивыбором."""
        supplier = Supplier.objects.create(legal_name="Bulk BrandId LLC", display_name="Bulk BrandId")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        brand = Brand.objects.create(canonical_name="Lavazza", normalized_name="lavazza")
        self.authenticate()

        response = self.client.patch(
            self.url,
            {"ids": [str(product_a.id), str(product_b.id)], "brand_id": str(brand.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.brand_id, brand.id)
        self.assertEqual(product_b.brand_id, brand.id)
        self.assertEqual(Brand.objects.filter(normalized_name="lavazza").count(), 1)

    def test_bulk_updates_country_of_origin(self):
        supplier = Supplier.objects.create(legal_name="Bulk Country LLC", display_name="Bulk Country")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        self.authenticate()

        response = self.client.patch(
            self.url,
            {"ids": [str(product_a.id), str(product_b.id)], "country_of_origin": "Россия"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.country_of_origin, "Россия")
        self.assertEqual(product_b.country_of_origin, "Россия")

    def test_query_count_does_not_scale_with_product_count(self):
        """2026-08-27, Vitya — "а это не сильно нагружать систему будет?".
        Раньше это был Python-цикл (get_or_create бренда + SELECT FOR UPDATE
        + UPDATE НА КАЖДЫЙ товар) — 105 запросов на 20 товаров с одним и тем
        же новым брендом. Тот же класс проблемы, что уже чинили для
        archive/delete/restore (2026-08-12/15). Переписано на set-based SQL:
        число запросов должно быть одинаковым для 3 и для 30 товаров."""
        from django.test.utils import CaptureQueriesContext

        supplier = Supplier.objects.create(legal_name="Bulk Perf LLC", display_name="Bulk Perf")
        self.authenticate()

        few_ids = [str(self.create_supplier_product(supplier).id) for _ in range(3)]
        with CaptureQueriesContext(connections["default"]) as few_ctx:
            self.client.patch(self.url, {"ids": few_ids, "brand": "Перф бренд"}, format="json")

        many_ids = [str(self.create_supplier_product(supplier).id) for _ in range(30)]
        with CaptureQueriesContext(connections["default"]) as many_ctx:
            self.client.patch(self.url, {"ids": many_ids, "brand": "Другой перф бренд"}, format="json")

        self.assertEqual(len(few_ctx.captured_queries), len(many_ctx.captured_queries))


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductBulkUpdateAllAPITests(APITestCase):
    """2026-08-27, Vitya — галочка "выбрать все" в bulk-actions таблицы товаров."""

    url = "/api/v1/suppliers/products/bulk-all"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier, status=EntityStatus.ACTIVE):
        unit = Unit.objects.create(code=f"kg-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        n = Unit.objects.count()
        product = Product.objects.create(
            canonical_name=f"Canonical bulk-all {n}", normalized_name=f"bulk-all-{n}", base_unit=unit
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=f"Bulk-all raw {n}",
            normalized_name=f"bulk-all-{n}",
            status=status,
        )

    def test_rejects_without_service_token(self):
        supplier = Supplier.objects.create(legal_name="BA LLC", display_name="BA")
        response = self.client.patch(
            self.url, {"supplier_id": str(supplier.id), "country_of_origin": "Россия"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_service_token_requires_supplier_id(self):
        self.authenticate()
        response = self.client.patch(self.url, {"country_of_origin": "Россия"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_requires_at_least_one_field_besides_supplier_id(self):
        supplier = Supplier.objects.create(legal_name="BA LLC 2", display_name="BA 2")
        self.authenticate()
        response = self.client.patch(self.url, {"supplier_id": str(supplier.id)}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_updates_all_active_products_only(self):
        supplier = Supplier.objects.create(legal_name="BA LLC 3", display_name="BA 3")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        archived = self.create_supplier_product(supplier, status=EntityStatus.ARCHIVED)
        other_supplier = Supplier.objects.create(legal_name="BA Other", display_name="BA Other")
        other_product = self.create_supplier_product(other_supplier)
        self.authenticate()

        response = self.client.patch(
            self.url,
            {"supplier_id": str(supplier.id), "brand": "Массовый бренд"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated_count"], 2)
        brand = Brand.objects.get(normalized_name="массовый бренд")
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        archived.refresh_from_db()
        other_product.refresh_from_db()
        self.assertEqual(product_a.brand_id, brand.id)
        self.assertEqual(product_b.brand_id, brand.id)
        self.assertIsNone(archived.brand_id)
        self.assertIsNone(other_product.brand_id)

    def test_updates_all_active_products_via_brand_id(self):
        supplier = Supplier.objects.create(legal_name="BA BrandId LLC", display_name="BA BrandId")
        product_a = self.create_supplier_product(supplier)
        product_b = self.create_supplier_product(supplier)
        brand = Brand.objects.create(canonical_name="Nestle", normalized_name="nestle")
        self.authenticate()

        response = self.client.patch(
            self.url,
            {"supplier_id": str(supplier.id), "brand_id": str(brand.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated_count"], 2)
        product_a.refresh_from_db()
        product_b.refresh_from_db()
        self.assertEqual(product_a.brand_id, brand.id)
        self.assertEqual(product_b.brand_id, brand.id)

    def test_cabinet_jwt_scopes_to_own_supplier(self):
        supplier = Supplier.objects.create(legal_name="BA LLC 4", display_name="BA 4")
        product = self.create_supplier_product(supplier)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        response = self.client.patch(self.url, {"country_of_origin": "Беларусь"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.data["supplier_id"]), str(supplier.id))
        product.refresh_from_db()
        self.assertEqual(product.country_of_origin, "Беларусь")


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierPriceUploadIntakeAPITests(TemporaryMediaRootMixin, APITestCase):
    file_url = "/api/v1/suppliers/uploads"
    text_url = "/api/v1/suppliers/uploads/text"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier(self):
        return Supplier.objects.create(
            legal_name="Supplier LLC",
            display_name="Supplier",
        )

    def test_rejects_file_upload_without_service_token(self):
        supplier = self.create_supplier()
        source_file = SimpleUploadedFile("price.csv", b"name,price\nmilk,10\n", content_type="text/csv")

        response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.TELEGRAM,
                "source_file": source_file,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 401)

    def test_file_upload_creates_supplier_price_upload(self):
        supplier = self.create_supplier()
        source_file = SimpleUploadedFile("price.csv", b"name,price\nmilk,10\n", content_type="text/csv")
        self.authenticate()

        response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.TELEGRAM,
                "source_file": source_file,
                "source_file_name": "supplier-price.csv",
                "telegram_message_id": "tg-123",
                "n8n_execution_id": "n8n-456",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 201)
        upload = SupplierPriceUpload.objects.get()
        self.assertEqual(upload.supplier, supplier)
        self.assertEqual(upload.source_channel, SourceChannel.TELEGRAM)
        self.assertEqual(upload.source_file_name, "supplier-price.csv")
        self.assertEqual(upload.status, UploadStatus.RECEIVED)
        self.assertEqual(upload.raw_payload["kind"], "file")
        self.assertEqual(upload.raw_payload["metadata"]["original_file_name"], "price.csv")
        self.assertEqual(upload.raw_payload["metadata"]["telegram_message_id"], "tg-123")
        self.assertEqual(upload.raw_payload["metadata"]["n8n_execution_id"], "n8n-456")
        self.assertTrue(upload.source_file_uri)
        self.assertEqual(upload.raw_payload["metadata"]["source_file_uri"], upload.source_file_uri)
        self.assertTrue(default_storage.exists(upload.source_file_uri))
        self.assertEqual(upload.raw_payload["extraction"]["rows_total"], 1)
        self.assertEqual(upload.raw_payload["extracted_rows"][0]["cells"]["name"], "milk")
        self.assertFalse(SupplierProduct.objects.exists())
        self.assertFalse(SupplierProductPrice.objects.exists())

    def test_supplier_cabinet_jwt_overrides_supplier_id_with_own_identity(self):
        own = self.create_supplier()
        other = Supplier.objects.create(legal_name="Other LLC", display_name="Other")
        source_file = SimpleUploadedFile("price.csv", b"name,price\nmilk,10\n", content_type="text/csv")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.post(
            self.file_url,
            {
                # Attempted IDOR: claim to upload on behalf of `other` — the
                # JWT identity must win, not this client-supplied value.
                "supplier_id": str(other.id),
                "source_channel": SourceChannel.TELEGRAM,
                "source_file": source_file,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 201)
        upload = SupplierPriceUpload.objects.get()
        self.assertEqual(upload.supplier_id, own.id)

    def test_repeated_file_upload_with_same_n8n_execution_id_returns_existing_upload(self):
        supplier = self.create_supplier()
        self.authenticate()

        first_response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.TELEGRAM,
                "source_file": SimpleUploadedFile("price.csv", b"name,price\nmilk,10\n", content_type="text/csv"),
                "n8n_execution_id": "n8n-repeat",
            },
            format="multipart",
        )
        second_response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.TELEGRAM,
                "source_file": SimpleUploadedFile("price.csv", b"name,price\nmilk,10\n", content_type="text/csv"),
                "n8n_execution_id": "n8n-repeat",
            },
            format="multipart",
        )

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 201)
        self.assertEqual(first_response.json()["id"], second_response.json()["id"])
        self.assertEqual(SupplierPriceUpload.objects.count(), 1)

    def test_text_upload_creates_supplier_price_upload(self):
        supplier = self.create_supplier()
        self.authenticate()

        response = self.client.post(
            self.text_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.MANUAL,
                "text": "milk 10 rub",
                "n8n_execution_id": "n8n-text",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        upload = SupplierPriceUpload.objects.get()
        self.assertEqual(upload.supplier, supplier)
        self.assertEqual(upload.source_channel, SourceChannel.MANUAL)
        self.assertIsNone(upload.source_file_name)
        self.assertEqual(upload.status, UploadStatus.RECEIVED)
        self.assertEqual(upload.raw_payload["kind"], "text")
        self.assertEqual(upload.raw_payload["text"], "milk 10 rub")
        self.assertEqual(upload.raw_payload["metadata"]["n8n_execution_id"], "n8n-text")
        self.assertFalse(SupplierProduct.objects.exists())
        self.assertFalse(SupplierProductPrice.objects.exists())

    def test_text_upload_supplier_cabinet_jwt_overrides_supplier_id(self):
        own = self.create_supplier()
        other = Supplier.objects.create(legal_name="Other LLC", display_name="Other")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.post(
            self.text_url,
            {
                "supplier_id": str(other.id),
                "source_channel": SourceChannel.MANUAL,
                "text": "milk 10 rub",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        upload = SupplierPriceUpload.objects.get()
        self.assertEqual(upload.supplier_id, own.id)

    def test_repeated_text_upload_with_same_n8n_execution_id_returns_existing_upload(self):
        supplier = self.create_supplier()
        self.authenticate()
        payload = {
            "supplier_id": str(supplier.id),
            "source_channel": SourceChannel.TELEGRAM,
            "text": "milk 10 rub",
            "n8n_execution_id": "n8n-text-repeat",
        }

        first_response = self.client.post(self.text_url, payload, format="json")
        second_response = self.client.post(self.text_url, payload, format="json")

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 201)
        self.assertEqual(first_response.json()["id"], second_response.json()["id"])
        self.assertEqual(SupplierPriceUpload.objects.count(), 1)

    def test_rejects_invalid_supplier(self):
        source_file = SimpleUploadedFile("price.csv", b"name,price\nmilk,10\n", content_type="text/csv")
        self.authenticate()

        response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(uuid4()),
                "source_channel": SourceChannel.API,
                "source_file": source_file,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SupplierPriceUpload.objects.exists())

    def test_corrupt_binary_xls_returns_validation_error(self):
        supplier = self.create_supplier()
        self.authenticate()
        response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.API,
                "source_file": SimpleUploadedFile(
                    "price.xls", bytes.fromhex("d0cf11e0a1b11ae1") + b"invalid", content_type="application/vnd.ms-excel"
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("source_file", response.json())
        self.assertFalse(SupplierPriceUpload.objects.exists())


class HeaderDetectionTests(SimpleTestCase):
    def test_detects_header_row_after_a_preamble_row(self):
        rows = [
            (1, ["Самарская и Ульяновская область: +7 900 000-00-00"]),
            (2, ["Наименование", "Цена", "Ед изм"]),
            (3, ["Кунжут черный жареный, 1кг", "850.75", "шт"]),
        ]

        header_index, headers = _detect_header(rows)

        self.assertEqual(header_index, 1)
        self.assertEqual(headers, ["наименование", "цена", "ед изм"])

    def test_detects_header_row_at_last_scanned_position(self):
        preamble = [(index, [f"junk {index}"]) for index in range(1, 10)]
        rows = preamble + [(10, ["Наименование", "Цена"])]

        header_index, headers = _detect_header(rows)

        self.assertEqual(header_index, 9)
        self.assertEqual(headers, ["наименование", "цена"])


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierPriceUploadNormalizeAPITests(TemporaryMediaRootMixin, APITestCase):
    file_url = "/api/v1/suppliers/uploads"
    text_url = "/api/v1/suppliers/uploads/text"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier(self):
        return Supplier.objects.create(
            legal_name="Supplier LLC",
            display_name="Supplier",
        )

    def create_units(self):
        unit_piece, _ = Unit.objects.get_or_create(code="шт", defaults={"name": "Штука", "kind": UnitKind.PIECE})
        unit_weight, _ = Unit.objects.get_or_create(code="кг", defaults={"name": "Килограмм", "kind": UnitKind.WEIGHT})
        return unit_piece, unit_weight

    def normalize_url(self, upload):
        return f"/api/v1/suppliers/uploads/{upload.id}/normalize"

    def ai_cleanup_url(self, upload):
        return f"/api/v1/suppliers/uploads/{upload.id}/ai-cleanup"

    def detail_url(self, upload):
        return f"/api/v1/suppliers/uploads/{upload.id}"

    def upload_csv(self, supplier, content, *, source_channel=SourceChannel.TELEGRAM):
        response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": source_channel,
                "source_file": SimpleUploadedFile("price.csv", content.encode("utf-8"), content_type="text/csv"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        return SupplierPriceUpload.objects.get(id=response.json()["id"])

    def upload_xlsx(self, supplier, rows):
        workbook = Workbook()
        worksheet = workbook.active
        for row in rows:
            worksheet.append(row)
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()
        buffer.seek(0)
        response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.TELEGRAM,
                "source_file": SimpleUploadedFile(
                    "price.xlsx",
                    buffer.read(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        return SupplierPriceUpload.objects.get(id=response.json()["id"])

    def upload_pdf(self, supplier, content=b"%PDF-1.4\n%test\n"):
        response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.TELEGRAM,
                "source_file": SimpleUploadedFile(
                    "price.pdf",
                    content,
                    content_type="application/pdf",
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        return SupplierPriceUpload.objects.get(id=response.json()["id"])

    def test_rejects_normalize_without_service_token(self):
        supplier = self.create_supplier()
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.API,
            raw_payload={"kind": "text", "text": "milk 10 rub"},
        )

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 401)

    def test_supplier_cabinet_jwt_can_normalize_own_upload(self):
        supplier = self.create_supplier()
        self.authenticate()
        upload = self.upload_csv(supplier, "name,price\nmilk,10\n")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertNotEqual(response.status_code, 404)

    def test_supplier_cabinet_jwt_cannot_normalize_other_suppliers_upload(self):
        own = self.create_supplier()
        other = self.create_supplier()
        self.authenticate()
        other_upload = self.upload_csv(other, "name,price\nmilk,10\n")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.post(
            self.normalize_url(other_upload), {"use_ai": False, "run_async": False}, format="json"
        )

        self.assertEqual(response.status_code, 404)
        other_upload.refresh_from_db()
        self.assertEqual(other_upload.status, UploadStatus.RECEIVED)

    def test_supplier_cabinet_jwt_can_get_own_upload_detail(self):
        supplier = self.create_supplier()
        self.authenticate()
        upload = self.upload_csv(supplier, "name,price\nmilk,10\n")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get(self.detail_url(upload))

        self.assertEqual(response.status_code, 200)

    def test_supplier_cabinet_jwt_cannot_get_other_suppliers_upload_detail(self):
        own = self.create_supplier()
        other = self.create_supplier()
        self.authenticate()
        other_upload = self.upload_csv(other, "name,price\nmilk,10\n")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.get(self.detail_url(other_upload))

        self.assertEqual(response.status_code, 404)

    def test_supplier_cabinet_jwt_cannot_get_other_suppliers_upload_items(self):
        own = self.create_supplier()
        other = self.create_supplier()
        self.authenticate()
        other_upload = self.upload_csv(other, "name,price\nmilk,10\n")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.get(f"/api/v1/suppliers/uploads/{other_upload.id}/items")

        self.assertEqual(response.status_code, 404)

    def test_active_member_of_both_companies_can_see_upload_from_the_other_one(self):
        """2026-08-17 product decision: switching the active company in the
        UI swaps the cabinet JWT (company-scoped, not per-person) — a
        person with active membership in both companies used to 404 on an
        upload started under whichever company ISN'T the current token's,
        even though they're allowed to see it. Own-company match is still
        the fast path; this only widens access for someone who's genuinely
        an active member of the upload's real owner, never anyone else."""
        current_company = self.create_supplier()
        upload_owner_company = self.create_supplier()
        member = User.objects.create(email=f"dual-{uuid4()}@example.com", full_name="Dual", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=upload_owner_company, user=member, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.authenticate()
        upload = self.upload_csv(upload_owner_company, "name,price\nmilk,10\n")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(current_company, user=member))
        response = self.client.get(self.detail_url(upload))

        self.assertEqual(response.status_code, 200)

    def test_member_of_only_one_company_still_cannot_see_the_others_upload(self):
        """Same setup as above but without the SupplierUser membership —
        confirms the relaxation didn't quietly widen access to everyone."""
        current_company = self.create_supplier()
        upload_owner_company = self.create_supplier()
        non_member = User.objects.create(
            email=f"non-member-{uuid4()}@example.com", full_name="Non Member", role=UserRole.SUPPLIER
        )
        self.authenticate()
        upload = self.upload_csv(upload_owner_company, "name,price\nmilk,10\n")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(current_company, user=non_member))
        response = self.client.get(self.detail_url(upload))

        self.assertEqual(response.status_code, 404)

    def test_supplier_cabinet_jwt_cannot_preview_other_suppliers_upload(self):
        own = self.create_supplier()
        other = self.create_supplier()
        self.authenticate()
        other_upload = self.upload_csv(other, "name,price\nmilk,10\n")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.get(f"/api/v1/suppliers/uploads/{other_upload.id}/preview.xlsx")

        self.assertEqual(response.status_code, 404)

    def test_supplier_cabinet_jwt_cannot_ai_cleanup_other_suppliers_upload(self):
        own = self.create_supplier()
        other = self.create_supplier()
        self.authenticate()
        other_upload = self.upload_csv(other, "name,price\nmilk,10\n")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.post(self.ai_cleanup_url(other_upload), {}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_backend_normalizes_csv_upload_without_n8n_ai_loop(self):
        supplier = self.create_supplier()
        unit_piece, unit_weight = self.create_units()
        self.authenticate()
        upload = self.upload_csv(
            supplier,
            'Наименование;Цена;Ед\n"Кунжут черный жареный ТМ ""Yakimal"", 1кг 1/15";850;шт\n',
        )

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items_total"], 1)
        self.assertEqual(response.json()["products_created"], 1)
        self.assertEqual(response.json()["needs_review"], 0)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.supplier, supplier)
        self.assertEqual(supplier_product.supplier_name, 'Кунжут черный жареный ТМ "Yakimal", 1кг 1/15')
        self.assertEqual(supplier_product.normalized_name, "кунжут черный жареный")
        self.assertEqual(supplier_product.unit, unit_piece)
        self.assertEqual(supplier_product.net_value, Decimal("1.0000"))
        self.assertEqual(supplier_product.net_unit, unit_weight)
        self.assertEqual(supplier_product.package, "1 кг; 1/15")
        self.assertEqual(supplier_product.delivery_multiple, Decimal("15.0000"))
        self.assertEqual(SupplierProductPrice.objects.get().price, Decimal("850.00"))

    def test_web_upload_completion_notifies_linked_supplier_bot(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(
            supplier,
            "Наименование;Цена;Ед\nМолоко;100;шт\n",
            source_channel=SourceChannel.WEB,
        )

        with patch("api.v1.suppliers.bot_events.notify_linked_supplier_chats") as notify:
            response = self.client.post(
                self.normalize_url(upload),
                {"use_ai": False, "run_async": False},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["supplier"], supplier)
        self.assertIn("Прайс с сайта обработан", notify.call_args.kwargs["text"])

    def test_backend_normalizes_pdf_upload_through_deferred_docling_extraction(self):
        supplier = self.create_supplier()
        unit_piece, _unit_weight = self.create_units()
        self.authenticate()
        upload = self.upload_pdf(supplier)

        self.assertTrue(upload.source_file_uri)
        self.assertTrue(upload.raw_payload["extraction"]["deferred"])
        self.assertEqual(upload.raw_payload["extraction"]["extractor"], "docling_pdf")
        self.assertEqual(upload.raw_payload["extraction"]["rows_total"], 0)

        markdown = "| Наименование | Цена | Ед |\n| --- | ---: | --- |\n| Кунжут черный жареный 1кг | 850 | шт |\n"
        with patch("file_imports.extraction._docling_markdown_from_pdf", return_value=markdown):
            response = self.client.post(
                self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items_total"], 1)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.unit, unit_piece)
        self.assertEqual(supplier_product.normalized_name, "кунжут черный жареный")
        self.assertEqual(SupplierProductPrice.objects.get().price, Decimal("850.00"))
        upload.refresh_from_db()
        self.assertFalse(upload.raw_payload["extraction"]["deferred"])
        self.assertEqual(upload.raw_payload["extraction"]["rows_total"], 1)

    def test_backend_normalizes_text_upload_without_n8n_ai_loop(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload_response = self.client.post(
            self.text_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.TELEGRAM,
                "text": 'Кунжут черный жареный ТМ "Yakimal", 1кг 1/15, шт. 850 руб',
            },
            format="json",
        )
        self.assertEqual(upload_response.status_code, 201)
        upload = SupplierPriceUpload.objects.get(id=upload_response.json()["id"])

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["products_created"], 1)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.normalized_name, "кунжут черный жареный")
        self.assertEqual(supplier_product.package, "1 кг; 1/15")
        self.assertEqual(SupplierProductPrice.objects.get().price, Decimal("850.00"))

    def test_backend_normalizes_supplier_xlsx_headers_without_ai(self):
        supplier = self.create_supplier()
        unit_piece, _unit_weight = self.create_units()
        self.authenticate()
        upload = self.upload_xlsx(
            supplier,
            [
                [
                    "Наименование товара",
                    "Ед.Изм. для заказа",
                    "Минимальная Кратность в заказе",
                    "Цена за Ед,Изм, для заказа",
                    "Группа Товара",
                    "Подгруппа товара",
                    "Вид товар",
                    "Доступность",
                    "SKU_ID",
                ],
                [
                    'Кунжут черный жареный ТМ "Yakimal", 1кг 1/15, шт',
                    "шт",
                    15,
                    850.75,
                    "продукты питания",
                    "бакалея",
                    "азиатская кухня",
                    "",
                    "SKU-1",
                ],
            ],
        )

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items_total"], 1)
        self.assertEqual(response.json()["products_created"], 1)
        self.assertEqual(response.json()["needs_review"], 0)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.supplier_sku, "SKU-1")
        self.assertEqual(supplier_product.unit, unit_piece)
        self.assertEqual(supplier_product.delivery_multiple, Decimal("15.0000"))
        self.assertEqual(SupplierProductPrice.objects.get().price, Decimal("850.75"))

    def test_backend_prefers_product_name_header_over_category_headers(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_xlsx(
            supplier,
            [
                [
                    "Вид товар",
                    "Группа товара",
                    "Подгруппа товара",
                    "Ед.Изм. для заказа",
                    "Наименование товара",
                    "Цена за Ед,Изм, для заказа",
                    "Минимальная Кратность в заказе",
                ],
                [
                    "макаронные изделия",
                    "продукты питания",
                    "бакалея",
                    "шт",
                    "Лапша гречневая Соба 400 гр, 1/20, шт",
                    73.15,
                    20,
                ],
                [
                    "макаронные изделия",
                    "продукты питания",
                    "бакалея",
                    "шт",
                    "Лапша домашняя Лагманная 600г/6, шт",
                    115.36,
                    1,
                ],
            ],
        )

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["products_created"], 2)
        self.assertEqual(SupplierProduct.objects.count(), 2)
        self.assertFalse(SupplierProduct.objects.filter(supplier_name="макаронные изделия").exists())
        self.assertTrue(
            SupplierProduct.objects.filter(
                supplier_name="Лапша гречневая Соба 400 гр, 1/20, шт",
                normalized_name="лапша гречневая соба",
                package="400 г; 1/20",
            ).exists()
        )

    def test_backend_normalizes_supplier_xlsx_with_preamble_row_before_header(self):
        supplier = self.create_supplier()
        unit_piece, _unit_weight = self.create_units()
        self.authenticate()
        upload = self.upload_xlsx(
            supplier,
            [
                ["Самарская и Ульяновская область: +7 900 000-00-00"],
                [
                    "Наименование товара",
                    "Ед.Изм. для заказа",
                    "Минимальная Кратность в заказе",
                    "Цена за Ед,Изм, для заказа",
                ],
                [
                    'Кунжут черный жареный ТМ "Yakimal", 1кг 1/15, шт',
                    "шт",
                    15,
                    850.75,
                ],
            ],
        )

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items_total"], 1)
        self.assertEqual(response.json()["needs_review"], 0)
        self.assertEqual(response.json()["products_created"], 1)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.unit, unit_piece)
        self.assertEqual(SupplierProductPrice.objects.get().price, Decimal("850.75"))

    def test_backend_xlsx_reupload_changed_price_updates_existing_product(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        headers = [
            "Наименование товара",
            "Ед.Изм. для заказа",
            "Минимальная Кратность в заказе",
            "Цена за Ед,Изм, для заказа",
            "SKU_ID",
        ]
        first_upload = self.upload_xlsx(
            supplier,
            [
                headers,
                ['Кунжут черный жареный ТМ "Yakimal", 1кг 1/15, шт', "шт", 15, 850.75, "SKU-1"],
            ],
        )
        second_upload = self.upload_xlsx(
            supplier,
            [
                headers,
                ['Кунжут черный жареный ТМ "Yakimal", 1кг 1/15, шт', "шт", 15, 999.10, "SKU-1"],
            ],
        )

        first_response = self.client.post(
            self.normalize_url(first_upload), {"use_ai": False, "run_async": False}, format="json"
        )
        second_response = self.client.post(
            self.normalize_url(second_upload), {"use_ai": False, "run_async": False}, format="json"
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.json()["products_created"], 1)
        self.assertEqual(second_response.json()["prices_created"], 1)
        self.assertEqual(second_response.json()["items_matched"], 0)
        upload_item = SupplierPriceUploadItem.objects.get(upload=second_upload)
        self.assertEqual(upload_item.raw_payload["old_price"], "850.75")
        self.assertEqual(SupplierProduct.objects.count(), 1)
        active_price = SupplierProductPrice.objects.get(status=PriceStatus.ACTIVE, valid_to__isnull=True)
        self.assertEqual(active_price.price, Decimal("999.10"))
        self.assertEqual(
            SupplierProductPrice.objects.filter(status=PriceStatus.SUPERSEDED, valid_to__isnull=False).count(),
            1,
        )

    def test_backend_xlsx_reupload_same_rows_matches_without_new_items_or_prices(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        headers = [
            "Наименование товара",
            "Ед.Изм. для заказа",
            "Минимальная Кратность в заказе",
            "Цена за Ед,Изм, для заказа",
            "SKU_ID",
        ]
        first_upload = self.upload_xlsx(
            supplier,
            [
                headers,
                ['Кунжут черный жареный ТМ "Yakimal", 1кг 1/15, шт', "шт", 15, 850.75, "SKU-1"],
            ],
        )
        second_upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            source_file_name="same-price.xlsx",
            raw_payload={
                "kind": "file",
                "extracted_rows": first_upload.raw_payload["extracted_rows"],
            },
        )

        first_response = self.client.post(
            self.normalize_url(first_upload), {"use_ai": False, "run_async": False}, format="json"
        )
        second_response = self.client.post(
            self.normalize_url(second_upload), {"use_ai": False, "run_async": False}, format="json"
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.json()["items_total"], 1)
        self.assertEqual(second_response.json()["items_created"], 0)
        self.assertEqual(second_response.json()["items_matched"], 1)
        self.assertEqual(second_response.json()["products_created"], 0)
        self.assertEqual(second_response.json()["products_updated"], 0)
        self.assertEqual(second_response.json()["prices_created"], 0)
        self.assertEqual(SupplierPriceUploadItem.objects.filter(upload=first_upload).count(), 1)
        self.assertFalse(SupplierPriceUploadItem.objects.filter(upload=second_upload).exists())
        self.assertEqual(SupplierProduct.objects.count(), 1)
        self.assertEqual(SupplierProductPrice.objects.count(), 1)

    def test_raw_name_matching_creates_separate_products_for_different_brands(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        headers = ["Наименование товара", "Ед.Изм. для заказа", "Цена за Ед,Изм, для заказа"]
        upload = self.upload_xlsx(
            supplier,
            [
                headers,
                ['Масло 72,5% 180гр ТМ "АМК", шт', "шт", 113.62],
                ['Масло 72,5% 180гр ТМ "Внуковское", шт', "шт", 157.59],
                ['Масло 72,5% 180гр ТМ "Сохраняя", шт', "шт", 165.47],
            ],
        )
        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["products_created"], 3)
        self.assertEqual(SupplierProduct.objects.count(), 3)

    def test_reupload_same_file_with_raw_name_matching_zero_changes(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        headers = ["Наименование товара", "Ед.Изм. для заказа", "Цена за Ед,Изм, для заказа"]
        rows = [
            headers,
            ['Масло 72,5% 180гр ТМ "АМК", шт', "шт", 113.62],
            ['Масло 72,5% 180гр ТМ "Внуковское", шт', "шт", 157.59],
            ['Масло 72,5% 180гр ТМ "Сохраняя", шт', "шт", 165.47],
        ]
        first_upload = self.upload_xlsx(supplier, rows)
        self.client.post(self.normalize_url(first_upload), {"use_ai": False, "run_async": False}, format="json")

        second_upload = self.upload_xlsx(supplier, rows)
        second_response = self.client.post(
            self.normalize_url(second_upload), {"use_ai": False, "run_async": False}, format="json"
        )
        data = second_response.json()
        self.assertEqual(data["products_created"], 0)
        self.assertEqual(data["prices_created"], 0)
        self.assertEqual(data["items_matched"], 3)
        self.assertEqual(SupplierProduct.objects.count(), 3)
        self.assertEqual(SupplierProductPrice.objects.filter(status=PriceStatus.ACTIVE).count(), 3)
        self.assertEqual(SupplierProductPrice.objects.filter(status=PriceStatus.SUPERSEDED).count(), 0)

    def test_reupload_one_changed_price_shows_exactly_one_update(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        headers = ["Наименование товара", "Ед.Изм. для заказа", "Цена за Ед,Изм, для заказа"]
        first_upload = self.upload_xlsx(
            supplier,
            [
                headers,
                ["Товар А, шт", "шт", 100.00],
                ["Товар Б, шт", "шт", 200.00],
                ["Товар В, шт", "шт", 300.00],
            ],
        )
        self.client.post(self.normalize_url(first_upload), {"use_ai": False, "run_async": False}, format="json")

        second_upload = self.upload_xlsx(
            supplier,
            [
                headers,
                ["Товар А, шт", "шт", 100.00],
                ["Товар Б, шт", "шт", 250.00],
                ["Товар В, шт", "шт", 300.00],
            ],
        )
        response = self.client.post(
            self.normalize_url(second_upload), {"use_ai": False, "run_async": False}, format="json"
        )
        data = response.json()
        self.assertEqual(data["items_total"], 3)
        self.assertEqual(data["products_created"], 0)
        self.assertEqual(data["prices_created"], 1)
        self.assertEqual(data["items_matched"], 2)

    def test_supplier_name_not_overwritten_on_update(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        headers = ["Наименование товара", "Ед.Изм. для заказа", "Цена за Ед,Изм, для заказа"]
        first_upload = self.upload_xlsx(
            supplier,
            [headers, ["Товар Оригинал, шт", "шт", 100.00]],
        )
        self.client.post(self.normalize_url(first_upload), {"use_ai": False, "run_async": False}, format="json")

        product = SupplierProduct.objects.get()
        original_name = product.supplier_name
        self.assertIn("Оригинал", original_name)

        second_upload = self.upload_xlsx(
            supplier,
            [headers, ["Товар Оригинал, шт", "шт", 150.00]],
        )
        self.client.post(self.normalize_url(second_upload), {"use_ai": False, "run_async": False}, format="json")

        product.refresh_from_db()
        self.assertEqual(product.supplier_name, original_name)

    def test_brand_extracted_and_saved_on_create(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        headers = ["Наименование товара", "Ед.Изм. для заказа", "Цена за Ед,Изм, для заказа"]
        upload = self.upload_xlsx(
            supplier,
            [headers, ['Кунжут черный ТМ "Yakimal", 1кг 1/15, шт', "шт", 850.00]],
        )
        self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")
        product = SupplierProduct.objects.get()
        self.assertIsNotNone(product.brand)
        self.assertEqual(product.brand.canonical_name, "Yakimal")

    def test_brand_saved_on_matched_reupload(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        headers = ["Наименование товара", "Ед.Изм. для заказа", "Цена за Ед,Изм, для заказа"]
        rows = [headers, ['Кунжут черный ТМ "Yakimal", 1кг 1/15, шт', "шт", 850.00]]
        first_upload = self.upload_xlsx(supplier, rows)
        self.client.post(self.normalize_url(first_upload), {"use_ai": False, "run_async": False}, format="json")

        product = SupplierProduct.objects.get()
        self.assertIsNotNone(product.brand)
        self.assertEqual(product.brand.canonical_name, "Yakimal")

        second_upload = self.upload_xlsx(supplier, rows)
        response = self.client.post(
            self.normalize_url(second_upload), {"use_ai": False, "run_async": False}, format="json"
        )
        self.assertEqual(response.json()["items_matched"], 1)

        product.refresh_from_db()
        self.assertIsNotNone(product.brand)
        self.assertEqual(product.brand.canonical_name, "Yakimal")
        self.assertEqual(Brand.objects.filter(canonical_name="Yakimal").count(), 1)

    @override_settings(AI_NORMALIZATION_CHUNK_SIZE=2)
    def test_backend_runs_full_ai_pass_in_chunks_after_python_parse(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(
            supplier,
            "\n".join(
                [
                    "Наименование;Цена;Ед",
                    '"Кунжут черный жареный 1кг";850;шт',
                    '"Соус томатный 500 г";120;шт',
                    '"Лапша соба 400 г";73;шт',
                ]
            ),
        )

        def ai_response(**kwargs):
            return {
                "products": [
                    {
                        "row_number": product["row_number"],
                        "normalized_name": f"ai {product['normalized_name']}",
                        "price": str(product["price"]),
                        "currency": "RUB",
                        "unit_code": product["unit_code"],
                        "confidence": 0.9900,
                        "warnings": [],
                    }
                    for product in kwargs["products"]
                ]
            }

        with patch("api.v1.suppliers.normalization._request_ai_normalization", side_effect=ai_response) as ai_mock:
            response = self.client.post(
                self.normalize_url(upload),
                {"ai_mode": "full", "confirm_full": True, "run_async": False},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ai_mock.call_count, 2)
        self.assertEqual([len(call.kwargs["products"]) for call in ai_mock.call_args_list], [2, 1])
        normalization = response.json()["normalization"]
        self.assertEqual(normalization["ai_mode"], "full")
        self.assertEqual(normalization["rows_total"], 3)
        self.assertEqual(normalization["rows_ambiguous"], 0)
        self.assertEqual(normalization["rows_ai_requested"], 3)
        self.assertEqual(normalization["rows_ai_resolved"], 3)
        self.assertEqual(normalization["ai_chunks_total"], 2)
        self.assertEqual(normalization["ai_chunks_succeeded"], 2)
        self.assertEqual(normalization["ai_chunks_failed"], 0)
        self.assertEqual(SupplierProduct.objects.count(), 3)
        self.assertEqual(SupplierProduct.objects.filter(normalized_name__startswith="ai ").count(), 3)

    @override_settings(AI_NORMALIZATION_DEFAULT_MODE="ambiguous")
    def test_backend_defaults_to_ambiguous_ai_mode(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут черный жареный 1кг";850;шт\n')

        with patch("api.v1.suppliers.normalization._request_ai_normalization") as ai_mock:
            response = self.client.post(self.normalize_url(upload), {"run_async": False}, format="json")

        self.assertEqual(response.status_code, 200)
        ai_mock.assert_not_called()
        normalization = response.json()["normalization"]
        self.assertEqual(normalization["ai_mode"], "ambiguous")
        self.assertEqual(normalization["rows_ai_requested"], 0)

    @override_settings(
        AI_NORMALIZATION_DEFAULT_MODE="full", AI_NORMALIZATION_FULL_MAX_ROWS=10, AI_NORMALIZATION_LOCAL_MAX_ROWS=10
    )
    def test_backend_uses_configured_default_ai_mode_when_ai_mode_is_omitted(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут черный жареный 1кг";850;шт\n')

        def ai_response(**kwargs):
            return {
                "schema_version": "supplier_price_ai.v1",
                "processing_version": "test-ai",
                "supplier_id": str(supplier.id),
                "upload_id": str(upload.id),
                "source_channel": "file",
                "supplier_name": supplier.display_name,
                "products": [
                    {
                        "row_number": product["row_number"],
                        "normalized_name": f"ai {product['normalized_name']}",
                        "price": str(product["price"]),
                        "currency": "RUB",
                        "unit_code": product["unit_code"],
                        "confidence": 0.9900,
                        "warnings": [],
                    }
                    for product in kwargs["products"]
                ],
            }

        with patch("api.v1.suppliers.normalization._request_ai_normalization", side_effect=ai_response) as ai_mock:
            response = self.client.post(
                self.normalize_url(upload), {"confirm_full": True, "run_async": False}, format="json"
            )

        self.assertEqual(response.status_code, 200)
        ai_mock.assert_called_once()
        normalization = response.json()["normalization"]
        self.assertEqual(normalization["ai_mode"], "full")
        self.assertEqual(normalization["rows_ai_requested"], 1)
        self.assertEqual(normalization["rows_ai_resolved"], 1)

    def test_use_ai_false_does_not_call_ai(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена\n"Соус рыбный";390\n')

        with patch("api.v1.suppliers.normalization._request_ai_normalization") as ai_mock:
            response = self.client.post(
                self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json"
            )

        self.assertEqual(response.status_code, 200)
        ai_mock.assert_not_called()
        self.assertEqual(response.json()["normalization"]["ai_mode"], "off")

    def test_backend_normalize_can_enqueue_async_job(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')

        with patch("api.v1.suppliers.tasks.normalize_supplier_price_upload_task.delay") as delay_mock:
            delay_mock.return_value.id = "celery-task-1"
            response = self.client.post(
                self.normalize_url(upload),
                {"use_ai": False, "run_async": True},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertTrue(payload["accepted"])
        self.assertTrue(payload["run_async"])
        self.assertEqual(payload["celery_task_id"], "celery-task-1")
        self.assertEqual(payload["status"], UploadStatus.PROCESSING)
        delay_mock.assert_called_once()
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.PROCESSING)
        self.assertEqual(upload.raw_payload["normalization"]["celery_task_id"], "celery-task-1")
        self.assertFalse(SupplierProduct.objects.exists())

    def test_upload_detail_exposes_cached_normalization_result_for_polling(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')

        normalize_response = self.client.post(
            self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json"
        )
        detail_response = self.client.get(self.detail_url(upload))

        self.assertEqual(normalize_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        payload = detail_response.json()
        self.assertEqual(payload["upload_id"], str(upload.id))
        self.assertEqual(payload["status"], UploadStatus.APPLIED)
        self.assertEqual(payload["items_total"], 1)
        self.assertEqual(payload["products_created"], 1)
        self.assertEqual(payload["products_updated"], 0)
        self.assertEqual(payload["prices_created"], 1)
        self.assertEqual(payload["needs_review"], 0)
        self.assertEqual(payload["normalization"]["status"], UploadStatus.APPLIED)
        self.assertEqual(payload["normalization"]["result"]["products_created"], 1)
        self.assertEqual(payload["ai_cleanup"], {})

    def test_ai_cleanup_rejects_before_primary_normalization(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')

        response = self.client.post(self.ai_cleanup_url(upload), {"scope": "all"}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_ai_cleanup_can_enqueue_after_primary_normalization(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')
        normalize_response = self.client.post(
            self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json"
        )
        self.assertEqual(normalize_response.status_code, 200)

        with patch("api.v1.suppliers.tasks.ai_cleanup_supplier_price_upload_task.apply_async") as apply_async_mock:
            apply_async_mock.return_value.id = "ai-cleanup-task-1"
            response = self.client.post(self.ai_cleanup_url(upload), {"scope": "all"}, format="json")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["celery_task_id"], "ai-cleanup-task-1")
        self.assertEqual(payload["ai_cleanup"]["status"], "queued")
        self.assertEqual(payload["ai_cleanup"]["scope"], "all")
        apply_async_mock.assert_called_once()
        self.assertEqual(apply_async_mock.call_args.kwargs["queue"], "ai_normalization")

    def test_ai_cleanup_active_job_blocks_duplicate_request(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')
        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")
        self.assertEqual(response.status_code, 200)
        upload.refresh_from_db()
        raw_payload = upload.raw_payload
        raw_payload["normalization"]["ai_cleanup"] = {"status": "processing", "scope": "all"}
        upload.raw_payload = raw_payload
        upload.save(update_fields=["raw_payload", "updated_at"])

        response = self.client.post(self.ai_cleanup_url(upload), {"scope": "all"}, format="json")

        self.assertEqual(response.status_code, 409)

    @override_settings(AI_CLEANUP_MAX_IN_FLIGHT=1)
    def test_ai_cleanup_load_shedding_when_queue_is_overloaded(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        first_upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')
        second_upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            raw_payload={
                "kind": "text",
                "text": "Лапша соба 73 руб",
                "normalization": {"ai_cleanup": {"status": "queued", "scope": "all"}},
            },
            status=UploadStatus.APPLIED,
        )
        self.assertIsNotNone(second_upload)
        normalize_response = self.client.post(
            self.normalize_url(first_upload), {"use_ai": False, "run_async": False}, format="json"
        )
        self.assertEqual(normalize_response.status_code, 200)

        response = self.client.post(self.ai_cleanup_url(first_upload), {"scope": "all"}, format="json")

        self.assertEqual(response.status_code, 503)
        first_upload.refresh_from_db()
        self.assertEqual(first_upload.raw_payload["normalization"]["ai_cleanup"]["status"], "rejected_overload")

    def test_ai_cleanup_second_pass_updates_existing_product_without_duplicate(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')
        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")
        self.assertEqual(response.status_code, 200)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.normalized_name, "кунжут")

        def ai_response(**kwargs):
            return {
                "products": [
                    {
                        "row_number": product["row_number"],
                        "raw_name": product["raw_name"],
                        "normalized_name": "кунжут очищенный",
                        "price": str(product["price"]),
                        "currency": "RUB",
                        "unit_code": product["unit_code"],
                        "confidence": 0.9900,
                        "comment": "AI cleanup уточнил название",
                        "warnings": [],
                    }
                    for product in kwargs["products"]
                ]
            }

        from api.v1.suppliers.services import _run_supplier_price_upload_ai_cleanup

        with patch("api.v1.suppliers.normalization._request_ai_normalization", side_effect=ai_response) as ai_mock:
            _run_supplier_price_upload_ai_cleanup(upload=upload, scope="all", force=False)

        self.assertEqual(ai_mock.call_count, 1)
        self.assertEqual(SupplierProduct.objects.count(), 1)
        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.normalized_name, "кунжут очищенный")
        upload.refresh_from_db()
        cleanup = upload.raw_payload["normalization"]["ai_cleanup"]
        self.assertEqual(cleanup["status"], "applied")
        self.assertEqual(cleanup["rows_requested"], 1)
        self.assertEqual(cleanup["rows_resolved"], 1)

    def test_ai_cleanup_cannot_change_price_or_multiplicity(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед;Кратность\n"Кунжут 1кг";850;шт;5\n')
        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")
        self.assertEqual(response.status_code, 200)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.current_price, Decimal("850"))
        self.assertEqual(supplier_product.delivery_multiple, Decimal("5"))

        def ai_response(**kwargs):
            return {
                "products": [
                    {
                        "row_number": product["row_number"],
                        "raw_name": product["raw_name"],
                        "normalized_name": product["normalized_name"],
                        "price": "1.00",
                        "min_order_qty": "999",
                        "multiplicity": "999",
                        "currency": "RUB",
                        "unit_code": product["unit_code"],
                        "confidence": 0.99,
                        "comment": "AI пытается подделать цену и кратность",
                        "warnings": [],
                    }
                    for product in kwargs["products"]
                ]
            }

        from api.v1.suppliers.services import _run_supplier_price_upload_ai_cleanup

        with patch("api.v1.suppliers.normalization._request_ai_normalization", side_effect=ai_response):
            _run_supplier_price_upload_ai_cleanup(upload=upload, scope="all", force=False)

        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.current_price, Decimal("850"))
        self.assertEqual(supplier_product.delivery_multiple, Decimal("5"))

    def test_backend_normalize_accepts_async_alias(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')

        with patch("api.v1.suppliers.tasks.normalize_supplier_price_upload_task.delay") as delay_mock:
            delay_mock.return_value.id = "celery-task-async"
            response = self.client.post(
                self.normalize_url(upload),
                {"use_ai": False, "async": True},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        delay_mock.assert_called_once()

    @override_settings(DEBUG=True, AI_NORMALIZATION_FULL_MAX_ROWS=200, AI_NORMALIZATION_LOCAL_MAX_ROWS=1000)
    def test_full_ai_mode_large_upload_is_rejected_in_debug(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        rows = ["Наименование;Цена;Ед"]
        rows.extend(f'"Товар {index} 1кг";{index};шт' for index in range(1, 835))
        upload = self.upload_csv(supplier, "\n".join(rows))

        with patch("api.v1.suppliers.normalization._request_ai_normalization") as ai_mock:
            response = self.client.post(
                self.normalize_url(upload),
                {"ai_mode": "full", "confirm_full": True, "run_async": False},
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        ai_mock.assert_not_called()
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.FAILED)
        self.assertIn("Full AI normalization is limited", upload.error_message)

    @override_settings(AI_NORMALIZATION_CHUNK_SIZE=2)
    def test_backend_stops_ai_chunks_after_credentials_error(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(
            supplier,
            "\n".join(
                [
                    "Наименование;Цена",
                    '"Соус рыбный";390',
                    '"Соус устричный";410',
                    '"Соус соевый";210',
                ]
            ),
        )

        with patch(
            "api.v1.suppliers.normalization._request_ai_normalization",
            side_effect=SupplierPriceAIError(
                "AI normalization credentials are not configured.",
                code=AI_ERROR_CREDENTIALS_NOT_CONFIGURED,
                retryable=False,
            ),
        ) as ai_mock:
            response = self.client.post(
                self.normalize_url(upload),
                {"ai_mode": "full", "confirm_full": True, "run_async": False},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ai_mock.call_count, 1)
        normalization = response.json()["normalization"]
        self.assertEqual(normalization["ai_chunks_total"], 2)
        self.assertEqual(normalization["ai_chunks_failed"], 1)
        self.assertEqual(normalization["ai_error_codes"], ["credentials_not_configured"])
        self.assertEqual(normalization["rows_ai_resolved"], 0)

    @override_settings(AI_NORMALIZATION_CHUNK_SIZE=2, AI_NORMALIZATION_MAX_RETRIES=3)
    def test_401_403_errors_are_not_retried(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(
            supplier,
            "\n".join(
                [
                    "Наименование;Цена",
                    '"Соус рыбный";390',
                    '"Соус устричный";410',
                    '"Соус соевый";210',
                ]
            ),
        )

        with patch(
            "api.v1.suppliers.normalization._request_ai_normalization",
            side_effect=SupplierPriceAIError(
                "AI normalization HTTP 401 Unauthorized.",
                code=AI_ERROR_UNAUTHORIZED_401,
                http_status=401,
                retryable=False,
            ),
        ) as ai_mock:
            response = self.client.post(
                self.normalize_url(upload),
                {"ai_mode": "full", "confirm_full": True, "run_async": False},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ai_mock.call_count, 1)
        self.assertEqual(response.json()["normalization"]["ai_error_codes"], ["unauthorized_401"])

    @override_settings(AI_NORMALIZATION_MAX_RETRIES=1)
    def test_timeout_is_retried_according_to_max_retries(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена\n"Соус рыбный";390\n')
        timeout_error = SupplierPriceAIError(
            "AI normalization request timed out.",
            code=AI_ERROR_TIMEOUT,
            retryable=True,
        )
        ai_payload = {
            "products": [
                {
                    "row_number": 2,
                    "raw_name": "Соус рыбный",
                    "normalized_name": "соус рыбный",
                    "price": "390.00",
                    "currency": "RUB",
                    "unit_code": "шт",
                    "confidence": 0.9500,
                    "warnings": [],
                }
            ]
        }

        with (
            patch(
                "api.v1.suppliers.normalization._request_ai_normalization", side_effect=[timeout_error, ai_payload]
            ) as ai_mock,
            patch("api.v1.suppliers.normalization.time.sleep") as sleep_mock,
        ):
            response = self.client.post(
                self.normalize_url(upload), {"ai_mode": "ambiguous", "run_async": False}, format="json"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ai_mock.call_count, 2)
        sleep_mock.assert_called_once()
        self.assertEqual(response.json()["normalization"]["ai_retries"], 1)

    @override_settings(OPENAI_API_KEY="test-key")
    def test_openai_sdk_error_is_classified_with_status(self):
        supplier = self.create_supplier()
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            raw_payload={"kind": "text", "text": "Соус рыбный 390"},
        )

        class FakeOpenAIStatusError(Exception):
            status_code = 403

        with patch(
            "api.v1.suppliers.ai_client.request_ai_normalization_payload",
            side_effect=FakeOpenAIStatusError("forbidden"),
        ):
            with self.assertRaises(SupplierPriceAIError) as context:
                _request_ai_normalization(
                    upload=upload,
                    rows_by_number={},
                    products=[{"row_number": 1, "raw_name": "Соус рыбный", "confidence": Decimal("0.5000")}],
                )

        self.assertEqual(context.exception.code, AI_ERROR_FORBIDDEN_403)
        self.assertEqual(context.exception.http_status, 403)
        self.assertIn("forbidden", str(context.exception))

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai",
        AI_NORMALIZATION_USE_INSTRUCTOR=False,
        OPENAI_API_KEY="test-key",
    )
    def test_ai_client_json_fallback_validates_plain_chat_json(self):
        supplier = self.create_supplier()
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            raw_payload={"kind": "text", "text": "Соус рыбный 390"},
        )

        class FakeMessage:
            content = '{"products":[{"row_number":1,"raw_name":"Соус рыбный","normalized_name":"соус рыбный","price":390,"currency":"RUB","unit_code":"шт","confidence":0.95,"warnings":[]}]}'

        class FakeChoice:
            message = FakeMessage()

        class FakeCompletion:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **kwargs):
                return FakeCompletion()

        class FakeChat:
            completions = FakeCompletions()

        class FakeOpenAI:
            chat = FakeChat()

        with patch("api.v1.suppliers.ai_client.OpenAI", return_value=FakeOpenAI()):
            payload = _request_ai_normalization(
                upload=upload,
                rows_by_number={},
                products=[{"row_number": 1, "raw_name": "Соус рыбный", "confidence": Decimal("0.5000")}],
            )

        self.assertEqual(payload["products"][0]["normalized_name"], "соус рыбный")
        self.assertEqual(payload["products"][0]["unit_code"], "шт")

    @override_settings(
        AI_NORMALIZATION_PROVIDER="anthropic",
        OPENAI_API_KEY="test-key",
        OPENAI_BASE_URL="https://aiprimetech.io/v1",
        OPENAI_NORMALIZATION_MODEL="claude-haiku-4-5",
    )
    def test_ai_client_anthropic_provider_uses_messages_api(self):
        supplier = self.create_supplier()
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            raw_payload={"kind": "text", "text": "Соус рыбный 390"},
        )

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return (
                    '{"content":[{"type":"text","text":"{\\"products\\":[{\\"row_number\\":1,'
                    '\\"raw_name\\":\\"Soos\\",\\"normalized_name\\":\\"соус рыбный\\",'
                    '\\"price\\":390,\\"currency\\":\\"RUB\\",\\"unit_code\\":\\"шт\\",'
                    '\\"confidence\\":0.95,\\"warnings\\":[]}]}"}]}'
                ).encode("utf-8")

        with patch("api.v1.suppliers.ai_client.urllib.request.urlopen", return_value=FakeResponse()) as urlopen_mock:
            payload = _request_ai_normalization(
                upload=upload,
                rows_by_number={},
                products=[{"row_number": 1, "raw_name": "Соус рыбный", "confidence": Decimal("0.5000")}],
            )

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.full_url, "https://aiprimetech.io/v1/messages")
        self.assertEqual(payload["products"][0]["normalized_name"], "соус рыбный")

    @override_settings(
        AI_NORMALIZATION_PROVIDER="anthropic",
        OPENAI_API_KEY="test-key",
        OPENAI_BASE_URL="https://aiprimetech.io/v1",
        OPENAI_NORMALIZATION_MODEL="claude-haiku-4-5",
    )
    def test_ai_client_anthropic_invalid_json_is_classified(self):
        supplier = self.create_supplier()
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            raw_payload={"kind": "text", "text": "Соус рыбный 390"},
        )

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"content":[{"type":"text","text":"{\\"products\\":["}]}'

        with patch("api.v1.suppliers.ai_client.urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertRaises(SupplierPriceAIError) as context:
                _request_ai_normalization(
                    upload=upload,
                    rows_by_number={},
                    products=[{"row_number": 1, "raw_name": "Соус рыбный", "confidence": Decimal("0.5000")}],
                )

        self.assertEqual(context.exception.code, AI_ERROR_INVALID_JSON)

    @override_settings(AI_NORMALIZATION_AUTHORIZATION="Bearer", AI_NORMALIZATION_API_KEY="test-key")
    def test_incomplete_authorization_header_falls_back_to_api_key(self):
        self.assertEqual(_ai_authorization_header(), "Bearer test-key")

    def test_backend_sends_only_ambiguous_rows_to_ai_fallback(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(
            supplier,
            'Наименование;Цена\n"Соус рыбный";390\n',
        )

        with patch("api.v1.suppliers.normalization._request_ai_normalization") as ai_mock:
            ai_mock.return_value = {
                "products": [
                    {
                        "row_number": 2,
                        "raw_name": "Соус рыбный",
                        "normalized_name": "соус рыбный",
                        "price": "390.00",
                        "currency": "RUB",
                        "unit_code": "шт",
                        "package_info": None,
                        "volume": None,
                        "volume_unit": None,
                        "weight": None,
                        "weight_unit": None,
                        "min_order_qty": None,
                        "multiplicity": None,
                        "supplier_sku": None,
                        "category_path": [],
                        "comment": None,
                        "availability_status": "available",
                        "confidence": 0.9500,
                        "warnings": [],
                    }
                ]
            }

            response = self.client.post(
                self.normalize_url(upload), {"ai_mode": "ambiguous", "run_async": False}, format="json"
            )

        self.assertEqual(response.status_code, 200)
        ai_mock.assert_called_once()
        self.assertEqual(len(ai_mock.call_args.kwargs["products"]), 1)
        self.assertEqual(response.json()["normalization"]["ai_mode"], "ambiguous")
        self.assertEqual(response.json()["normalization"]["rows_ai_requested"], 1)
        self.assertEqual(response.json()["normalization"]["rows_ai_resolved"], 1)
        self.assertEqual(response.json()["needs_review"], 0)
        self.assertEqual(SupplierProduct.objects.get().normalized_name, "соус рыбный")

    def test_file_upload_intake_reuses_existing_upload_for_same_content(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        content = 'Наименование;Цена;Ед\n"Брокколи 1/10кг, шт";1815.09;шт\n'

        first_upload = self.upload_csv(supplier, content)
        second_upload = self.upload_csv(supplier, content)

        self.assertEqual(second_upload.id, first_upload.id)
        self.assertEqual(SupplierPriceUpload.objects.count(), 1)
        self.assertEqual(
            first_upload.raw_payload["metadata"]["content_sha256"],
            second_upload.raw_payload["metadata"]["content_sha256"],
        )

        first_response = self.client.post(
            self.normalize_url(first_upload), {"use_ai": False, "run_async": False}, format="json"
        )
        second_response = self.client.post(
            self.normalize_url(second_upload), {"use_ai": False, "run_async": False}, format="json"
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.json()["products_created"], 1)
        self.assertEqual(second_response.json()["items_total"], 1)
        self.assertEqual(second_response.json()["products_created"], 0)
        self.assertEqual(second_response.json()["products_updated"], 0)
        self.assertEqual(second_response.json()["prices_created"], 0)
        self.assertTrue(second_response.json()["cached"])
        self.assertEqual(SupplierProduct.objects.count(), 1)
        self.assertEqual(SupplierProductPrice.objects.count(), 1)

    def test_force_reruns_normalization_instead_of_returning_cached_needs_review_result(self):
        supplier = self.create_supplier()
        self.authenticate()
        # 2026-08-09: "шт" теперь сеется миграцией core.0011_seed_base_units — парсер
        # по-прежнему распознаёт алиас "шт" (статичная UNIT_ALIASES_MAP, не зависит от
        # БД), но _ai_item_needs_review нуждается в РЕЗОЛВЕ этого кода в активный Unit
        # из БД (_resolve_ai_unit) — деактивируем его явно, чтобы воспроизвести "юнита
        # ещё нет", раз теперь он есть по умолчанию.
        Unit.objects.filter(code="шт").update(status=EntityStatus.INACTIVE)
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')

        first_response = self.client.post(
            self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json"
        )
        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()["needs_review"], 1)
        self.assertEqual(first_response.json()["products_created"], 0)

        # Unit becomes available later (e.g. реактивирован задним числом) — force=true
        # must not just replay the stale cached needs_review result.
        Unit.objects.filter(code="шт").update(status=EntityStatus.ACTIVE)
        second_response = self.client.post(
            self.normalize_url(upload), {"use_ai": False, "force": True, "run_async": False}, format="json"
        )

        self.assertEqual(second_response.status_code, 200)
        self.assertNotIn("cached", second_response.json())
        self.assertEqual(second_response.json()["needs_review"], 0)
        self.assertEqual(second_response.json()["products_created"], 1)
        self.assertEqual(SupplierProduct.objects.count(), 1)

    def test_processing_upload_cannot_be_normalized_again(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')
        upload.status = UploadStatus.PROCESSING
        upload.save(update_fields=["status", "updated_at"])

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 409)
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.PROCESSING)

    def test_failed_upload_requires_force_to_restart_normalization(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')
        upload.status = UploadStatus.FAILED
        upload.error_message = "previous failure"
        upload.save(update_fields=["status", "error_message", "updated_at"])

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 400)
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.FAILED)
        self.assertEqual(upload.error_message, "previous failure")

    def test_failed_upload_error_message_includes_original_failure_reason(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')
        upload.status = UploadStatus.FAILED
        upload.error_message = "Full AI normalization is limited to 200 rows; requested 386."
        upload.save(update_fields=["status", "error_message", "updated_at"])

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Full AI normalization is limited to 200 rows; requested 386.", str(response.json()))

    def test_failed_upload_can_restart_with_force(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(supplier, 'Наименование;Цена;Ед\n"Кунжут 1кг";850;шт\n')
        upload.status = UploadStatus.FAILED
        upload.error_message = "previous failure"
        upload.save(update_fields=["status", "error_message", "updated_at"])

        response = self.client.post(
            self.normalize_url(upload), {"use_ai": False, "force": True, "run_async": False}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.APPLIED)
        self.assertIsNone(upload.error_message)

    def test_package_decimal_format_does_not_use_scientific_notation(self):
        supplier = self.create_supplier()
        self.create_units()
        self.authenticate()
        upload = self.upload_csv(
            supplier,
            'Наименование;Цена;Ед\n"Белый гриб резаный 1/10кг, шт";8738.14;шт\n',
        )

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 200)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.package, "10 кг; 1/10")

    def test_backend_normalize_repairs_legacy_column_extracted_rows(self):
        supplier = self.create_supplier()
        self.create_units()
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            source_file_name="legacy.xlsx",
            raw_payload={
                "kind": "file",
                "extracted_rows": [
                    {
                        "row_number": 1,
                        "cells": {
                            "column_1": "Наименование товара",
                            "column_2": "Ед.Изм. для заказа",
                            "column_3": "Минимальная Кратность в заказе",
                            "column_4": "Цена за Ед,Изм, для заказа",
                        },
                    },
                    {
                        "row_number": 2,
                        "cells": {
                            "column_1": 'Кунжут черный жареный ТМ "Yakimal", 1кг 1/15, шт',
                            "column_2": "шт",
                            "column_3": "15",
                            "column_4": "850.75",
                        },
                    },
                ],
            },
        )
        SupplierPriceUploadItem.objects.create(
            upload=upload,
            row_number=1,
            raw_name="Наименование товара",
            status=UploadItemStatus.NEEDS_REVIEW,
        )
        self.authenticate()

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items_total"], 1)
        self.assertEqual(response.json()["needs_review"], 0)
        self.assertEqual(SupplierPriceUploadItem.objects.filter(upload=upload).count(), 1)
        self.assertEqual(SupplierPriceUploadItem.objects.get(upload=upload).row_number, 2)
        self.assertEqual(SupplierProduct.objects.count(), 1)
        self.assertEqual(SupplierProductPrice.objects.get().price, Decimal("850.75"))

    def test_backend_normalize_repairs_legacy_column_extracted_rows_with_preamble_before_header(self):
        # Реальный путь ретрая: _rows_for_upload() берёт закешированные extracted_rows
        # ИЗ raw_payload и не перечитывает исходный файл, пока кеш непустой — значит
        # для уже сломанных (по старому _detect_header) загрузок чинить нужно именно
        # этот путь, а не только парсинг нового файла.
        supplier = self.create_supplier()
        self.create_units()
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            source_file_name="legacy_with_preamble.xlsx",
            raw_payload={
                "kind": "file",
                "extracted_rows": [
                    {
                        "row_number": 1,
                        "cells": {"column_1": "Самарская и Ульяновская область: +7 900 000-00-00"},
                    },
                    {
                        "row_number": 2,
                        "cells": {
                            "column_1": "Наименование товара",
                            "column_2": "Ед.Изм. для заказа",
                            "column_3": "Минимальная Кратность в заказе",
                            "column_4": "Цена за Ед,Изм, для заказа",
                        },
                    },
                    {
                        "row_number": 3,
                        "cells": {
                            "column_1": 'Кунжут черный жареный ТМ "Yakimal", 1кг 1/15, шт',
                            "column_2": "шт",
                            "column_3": "15",
                            "column_4": "850.75",
                        },
                    },
                ],
            },
        )
        SupplierPriceUploadItem.objects.create(
            upload=upload,
            row_number=1,
            raw_name="Самарская и Ульяновская область: +7 900 000-00-00",
            status=UploadItemStatus.NEEDS_REVIEW,
        )
        self.authenticate()

        response = self.client.post(self.normalize_url(upload), {"use_ai": False, "run_async": False}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items_total"], 1)
        self.assertEqual(response.json()["needs_review"], 0)
        self.assertEqual(SupplierPriceUploadItem.objects.filter(upload=upload).count(), 1)
        self.assertEqual(SupplierPriceUploadItem.objects.get(upload=upload).row_number, 3)
        self.assertEqual(SupplierProduct.objects.count(), 1)
        self.assertEqual(SupplierProductPrice.objects.get().price, Decimal("850.75"))


@override_settings(SERVICE_API_TOKEN="test-token")
class BotUserBindingResolveAPITests(APITestCase):
    # 2026-08-03: /api/v1/suppliers/bot-bindings/resolve (дубль этого же
    # BotUserBindingResolveView) удалён — он был "оставлен для совместимости"
    # с нодой старого n8n Telegram-воркфлоу; тот воркфлоу заменён Python-ботом
    # (autosnab_bots), который вызывает только канонический root_url ниже.
    root_url = "/api/v1/bot-user-bindings/resolve"
    old_url = "/api/v1/suppliers/bot-bindings/resolve"
    register_url = "/api/v1/bot-user-bindings/register"
    pending_url = "/api/v1/bot-user-bindings/pending-actions"
    state_url = "/api/v1/bot-user-states/current"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_rejects_without_service_token(self):
        response = self.client.get(self.root_url, {"channel": SourceChannel.TELEGRAM, "chat_id": "chat-1"})

        self.assertEqual(response.status_code, 403)

    def test_resolves_active_binding(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        binding = BotUserBinding.objects.create(
            supplier=supplier,
            channel=SourceChannel.TELEGRAM,
            chat_id="chat-1",
            external_user_id="user-1",
            username="supplier_user",
            status=EntityStatus.ACTIVE,
        )
        self.authenticate()

        response = self.client.get(self.root_url, {"channel": SourceChannel.TELEGRAM, "chat_id": "chat-1"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["binding"]["id"], str(binding.id))
        self.assertEqual(payload["binding"]["supplier_id"], str(supplier.id))
        self.assertEqual(payload["binding"]["supplier_name"], "Supplier")

    def test_resolves_account_binding_before_first_company(self):
        user = User.objects.create(
            email="account-only-binding@example.com",
            full_name="Account Only",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        binding = BotUserBinding.objects.create(
            supplier=None,
            user=user,
            channel=SourceChannel.TELEGRAM,
            chat_id="chat-without-company",
            external_user_id="user-without-company",
            status=EntityStatus.ACTIVE,
        )
        self.authenticate()

        response = self.client.get(
            self.root_url,
            {"channel": SourceChannel.TELEGRAM, "chat_id": "chat-without-company"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["binding"]["id"], str(binding.id))
        self.assertIsNone(payload["binding"]["supplier_id"])
        self.assertIsNone(payload["binding"]["supplier_name"])

    def test_resolves_root_endpoint_by_telegram_user_id(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        binding = BotUserBinding.objects.create(
            supplier=supplier,
            channel=SourceChannel.TELEGRAM,
            chat_id="chat-1",
            external_user_id="user-1",
            status=EntityStatus.ACTIVE,
        )
        self.authenticate()

        response = self.client.get(
            self.root_url,
            {"platform": SourceChannel.TELEGRAM, "telegram_user_id": "user-1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["exists"])
        self.assertEqual(response.json()["binding"]["id"], str(binding.id))

    def test_returns_404_for_unknown_binding(self):
        self.authenticate()

        response = self.client.get(self.root_url, {"channel": SourceChannel.TELEGRAM, "chat_id": "missing"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["exists"], False)
        self.assertEqual(response.json()["code"], "BOT_USER_BINDING_NOT_FOUND")

    def test_old_suppliers_prefixed_path_is_removed(self):
        self.authenticate()

        response = self.client.get(self.old_url, {"channel": SourceChannel.TELEGRAM, "chat_id": "chat-1"})

        self.assertEqual(response.status_code, 404)

    def test_creates_pending_action(self):
        self.authenticate()

        response = self.client.post(
            self.pending_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "action_type": "upload_price",
                "file_id": "file-1",
                "file_name": "price.xlsx",
                "message_id": "message-1",
                "status": "waiting_registration",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        pending_action = BotPendingAction.objects.get()
        self.assertEqual(pending_action.action_type, "upload_price")
        self.assertEqual(pending_action.status, "waiting_registration")

    def test_gets_latest_pending_action(self):
        old_action = BotPendingAction.objects.create(
            platform=SourceChannel.TELEGRAM,
            telegram_user_id="user-1",
            chat_id="chat-1",
            action_type="upload_price",
            file_id="old-file",
            status="waiting_registration",
        )
        latest_action = BotPendingAction.objects.create(
            platform=SourceChannel.TELEGRAM,
            telegram_user_id="user-1",
            chat_id="chat-1",
            action_type="upload_price",
            file_id="latest-file",
            status="waiting_registration",
        )
        self.authenticate()

        response = self.client.get(
            self.pending_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-1",
                "action_type": "upload_price",
                "status": "waiting_registration",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["exists"])
        self.assertEqual(response.json()["pending_action"]["id"], str(latest_action.id))
        self.assertNotEqual(response.json()["pending_action"]["id"], str(old_action.id))

    def test_registers_binding_by_link_token_and_returns_pending_action(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        link_token = SupplierBotLinkToken.objects.create(
            supplier=supplier, token="LINK-1", expires_at=timezone.now() + timezone.timedelta(minutes=30)
        )
        pending_action = BotPendingAction.objects.create(
            platform=SourceChannel.TELEGRAM,
            telegram_user_id="user-1",
            chat_id="chat-1",
            action_type="upload_price",
            file_id="file-1",
            file_name="price.xlsx",
            message_id="message-1",
            status="waiting_registration",
        )
        self.authenticate()

        response = self.client.post(
            self.register_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "username": "supplier_user",
                "first_name": "Ivan",
                "last_name": "Supplier",
                "link_token": "LINK-1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["binding"]["supplier_id"], str(supplier.id))
        self.assertEqual(payload["pending_action"]["id"], str(pending_action.id))
        self.assertEqual(BotUserBinding.objects.get().supplier_id, supplier.id)
        pending_action.refresh_from_db()
        self.assertEqual(pending_action.supplier_id, supplier.id)
        self.assertEqual(pending_action.status, "ready_for_processing")
        bot_state = BotUserState.objects.get()
        self.assertEqual(bot_state.state, "supplier_registered")
        self.assertEqual(bot_state.payload["supplier_id"], str(supplier.id))
        self.assertTrue(WorkflowEvent.objects.filter(event_type="supplier_registration_success").exists())
        link_token.refresh_from_db()
        self.assertTrue(link_token.used)

    def test_register_returns_existing_binding(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        SupplierBotLinkToken.objects.create(
            supplier=supplier, token="LINK-1", expires_at=timezone.now() + timezone.timedelta(minutes=30)
        )
        binding = BotUserBinding.objects.create(
            supplier=supplier,
            channel=SourceChannel.TELEGRAM,
            chat_id="chat-1",
            external_user_id="user-1",
            status=EntityStatus.ACTIVE,
        )
        self.authenticate()

        response = self.client.post(
            self.register_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "link_token": "LINK-1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["binding"]["id"], str(binding.id))
        self.assertEqual(BotUserBinding.objects.count(), 1)

    def test_register_rejects_unknown_link_token(self):
        self.authenticate()

        response = self.client.post(
            self.register_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "link_token": "missing",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "BOT_LINK_TOKEN_NOT_FOUND")
        self.assertEqual(response.json()["state"], "bot_link_invalid")
        self.assertFalse(BotUserBinding.objects.exists())
        self.assertEqual(BotUserState.objects.get().state, "bot_link_invalid")
        self.assertTrue(WorkflowEvent.objects.filter(event_type="supplier_registration_invalid_link_token").exists())

    def test_register_rejects_expired_link_token(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        SupplierBotLinkToken.objects.create(
            supplier=supplier, token="LINK-1", expires_at=timezone.now() - timezone.timedelta(minutes=1)
        )
        self.authenticate()

        response = self.client.post(
            self.register_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "link_token": "LINK-1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "BOT_LINK_TOKEN_EXPIRED")
        self.assertFalse(BotUserBinding.objects.exists())

    def test_register_rejects_already_used_link_token(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        SupplierBotLinkToken.objects.create(
            supplier=supplier,
            token="LINK-1",
            expires_at=timezone.now() + timezone.timedelta(minutes=30),
            used=True,
        )
        self.authenticate()

        response = self.client.post(
            self.register_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "link_token": "LINK-1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "BOT_LINK_TOKEN_USED")
        self.assertFalse(BotUserBinding.objects.exists())

    def test_register_rejects_inactive_supplier_link_token(self):
        supplier = Supplier.objects.create(
            legal_name="Supplier LLC", display_name="Supplier", status=EntityStatus.INACTIVE
        )
        SupplierBotLinkToken.objects.create(
            supplier=supplier, token="LINK-1", expires_at=timezone.now() + timezone.timedelta(minutes=30)
        )
        self.authenticate()

        response = self.client.post(
            self.register_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "link_token": "LINK-1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "SUPPLIER_INACTIVE")
        self.assertEqual(response.json()["state"], "error")
        self.assertFalse(BotUserBinding.objects.exists())
        self.assertEqual(BotUserState.objects.get().state, "error")

    def test_gets_idle_state_when_state_is_missing(self):
        self.authenticate()

        response = self.client.get(
            self.state_url,
            {"platform": SourceChannel.TELEGRAM, "chat_id": "chat-1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["exists"])
        self.assertEqual(response.json()["state"], "start")

    def test_sets_and_replaces_bot_user_state(self):
        self.authenticate()

        first_response = self.client.put(
            self.state_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "state": "bot_link_invalid",
                "payload": {"source": "register_button"},
            },
            format="json",
        )
        second_response = self.client.put(
            self.state_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "state": "registration_cancelled",
                "payload": {},
            },
            format="json",
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(BotUserState.objects.count(), 1)
        bot_state = BotUserState.objects.get()
        self.assertEqual(bot_state.state, "registration_cancelled")

    def test_deletes_bot_user_state(self):
        BotUserState.objects.create(
            platform=SourceChannel.TELEGRAM,
            telegram_user_id="user-1",
            chat_id="chat-1",
            state="bot_link_invalid",
        )
        self.authenticate()

        response = self.client.delete(f"{self.state_url}?platform={SourceChannel.TELEGRAM}&chat_id=chat-1")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(BotUserState.objects.exists())

    def test_deletes_pending_action(self):
        pending_action = BotPendingAction.objects.create(
            platform=SourceChannel.TELEGRAM,
            telegram_user_id="user-1",
            chat_id="chat-1",
            action_type="upload_price",
            file_id="file-1",
            status="waiting_registration",
        )
        self.authenticate()

        response = self.client.delete(f"{self.pending_url}/{pending_action.id}")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(BotPendingAction.objects.exists())

    def test_pending_action_post_reuses_same_file_id(self):
        self.authenticate()

        first_response = self.client.post(
            self.pending_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "action_type": "upload_price",
                "file_id": "file-1",
                "file_name": "old.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "status": "pending_registration",
            },
            format="json",
        )
        second_response = self.client.post(
            self.pending_url,
            {
                "platform": SourceChannel.TELEGRAM,
                "telegram_user_id": "user-1",
                "chat_id": "chat-1",
                "action_type": "upload_price",
                "file_id": "file-1",
                "file_name": "new.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "status": "pending_registration",
            },
            format="json",
        )

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(BotPendingAction.objects.count(), 1)
        pending_action = BotPendingAction.objects.get()
        self.assertEqual(pending_action.file_name, "new.xlsx")
        self.assertEqual(BotUserState.objects.get().state, "pending_price_upload")

    def test_updates_pending_action_status(self):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        pending_action = BotPendingAction.objects.create(
            platform=SourceChannel.TELEGRAM,
            telegram_user_id="user-1",
            chat_id="chat-1",
            action_type="upload_price",
            file_id="file-1",
            status="ready_for_processing",
        )
        self.authenticate()

        response = self.client.patch(
            f"{self.pending_url}/{pending_action.id}",
            {
                "status": "price_processing",
                "supplier_id": str(supplier.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        pending_action.refresh_from_db()
        self.assertEqual(pending_action.status, "price_processing")
        self.assertEqual(pending_action.supplier_id, supplier.id)
        self.assertEqual(BotUserState.objects.get().state, "price_processing")


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierPriceAIApplyAPITests(APITestCase):
    file_url = "/api/v1/suppliers/uploads"
    text_url = "/api/v1/suppliers/uploads/text"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier(self):
        return Supplier.objects.create(
            legal_name="Supplier LLC",
            display_name="Supplier",
        )

    def create_unit(self, code="kg"):
        return Unit.objects.create(
            code=code,
            name=code,
            kind=UnitKind.WEIGHT,
        )

    def create_upload(self, supplier=None):
        supplier = supplier or self.create_supplier()
        return SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.API,
            status=UploadStatus.RECEIVED,
            raw_payload={"kind": "text"},
        )

    def apply_url(self, upload):
        return f"/api/v1/suppliers/uploads/{upload.id}/apply-ai-result"

    def payload(self, *, unit, price="100.00", confidence="0.9500", normalized_name="milk"):
        return {
            "schema_version": "supplier_price_ai.v1",
            "processing_version": "2026-06-14",
            "supplier_name": "Supplier",
            "products": [
                {
                    "row_number": 1,
                    "raw_name": "Milk raw",
                    "normalized_name": normalized_name,
                    "price": price,
                    "unit_id": str(unit.id),
                    "package": "box",
                    "min_order_qty": "2.0000",
                    "supplier_sku": "sku-1",
                    "confidence": confidence,
                    "comment": "AI normalized",
                }
            ],
        }

    def test_rejects_apply_without_service_token(self):
        unit = self.create_unit()
        upload = self.create_upload()

        response = self.client.post(self.apply_url(upload), self.payload(unit=unit), format="json")

        self.assertEqual(response.status_code, 401)

    def test_supplier_cabinet_jwt_can_apply_own_upload(self):
        unit = self.create_unit()
        supplier = self.create_supplier()
        upload = self.create_upload(supplier=supplier)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.post(self.apply_url(upload), self.payload(unit=unit), format="json")

        self.assertNotEqual(response.status_code, 404)

    def test_supplier_cabinet_jwt_cannot_apply_other_suppliers_upload(self):
        unit = self.create_unit()
        own = self.create_supplier()
        other = self.create_supplier()
        other_upload = self.create_upload(supplier=other)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own))
        response = self.client.post(self.apply_url(other_upload), self.payload(unit=unit), format="json")

        self.assertEqual(response.status_code, 404)
        other_upload.refresh_from_db()
        self.assertEqual(other_upload.status, UploadStatus.RECEIVED)

    def test_rejects_invalid_schema(self):
        unit = self.create_unit()
        upload = self.create_upload()
        request_payload = self.payload(unit=unit)
        request_payload["schema_version"] = "wrong"
        self.authenticate()

        response = self.client.post(self.apply_url(upload), request_payload, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SupplierPriceUploadItem.objects.exists())
        self.assertFalse(SupplierProduct.objects.exists())

    def test_valid_payload_creates_upload_items(self):
        unit = self.create_unit()
        upload = self.create_upload()
        self.authenticate()

        response = self.client.post(self.apply_url(upload), self.payload(unit=unit), format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items_total"], 1)
        self.assertEqual(SupplierPriceUploadItem.objects.count(), 1)
        upload_item = SupplierPriceUploadItem.objects.get()
        self.assertEqual(upload_item.upload, upload)
        self.assertEqual(upload_item.raw_name, "Milk raw")
        self.assertEqual(upload_item.normalized_name, "milk")
        self.assertEqual(upload_item.parsed_price, Decimal("100.00"))
        self.assertEqual(upload_item.parsed_unit, unit)

    def test_valid_payload_creates_supplier_products(self):
        unit = self.create_unit()
        upload = self.create_upload()
        self.authenticate()

        response = self.client.post(self.apply_url(upload), self.payload(unit=unit), format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["products_created"], 1)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.supplier, upload.supplier)
        self.assertEqual(supplier_product.supplier_name, "Milk raw")
        self.assertEqual(supplier_product.normalized_name, "milk")
        self.assertEqual(supplier_product.unit, unit)
        self.assertEqual(supplier_product.last_upload, upload)
        self.assertEqual(SupplierProductPrice.objects.get().price, Decimal("100.00"))
        self.assertFalse(ProductEmbedding.objects.exists())

    def test_expanded_payload_fields_are_applied(self):
        supplier = self.create_supplier()
        unit, _ = Unit.objects.get_or_create(code="шт", defaults={"name": "Штука", "kind": UnitKind.PIECE})
        volume_unit, _ = Unit.objects.get_or_create(code="мл", defaults={"name": "Миллилитр", "kind": UnitKind.VOLUME})
        upload = self.create_upload(supplier=supplier)
        self.authenticate()

        response = self.client.post(
            self.apply_url(upload),
            {
                "schema_version": "supplier_price_ai.v1",
                "processing_version": "2026-06-17",
                "supplier_id": str(supplier.id),
                "upload_id": str(upload.id),
                "source_channel": SourceChannel.API,
                "supplier_name": "Supplier",
                "products": [
                    {
                        "row_number": 1,
                        "raw_name": "Coconut milk Aroy-D 400 ml",
                        "normalized_name": "coconut milk aroy-d 400 ml",
                        "price": "185.50",
                        "currency": "RUB",
                        "unit_code": "шт",
                        "package_info": "ж/б 400 мл",
                        "volume": "400.0000",
                        "volume_unit": "мл",
                        "min_order_qty": "1.0000",
                        "multiplicity": "6.0000",
                        "supplier_sku": "AROYD-400",
                        "availability_status": "available",
                        "confidence": "0.9400",
                        "warnings": [],
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        supplier_product = SupplierProduct.objects.get()
        self.assertEqual(supplier_product.package, "ж/б 400 мл")
        self.assertEqual(supplier_product.net_value, Decimal("400.0000"))
        self.assertEqual(supplier_product.net_unit, volume_unit)
        self.assertEqual(supplier_product.delivery_multiple, Decimal("6.0000"))
        self.assertEqual(supplier_product.unit, unit)
        self.assertEqual(SupplierProductPrice.objects.get().currency, "RUB")

    def test_price_history_works(self):
        supplier = self.create_supplier()
        unit = self.create_unit()
        upload = self.create_upload(supplier=supplier)
        supplier_product = SupplierProduct.objects.create(
            supplier=supplier,
            unit=unit,
            supplier_sku="sku-1",
            supplier_name="Milk old",
            normalized_name="milk",
            package="box",
            status=EntityStatus.ACTIVE,
        )
        old_price = SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price=Decimal("90.00"),
            status=PriceStatus.ACTIVE,
        )
        self.authenticate()

        response = self.client.post(self.apply_url(upload), self.payload(unit=unit, price="110.00"), format="json")

        self.assertEqual(response.status_code, 200)
        old_price.refresh_from_db()
        self.assertEqual(old_price.status, PriceStatus.SUPERSEDED)
        self.assertIsNotNone(old_price.valid_to)
        active_price = SupplierProductPrice.objects.get(
            supplier_product=supplier_product,
            status=PriceStatus.ACTIVE,
            valid_to__isnull=True,
        )
        self.assertEqual(active_price.price, Decimal("110.00"))

    def test_repeated_apply_is_idempotent_where_possible(self):
        unit = self.create_unit()
        upload = self.create_upload()
        request_payload = self.payload(unit=unit)
        self.authenticate()

        first_response = self.client.post(self.apply_url(upload), request_payload, format="json")
        second_response = self.client.post(self.apply_url(upload), request_payload, format="json")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(SupplierPriceUploadItem.objects.count(), 1)
        self.assertEqual(SupplierProduct.objects.count(), 1)
        self.assertEqual(SupplierProductPrice.objects.count(), 1)
        self.assertEqual(second_response.json()["products_created"], 0)
        self.assertEqual(second_response.json()["prices_created"], 0)

    def test_low_confidence_item_goes_to_needs_review(self):
        unit = self.create_unit()
        upload = self.create_upload()
        self.authenticate()

        response = self.client.post(
            self.apply_url(upload),
            self.payload(unit=unit, confidence="0.5000"),
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], UploadStatus.NEEDS_REVIEW)
        self.assertEqual(response.json()["needs_review"], 1)
        upload_item = SupplierPriceUploadItem.objects.get()
        self.assertEqual(upload_item.status, UploadItemStatus.NEEDS_REVIEW)
        self.assertFalse(SupplierProduct.objects.exists())
        self.assertFalse(SupplierProductPrice.objects.exists())

    def test_upload_detail_and_items_endpoints_return_review_summary(self):
        unit = self.create_unit()
        upload = self.create_upload()
        self.authenticate()
        apply_response = self.client.post(
            self.apply_url(upload),
            self.payload(unit=unit, confidence="0.5000"),
            format="json",
        )

        detail_response = self.client.get(f"/api/v1/suppliers/uploads/{upload.id}")
        items_response = self.client.get(
            f"/api/v1/suppliers/uploads/{upload.id}/items",
            {"status": UploadItemStatus.NEEDS_REVIEW},
        )

        self.assertEqual(apply_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()["items_total"], 1)
        self.assertEqual(detail_response.json()["items_needs_review"], 1)
        self.assertEqual(items_response.status_code, 200)
        self.assertEqual(items_response.json()["count"], 1)
        self.assertEqual(items_response.json()["results"][0]["status"], UploadItemStatus.NEEDS_REVIEW)

    def test_rejects_ai_payload_with_mismatched_supplier_id(self):
        unit = self.create_unit()
        upload = self.create_upload()
        request_payload = self.payload(unit=unit)
        request_payload["supplier_id"] = str(uuid4())
        self.authenticate()

        response = self.client.post(self.apply_url(upload), request_payload, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SupplierPriceUploadItem.objects.exists())

    def test_rejects_empty_text(self):
        supplier = self.create_supplier()
        self.authenticate()

        response = self.client.post(
            self.text_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.API,
                "text": "   ",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SupplierPriceUpload.objects.exists())

    def test_rejects_unsupported_file_type(self):
        supplier = self.create_supplier()
        source_file = SimpleUploadedFile("price.exe", b"binary", content_type="application/octet-stream")
        self.authenticate()

        response = self.client.post(
            self.file_url,
            {
                "supplier_id": str(supplier.id),
                "source_channel": SourceChannel.API,
                "source_file": source_file,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# Supplier list and ownership claim views (TASK-22)
# ---------------------------------------------------------------------------

from django.core.cache import cache  # noqa: E402
from django.test import TestCase as DjangoTestCase  # noqa: E402


class SuppliersListViewTests(DjangoTestCase):
    def _make_supplier(self, name, inn=None, status="active"):
        return Supplier.objects.create(
            display_name=name,
            legal_name=name,
            inn=inn,
            status=status,
        )

    def test_lists_active_suppliers(self):
        self._make_supplier("Поставщик А", inn="7707083893")
        self._make_supplier("Поставщик Б")
        response = self.client.get("/suppliers/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Поставщик А")
        self.assertContains(response, "Поставщик Б")

    def test_hides_inactive_suppliers(self):
        self._make_supplier("Архивный", status="inactive")
        response = self.client.get("/suppliers/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Архивный")

    def test_masks_inn(self):
        self._make_supplier("С ИНН", inn="7707083893")
        response = self.client.get("/suppliers/")
        # Masked version shown: inn[:4] + "*" * (len-6) + inn[-2:] → "7707****93" for 10-digit INN
        self.assertContains(response, "7707****93")
        # Full INN in data-inn attribute for JS search — that's intentional
        self.assertContains(response, 'data-inn="7707083893"')

    def test_empty_state(self):
        response = self.client.get("/suppliers/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ещё не добавлены")


class SupplierOwnershipRequestApprovalTests(DjangoTestCase):
    """approve_ownership_requests admin action — independent of supplier_claim_view (deleted 2026-07-15)."""

    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Тест Поставщик",
            legal_name="ООО Тест",
            inn="7707083893",
            status="active",
        )

    def test_approve_action_creates_supplier_user(self):
        from django.test import RequestFactory

        from suppliers.admin import approve_ownership_requests

        req_obj = SupplierOwnershipRequest.objects.create(
            supplier=self.supplier,
            full_name="Тест Владелец",
            email="owner_auto@test.ru",
            inn="7707083893",
            role_requested="owner",
        )
        factory = RequestFactory()
        admin_request = factory.get("/")
        approve_ownership_requests(None, admin_request, SupplierOwnershipRequest.objects.filter(pk=req_obj.pk))
        req_obj.refresh_from_db()
        self.assertEqual(req_obj.status, "approved")
        self.assertTrue(SupplierUser.objects.filter(supplier=self.supplier, role="owner").exists())

    def test_approve_action_reuses_existing_user(self):
        from django.test import RequestFactory

        from core.models import User, UserRole
        from suppliers.admin import approve_ownership_requests

        existing_user = User.objects.create(email="existing@test.ru", full_name="Уже есть", role=UserRole.SUPPLIER)
        req_obj = SupplierOwnershipRequest.objects.create(
            supplier=self.supplier,
            full_name="Уже есть",
            email="existing@test.ru",
            inn="7707083893",
            role_requested="owner",
        )
        factory = RequestFactory()
        admin_request = factory.get("/")
        approve_ownership_requests(None, admin_request, SupplierOwnershipRequest.objects.filter(pk=req_obj.pk))
        self.assertEqual(SupplierUser.objects.filter(supplier=self.supplier).count(), 1)
        self.assertEqual(SupplierUser.objects.get(supplier=self.supplier).user, existing_user)


class SupplierUserInviteCancelAdminActionTests(DjangoTestCase):
    """2026-08-07: не было способа отменить уже отправленное приглашение —
    Витя уткнулся, пытаясь сделать это через админку. Добавлен
    cancel_supplier_user_invites (тот же паттерн, что approve/reject
    ownership requests выше)."""

    def setUp(self):
        self.supplier = Supplier.objects.create(
            display_name="Тест Поставщик",
            legal_name="ООО Тест",
            inn="7707083893",
            status="active",
        )

    def _make_invite(self, *, status=SupplierUserInviteStatus.PENDING, token="tok-cancel-test"):
        return SupplierUserInvite.objects.create(
            supplier=self.supplier,
            target_email="invitee@test.ru",
            token=token,
            role="manager",
            status=status,
            expires_at=timezone.now() + timezone.timedelta(hours=72),
        )

    def test_cancels_pending_invite(self):
        from suppliers.admin import cancel_supplier_user_invites

        invite = self._make_invite()
        cancel_supplier_user_invites(None, None, SupplierUserInvite.objects.filter(pk=invite.pk))
        invite.refresh_from_db()
        self.assertEqual(invite.status, SupplierUserInviteStatus.CANCELLED)

    def test_does_not_touch_already_claimed_invite(self):
        from suppliers.admin import cancel_supplier_user_invites

        invite = self._make_invite(status=SupplierUserInviteStatus.CLAIMED, token="tok-cancel-claimed")
        cancel_supplier_user_invites(None, None, SupplierUserInvite.objects.filter(pk=invite.pk))
        invite.refresh_from_db()
        self.assertEqual(invite.status, SupplierUserInviteStatus.CLAIMED)


class SupplierAddViewTests(DjangoTestCase):
    url = "/suppliers/add/"

    def setUp(self):
        cache.clear()

    def test_get_returns_200(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Добавить компанию")

    def test_successful_submission_creates_supplier_and_request(self):
        response = self.client.post(
            self.url,
            {
                "company_name": "Новая Компания",
                "full_name": "Иван Иванов",
                "email": "ivan@test.ru",
                "inn": "7707083893",
            },
        )
        self.assertRedirects(response, "/suppliers/add/?added=1")
        follow = self.client.get("/suppliers/add/?added=1")
        self.assertContains(follow, "Заявка отправлена")
        supplier = Supplier.objects.get(inn="7707083893")
        self.assertEqual(supplier.status, "inactive")
        req = SupplierOwnershipRequest.objects.get(supplier=supplier)
        self.assertEqual(req.role_requested, "add_company")
        self.assertEqual(req.full_name, "Иван Иванов")

    def test_validation_requires_company_name(self):
        response = self.client.post(
            self.url,
            {
                "company_name": "",
                "full_name": "Иван",
                "email": "ivan@test.ru",
                "inn": "7707083893",
            },
        )
        self.assertContains(response, "Введите название компании")
        self.assertFalse(Supplier.objects.filter(inn="7707083893").exists())

    def test_validation_requires_valid_inn(self):
        response = self.client.post(
            self.url,
            {
                "company_name": "Компания",
                "full_name": "Иван",
                "email": "ivan@test.ru",
                "inn": "123",
            },
        )
        self.assertContains(response, "10 цифр")
        self.assertFalse(Supplier.objects.exists())

    def test_existing_supplier_by_inn_shows_redirect_message(self):
        Supplier.objects.create(display_name="Уже есть", inn="7707083893", status="active")
        response = self.client.post(
            self.url,
            {
                "company_name": "Другое название",
                "full_name": "Иван",
                "email": "ivan@test.ru",
                "inn": "7707083893",
            },
        )
        self.assertContains(response, "уже есть в базе")
        self.assertContains(response, "Уже есть")

    def test_duplicate_add_request_shows_warning(self):
        self.client.post(
            self.url,
            {
                "company_name": "Новая Компания",
                "full_name": "Иван Иванов",
                "email": "ivan@test.ru",
                "inn": "7707083893",
            },
        )
        response = self.client.post(
            self.url,
            {
                "company_name": "Новая Компания",
                "full_name": "Иван Иванов",
                "email": "ivan@test.ru",
                "inn": "7707083893",
            },
        )
        self.assertContains(response, "уже отправлена")
        self.assertEqual(Supplier.objects.filter(inn="7707083893").count(), 1)

    def test_invalid_inn_checksum_rejected(self):
        response = self.client.post(
            self.url,
            {
                "company_name": "Компания",
                "full_name": "Иван",
                "email": "ivan@test.ru",
                "inn": "1234567890",
            },
        )
        self.assertContains(response, "контрольной суммы")
        self.assertFalse(Supplier.objects.exists())

    def test_rate_limiting_blocks_add(self):
        from unittest.mock import patch

        with patch("api.v1.views._is_rate_limited", return_value=True):
            response = self.client.post(
                self.url,
                {
                    "company_name": "Компания",
                    "full_name": "Иван",
                    "email": "ivan@test.ru",
                    "inn": "7707083893",
                },
            )
        self.assertContains(response, "Слишком много попыток")


class SupplierProductsListProxyTests(APITestCase):
    """Tests for supplier_products_list_proxy view at /supplier/<id>/api/products/."""

    def setUp(self):
        self.supplier = Supplier.objects.create(
            legal_name="Test Supplier LLC",
            display_name="Test Supplier",
        )
        self.unit = Unit.objects.create(code="pcs", name="штука", kind=UnitKind.PIECE)

    def url(self, supplier_id=None):
        sid = supplier_id or self.supplier.id
        return f"/supplier/{sid}/api/products/"

    def make_product(self, *, name="Болт М8", price=None, category=None, brand=None, **kwargs):
        product = Product.objects.create(
            canonical_name=name,
            normalized_name=name.lower(),
            base_unit=self.unit,
        )
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            product=product,
            unit=self.unit,
            supplier_name=name,
            normalized_name=name.lower(),
            status=EntityStatus.ACTIVE,
            category=category,
            brand=brand,
            **kwargs,
        )
        if price is not None:
            SupplierProductPrice.objects.create(
                supplier_product=sp,
                price=Decimal(str(price)),
                status=PriceStatus.ACTIVE,
            )
        return sp

    def make_upload(self, status="applied"):
        return SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            status=status,
            source_channel=SourceChannel.TELEGRAM,
            uploaded_at=timezone.now(),
        )

    # --- basic ---

    def test_returns_200_for_existing_supplier(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)

    def test_returns_404_for_unknown_supplier(self):
        from uuid import uuid4

        response = self.client.get(self.url(uuid4()))
        self.assertEqual(response.status_code, 404)

    def test_returns_results_key(self):
        response = self.client.get(self.url())
        self.assertIn("results", response.json())

    def test_empty_when_no_products(self):
        response = self.client.get(self.url())
        self.assertEqual(response.json()["results"], [])

    # --- field completeness ---

    def test_returns_all_required_fields(self):
        self.make_product(price=99)
        response = self.client.get(self.url())
        item = response.json()["results"][0]
        expected_fields = {
            "id",
            "supplier_raw_name",
            "supplier_normalized_name",
            "current_price",
            "prev_price",
            "price_changed_at",
            "changed",
            "unit",
            "unit_id",
            "package",
            "min_order_qty",
            "delivery_multiple",
            "comment",
            "brand",
            "group",
            "subgroup",
            "product_type",
            "image_url",
            "category",
            "country",
            "system_sku",
        }
        self.assertEqual(expected_fields, set(item.keys()))

    def test_price_fields_populated(self):
        self.make_product(name="Гайка М10", price="150.50")
        response = self.client.get(self.url())
        item = response.json()["results"][0]
        self.assertEqual(item["current_price"], "150.50")

    def test_no_price_returns_null_current_price(self):
        self.make_product()
        response = self.client.get(self.url())
        item = response.json()["results"][0]
        self.assertIsNone(item["current_price"])

    def test_comment_field_returned(self):
        self.make_product(comment="Оригинальная деталь")
        response = self.client.get(self.url())
        self.assertEqual(response.json()["results"][0]["comment"], "Оригинальная деталь")

    def test_image_url_field_returned(self):
        self.make_product(image_url="https://example.com/img.jpg")
        response = self.client.get(self.url())
        self.assertEqual(response.json()["results"][0]["image_url"], "https://example.com/img.jpg")

    def test_brand_field_returned(self):
        brand = Brand.objects.create(canonical_name="Bosch", normalized_name="bosch")
        self.make_product(brand=brand)
        response = self.client.get(self.url())
        self.assertEqual(response.json()["results"][0]["brand"], "Bosch")

    def test_brand_empty_string_when_no_brand(self):
        self.make_product()
        response = self.client.get(self.url())
        self.assertEqual(response.json()["results"][0]["brand"], "")

    # --- category cascade ---

    def test_group_subgroup_product_type_from_3level_category(self):
        group = ProductCategory.objects.create(name="Крепёж", slug="krepezh")
        subgroup = ProductCategory.objects.create(name="Болты", slug="bolty", parent=group)
        product_type = ProductCategory.objects.create(name="Болты М8", slug="bolty-m8", parent=subgroup)
        self.make_product(category=product_type)

        item = self.client.get(self.url()).json()["results"][0]

        self.assertEqual(item["group"], "Крепёж")
        self.assertEqual(item["subgroup"], "Болты")
        self.assertEqual(item["product_type"], "Болты М8")
        self.assertEqual(item["category"], "Болты М8")

    def test_group_subgroup_empty_for_top_level_category(self):
        top = ProductCategory.objects.create(name="Запчасти", slug="zapchasti")
        self.make_product(category=top)

        item = self.client.get(self.url()).json()["results"][0]

        self.assertEqual(item["group"], "")
        self.assertEqual(item["subgroup"], "")
        self.assertEqual(item["product_type"], "Запчасти")

    def test_category_fields_empty_when_no_category(self):
        self.make_product()
        item = self.client.get(self.url()).json()["results"][0]
        self.assertEqual(item["group"], "")
        self.assertEqual(item["subgroup"], "")
        self.assertEqual(item["product_type"], "")
        self.assertEqual(item["category"], "")

    # --- prev_price / changed ---

    def test_changed_false_when_no_prev_price(self):
        self.make_product(price="50")
        item = self.client.get(self.url()).json()["results"][0]
        self.assertFalse(item["changed"])
        self.assertEqual(item["prev_price"], "")

    # --- filters ---

    def test_excludes_archived_products(self):
        self.make_product(name="Активный", price="10")
        archived = self.make_product(name="Архивный", price="20")
        archived.status = EntityStatus.ARCHIVED
        archived.save()

        ids = [r["id"] for r in self.client.get(self.url()).json()["results"]]

        self.assertNotIn(str(archived.id), ids)
        self.assertEqual(len(ids), 1)

    def test_only_returns_products_of_this_supplier(self):
        other_supplier = Supplier.objects.create(legal_name="Other LLC", display_name="Other")
        other_product = Product.objects.create(
            canonical_name="Чужой товар", normalized_name="чужой", base_unit=self.unit
        )
        SupplierProduct.objects.create(
            supplier=other_supplier,
            product=other_product,
            unit=self.unit,
            supplier_name="Чужой",
            normalized_name="чужой",
            status=EntityStatus.ACTIVE,
        )
        self.make_product(name="Свой товар")

        results = self.client.get(self.url()).json()["results"]

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["supplier_raw_name"], "Свой товар")

    # --- limit ---

    def test_custom_limit_respected(self):
        for i in range(10):
            self.make_product(name=f"Товар {i}")
        response = self.client.get(self.url(), {"limit": 3})
        self.assertEqual(len(response.json()["results"]), 3)

    def test_limit_capped_at_10000(self):
        response = self.client.get(self.url(), {"limit": 99999})
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Helpers shared by services tests
# ---------------------------------------------------------------------------


def _make_supplier(name="Supplier LLC"):
    return Supplier.objects.create(legal_name=name, display_name=name)


def _make_unit(code="pcs", kind=None):
    kind = kind or UnitKind.PIECE
    return Unit.objects.create(code=code, name=code, kind=kind)


def _xlsx_upload(rows, name="products.xlsx"):
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


IMPORT_ANALYZE_URL = "/api/v1/suppliers/products/import/analyze"


def _analyze_ready(client, upload):
    """analyze (202, обработка в очереди) -> фоновая обработка выполняется тут же (eager) -> GET сессии.

    Возвращает (ответ analyze, ответ GET сессии); у GET тело как у прежнего 201 от analyze."""
    with (
        override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True),
        patch("api.v1.suppliers.tasks.close_old_connections"),
    ):
        accepted = client.post(IMPORT_ANALYZE_URL, {"file": upload}, format="multipart")
    assert accepted.status_code == 202, accepted.content
    return accepted, client.get(accepted.json()["status_url"])


def _analyze_queued(client, upload):
    """analyze без выполнения очереди: сессия остаётся pending."""
    with patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay"):
        return client.post(IMPORT_ANALYZE_URL, {"file": upload}, format="multipart")


def _make_upload(supplier, *, status=UploadStatus.RECEIVED):
    return SupplierPriceUpload.objects.create(
        supplier=supplier,
        source_channel=SourceChannel.TELEGRAM,
        status=status,
        raw_payload={"kind": "text", "text": "test", "metadata": {"text_sha256": "abc"}},
    )


def _minimal_product_data(*, raw_name="Болт М8", normalized_name="болт м8", price="100.00", unit_code="pcs"):
    return {
        "raw_name": raw_name,
        "normalized_name": normalized_name,
        "price": Decimal(price),
        "unit_code": unit_code,
        "confidence": 1.0,
    }


def _make_payload(supplier, upload, products):
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
# apply_supplier_price_ai_result
# ---------------------------------------------------------------------------


class ApplySupplierPriceAIResultTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.upload = _make_upload(self.supplier, status=UploadStatus.PROCESSING)

    def _apply(self, products):
        from api.v1.suppliers.services import apply_supplier_price_ai_result

        payload = _make_payload(self.supplier, self.upload, products)
        return apply_supplier_price_ai_result(upload_id=self.upload.id, data=payload)

    # --- upload status after apply ---

    def test_upload_becomes_applied_when_no_review_items(self):
        self._apply([_minimal_product_data()])
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.APPLIED)

    def test_upload_becomes_needs_review_when_low_confidence(self):
        product = {**_minimal_product_data(), "confidence": 0.5}
        self._apply([product])
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.NEEDS_REVIEW)

    def test_upload_becomes_needs_review_when_price_missing(self):
        product = {**_minimal_product_data(), "price": None}
        self._apply([product])
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.NEEDS_REVIEW)

    def test_upload_becomes_needs_review_when_unit_missing(self):
        product = {**_minimal_product_data(), "unit_code": "unknown_xyz"}
        self._apply([product])
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.NEEDS_REVIEW)

    # --- product creation ---

    def test_creates_new_supplier_product(self):
        result = self._apply([_minimal_product_data()])
        self.assertEqual(result["products_created"], 1)
        self.assertTrue(SupplierProduct.objects.filter(supplier=self.supplier, supplier_name="Болт М8").exists())

    def test_new_product_gets_system_sku(self):
        self._apply([_minimal_product_data()])
        sp = SupplierProduct.objects.get(supplier=self.supplier)
        self.assertIsNotNone(sp.system_sku)
        self.assertTrue(sp.system_sku.startswith("AS-"))

    def test_creates_active_price_for_new_product(self):
        result = self._apply([_minimal_product_data(raw_name="Гайка", price="250.00")])
        self.assertEqual(result["prices_created"], 1)
        sp = SupplierProduct.objects.get(supplier=self.supplier)
        price = SupplierProductPrice.objects.get(supplier_product=sp)
        self.assertEqual(price.status, PriceStatus.ACTIVE)
        self.assertEqual(price.price, Decimal("250.00"))

    def test_creates_brand_via_get_or_create(self):
        product = {**_minimal_product_data(), "brand": "Bosch"}
        self._apply([product])
        sp = SupplierProduct.objects.get(supplier=self.supplier)
        self.assertIsNotNone(sp.brand)
        self.assertEqual(sp.brand.canonical_name, "Bosch")
        self.assertEqual(Brand.objects.filter(normalized_name="bosch").count(), 1)

    def test_second_apply_does_not_duplicate_brand(self):
        product = {**_minimal_product_data(), "brand": "Bosch"}
        self._apply([product])
        upload2 = _make_upload(self.supplier, status=UploadStatus.PROCESSING)
        from api.v1.suppliers.services import apply_supplier_price_ai_result

        payload2 = _make_payload(self.supplier, upload2, [product])
        apply_supplier_price_ai_result(upload_id=upload2.id, data=payload2)
        self.assertEqual(Brand.objects.filter(normalized_name="bosch").count(), 1)

    def test_creates_category_hierarchy(self):
        product = {**_minimal_product_data(), "category_path": ["Крепёж", "Болты", "Болты М8"]}
        self._apply([product])
        sp = SupplierProduct.objects.get(supplier=self.supplier)
        self.assertIsNotNone(sp.category)
        self.assertEqual(sp.category.name, "Болты М8")
        self.assertEqual(sp.category.parent.name, "Болты")
        self.assertEqual(sp.category.parent.parent.name, "Крепёж")

    # --- product update (existing product) ---

    def test_updates_existing_product_by_name(self):
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            status=EntityStatus.ACTIVE,
        )
        result = self._apply([_minimal_product_data(price="150.00")])
        self.assertEqual(result["products_created"], 0)
        sp.refresh_from_db()
        self.assertEqual(sp.normalized_name, "болт м8")

    def test_supersedes_old_price_on_update(self):
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("100.00"),
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )
        self._apply([_minimal_product_data(price="200.00")])
        old = SupplierProductPrice.objects.get(supplier_product=sp, price=Decimal("100.00"))
        self.assertEqual(old.status, PriceStatus.SUPERSEDED)
        self.assertIsNotNone(old.valid_to)
        new = SupplierProductPrice.objects.get(supplier_product=sp, price=Decimal("200.00"))
        self.assertEqual(new.status, PriceStatus.ACTIVE)

    def test_does_not_create_price_when_unchanged(self):
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            package=None,
            min_order_qty=None,
            delivery_multiple=None,
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("100.00"),
            currency="RUB",
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
            valid_to=None,
        )
        result = self._apply([_minimal_product_data(price="100.00")])
        self.assertEqual(result["prices_created"], 0)
        self.assertEqual(SupplierProductPrice.objects.filter(supplier_product=sp).count(), 1)

    # --- counters ---

    def test_result_counters_for_new_product(self):
        result = self._apply([_minimal_product_data()])
        self.assertEqual(result["products_created"], 1)
        self.assertEqual(result["prices_created"], 1)
        self.assertEqual(result["items_total"], 1)
        self.assertEqual(result["needs_review"], 0)

    def test_result_counters_for_needs_review(self):
        product = {**_minimal_product_data(), "confidence": 0.3}
        result = self._apply([product])
        self.assertEqual(result["needs_review"], 1)
        self.assertEqual(result["products_created"], 0)

    def test_multiple_products_in_one_payload(self):
        products = [_minimal_product_data(raw_name=f"Товар {i}", normalized_name=f"товар {i}") for i in range(3)]
        result = self._apply(products)
        self.assertEqual(result["items_total"], 3)
        self.assertEqual(result["products_created"], 3)
        self.assertEqual(result["prices_created"], 3)
        self.assertEqual(SupplierProduct.objects.filter(supplier=self.supplier).count(), 3)

    # --- upload item ---

    def test_creates_upload_item_for_new_product(self):
        self._apply([_minimal_product_data()])
        self.assertTrue(SupplierPriceUploadItem.objects.filter(upload=self.upload).exists())

    def test_upload_item_status_needs_review_for_low_confidence(self):
        product = {**_minimal_product_data(), "confidence": 0.1}
        self._apply([product])
        item = SupplierPriceUploadItem.objects.get(upload=self.upload)
        self.assertEqual(item.status, UploadItemStatus.NEEDS_REVIEW)

    # --- validation ---

    def test_raises_if_upload_id_mismatch(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from api.v1.suppliers.services import apply_supplier_price_ai_result

        payload = {
            "schema_version": 1,
            "processing_version": 1,
            "supplier_id": self.supplier.id,
            "upload_id": uuid4(),  # wrong id
            "source_channel": self.upload.source_channel,
            "supplier_name": self.supplier.display_name,
            "products": [],
        }
        with self.assertRaises(DRFValidationError):
            apply_supplier_price_ai_result(upload_id=self.upload.id, data=payload)

    def test_progress_callback_fires_periodically_and_once_at_the_end(self):
        """2026-08-17: apply()'s own per-row loop used to report no progress
        at all — a real 834-row upload sat at 0% for several minutes of
        genuine, correct processing (see APPLY_PROGRESS_UPDATE_EVERY's
        comment). Every Nth row, not every row, to keep the extra out-of-band
        writes from adding meaningfully to the runtime themselves."""
        from api.v1.suppliers.services import APPLY_PROGRESS_UPDATE_EVERY, apply_supplier_price_ai_result

        row_count = APPLY_PROGRESS_UPDATE_EVERY * 2 + 3
        products = [_minimal_product_data(raw_name=f"Товар {i}", unit_code=self.unit.code) for i in range(row_count)]
        payload = _make_payload(self.supplier, self.upload, products)
        calls = []

        apply_supplier_price_ai_result(
            upload_id=self.upload.id,
            data=payload,
            progress_callback=lambda *, rows_done, rows_total: calls.append((rows_done, rows_total)),
        )

        self.assertEqual(
            calls,
            [
                (APPLY_PROGRESS_UPDATE_EVERY, row_count),
                (APPLY_PROGRESS_UPDATE_EVERY * 2, row_count),
                (row_count, row_count),
            ],
        )

    def test_no_progress_callback_is_the_default_and_does_not_error(self):
        self._apply([_minimal_product_data(), _minimal_product_data(raw_name="second")])

    def test_prefetch_existing_supplier_products_query_count_does_not_scale_with_batch_size(self):
        """2026-08-17: _get_existing_supplier_product ran up to 4 sequential
        SELECTs per row (id, sku, raw_name, normalized_name+package) — for a
        real 834-row upload, mostly misses (nothing to match on a
        first-time upload), at ~47ms/query (2026-08-15/16 latency
        investigations) that alone was a real, measured chunk of a
        17.8-minute run. _prefetch_existing_supplier_products replaces it
        with (at most) 4 bulk queries regardless of row count — tested in
        isolation from the rest of apply()'s per-row writes (item/product/
        price creation), which legitimately do scale with row count and
        would swamp this signal in an end-to-end query count."""
        from django.test.utils import CaptureQueriesContext

        from api.v1.suppliers.services import _prefetch_existing_supplier_products

        small_batch = [_minimal_product_data(raw_name=f"Small {i}", unit_code=self.unit.code) for i in range(5)]
        large_batch = [_minimal_product_data(raw_name=f"Large {i}", unit_code=self.unit.code) for i in range(50)]

        with CaptureQueriesContext(connections["default"]) as small_ctx:
            _prefetch_existing_supplier_products(upload=self.upload, products=small_batch)
        with CaptureQueriesContext(connections["default"]) as large_ctx:
            _prefetch_existing_supplier_products(upload=self.upload, products=large_batch)

        self.assertEqual(len(small_ctx.captured_queries), len(large_ctx.captured_queries))
        self.assertLessEqual(len(large_ctx.captured_queries), 4)

    # --- 2026-08-18 bulk-write rewrite ---

    def test_apply_query_count_does_not_scale_with_batch_size(self):
        """2026-08-18: the whole point of the bulk_create/bulk_update
        rewrite (products/items/prices classified in memory, written in 3
        bulk statements total) — a real 834-row upload was ~1.3s/row even
        after the 2026-08-17 read-side fixes, because every row still paid
        its own item-upsert + product-save + price-supersede/create, each
        eating Yandex Managed Postgres's ~47-50ms round-trip floor. Query
        count for a 60-row batch must be roughly the same as for a 5-row
        batch, not 12x it."""
        from django.test.utils import CaptureQueriesContext

        from api.v1.suppliers.services import apply_supplier_price_ai_result

        small_upload = _make_upload(self.supplier, status=UploadStatus.PROCESSING)
        large_upload = _make_upload(self.supplier, status=UploadStatus.PROCESSING)
        small_batch = [_minimal_product_data(raw_name=f"Small {i}", unit_code=self.unit.code) for i in range(5)]
        large_batch = [_minimal_product_data(raw_name=f"Large {i}", unit_code=self.unit.code) for i in range(60)]

        with CaptureQueriesContext(connections["default"]) as small_ctx:
            apply_supplier_price_ai_result(
                upload_id=small_upload.id, data=_make_payload(self.supplier, small_upload, small_batch)
            )
        with CaptureQueriesContext(connections["default"]) as large_ctx:
            apply_supplier_price_ai_result(
                upload_id=large_upload.id, data=_make_payload(self.supplier, large_upload, large_batch)
            )

        self.assertEqual(SupplierProduct.objects.filter(supplier=self.supplier).count(), 65)
        # Generous headroom (not an exact count — brand/category resolution,
        # the advisory lock, etc. all still run) — the point is "roughly
        # flat", not "identical to the query".
        self.assertLess(
            len(large_ctx.captured_queries),
            len(small_ctx.captured_queries) + 10,
            f"small batch (5 rows): {len(small_ctx.captured_queries)} queries, "
            f"large batch (60 rows): {len(large_ctx.captured_queries)} queries — "
            "query count is scaling with row count again",
        )

    def test_in_batch_duplicate_new_product_rows_use_the_last_price(self):
        """Two rows in one payload for a product that doesn't exist yet —
        same raw_name, different prices. Only the LAST row's price should
        ever become an actual SupplierProductPrice row (see the module
        comment above _ApplyBatchState in services.py for why intermediate
        in-batch duplicate writes are deliberately not replicated)."""
        result = self._apply(
            [
                _minimal_product_data(raw_name="Дубликат", price="100.00", unit_code=self.unit.code),
                _minimal_product_data(raw_name="Дубликат", price="200.00", unit_code=self.unit.code),
            ]
        )
        self.assertEqual(SupplierProduct.objects.filter(supplier=self.supplier, supplier_name="Дубликат").count(), 1)
        sp = SupplierProduct.objects.get(supplier=self.supplier, supplier_name="Дубликат")
        prices = list(SupplierProductPrice.objects.filter(supplier_product=sp))
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0].price, Decimal("200.00"))
        self.assertEqual(prices[0].status, PriceStatus.ACTIVE)
        self.assertEqual(result["products_created"], 1)

    def test_in_batch_duplicate_rows_that_end_up_back_at_the_original_price_are_a_no_op(self):
        """Existing product at 100.00. One apply() call has two rows for
        it: one bidding 200.00, the next back down to 100.00 — the batch's
        net effect on the active price is nothing. Sequential per-row
        processing would still have written 2 new price rows along the way
        (superseded 100 -> active-then-superseded 200 -> active 100 again);
        the bulk classification compares the batch's FINAL target against
        the true pre-batch price once, so this is a genuine no-op: no new
        rows, the original price row is never even touched."""
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Дубликат2",
            normalized_name="дубликат2",
            status=EntityStatus.ACTIVE,
        )
        original_price = SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("100.00"),
            currency="RUB",
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )
        result = self._apply(
            [
                _minimal_product_data(raw_name="Дубликат2", normalized_name="дубликат2", price="200.00"),
                _minimal_product_data(raw_name="Дубликат2", normalized_name="дубликат2", price="100.00"),
            ]
        )
        self.assertEqual(result["prices_created"], 0)
        self.assertEqual(SupplierProductPrice.objects.filter(supplier_product=sp).count(), 1)
        original_price.refresh_from_db()
        self.assertEqual(original_price.status, PriceStatus.ACTIVE)
        self.assertIsNone(original_price.valid_to)

    def test_in_batch_duplicate_rows_resolved_via_different_cache_keys_do_not_create_two_active_prices(self):
        """Regression for TASK-98: _prefetch_existing_supplier_products builds
        4 independent cache dicts (by_id/by_sku/by_raw_name/by_normalized_and_package)
        from 4 separate queries — the same DB row can come back as two different
        Python object instances depending on which dict a row's product_data
        resolves through. price_plan used to key on id(supplier_product)
        (object identity), so two rows for the same real product that resolve
        via different cache dicts got two independent plan entries — and the
        final write loop created two simultaneously ACTIVE SupplierProductPrice
        rows for one product."""
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Молоко пакет",
            normalized_name="молоко пакет",
            supplier_sku="SKU-1",
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("50.00"),
            currency="RUB",
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )

        row_via_id = _minimal_product_data(
            raw_name="Молоко пакет", normalized_name="молоко пакет", price="60.00", unit_code=self.unit.code
        )
        row_via_id["supplier_product_id"] = sp.id
        row_via_raw_name = _minimal_product_data(
            raw_name="Молоко пакет", normalized_name="молоко пакет", price="70.00", unit_code=self.unit.code
        )

        self._apply([row_via_id, row_via_raw_name])

        active_prices = SupplierProductPrice.objects.filter(supplier_product=sp, status=PriceStatus.ACTIVE)
        self.assertEqual(
            active_prices.count(), 1, f"expected exactly one active price, got {list(active_prices.values('price'))}"
        )
        self.assertEqual(active_prices.get().price, Decimal("70.00"))

    def test_mixed_new_changed_and_matched_rows_in_one_batch(self):
        """One apply() call, three rows each taking a different branch —
        create, update-existing, matched-and-unchanged — in the SAME bulk
        write. Guards against the 3 branches' plan lists (new_products/
        products_to_update/nothing) bleeding into each other."""
        unchanged_price = "500.00"
        unchanged = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Без изменений",
            normalized_name="без изменений",
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=unchanged,
            price=Decimal(unchanged_price),
            currency="RUB",
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )
        to_update = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Будет изменён",
            normalized_name="старое имя",
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=to_update,
            price=Decimal("10.00"),
            currency="RUB",
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )

        result = self._apply(
            [
                _minimal_product_data(raw_name="Без изменений", normalized_name="без изменений", price=unchanged_price),
                _minimal_product_data(raw_name="Будет изменён", normalized_name="новое имя", price="20.00"),
                _minimal_product_data(raw_name="Новый товар", normalized_name="новый товар", price="30.00"),
            ]
        )

        self.assertEqual(result["products_created"], 1)
        self.assertEqual(SupplierProduct.objects.filter(supplier=self.supplier).count(), 3)

        unchanged.refresh_from_db()
        self.assertEqual(unchanged.normalized_name, "без изменений")
        self.assertEqual(SupplierProductPrice.objects.filter(supplier_product=unchanged).count(), 1)

        to_update.refresh_from_db()
        self.assertEqual(to_update.normalized_name, "новое имя")
        active = SupplierProductPrice.objects.get(supplier_product=to_update, status=PriceStatus.ACTIVE)
        self.assertEqual(active.price, Decimal("20.00"))
        superseded = SupplierProductPrice.objects.get(supplier_product=to_update, status=PriceStatus.SUPERSEDED)
        self.assertEqual(superseded.price, Decimal("10.00"))

        new_sp = SupplierProduct.objects.get(supplier=self.supplier, supplier_name="Новый товар")
        self.assertEqual(
            SupplierProductPrice.objects.get(supplier_product=new_sp, status=PriceStatus.ACTIVE).price,
            Decimal("30.00"),
        )

    def test_updates_current_price_snapshot_via_bulk_sync(self):
        """SupplierProduct.current_price/previous_price/price_changed_at
        (suppliers/services.py's sync_supplier_product_price_snapshot) are
        normally kept in sync by SupplierProductPrice's post_save signal —
        which bulk_create() never fires. apply()'s bulk-write path has to
        do this itself (_bulk_sync_supplier_product_price_snapshots) or
        these 3 columns would silently go stale on every upload."""
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Снапшот",
            normalized_name="снапшот",
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("10.00"),
            currency="RUB",
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )
        self._apply([_minimal_product_data(raw_name="Снапшот", normalized_name="снапшот", price="15.00")])
        sp.refresh_from_db()
        self.assertEqual(sp.current_price, Decimal("15.00"))
        self.assertEqual(sp.previous_price, Decimal("10.00"))
        self.assertIsNotNone(sp.price_changed_at)


# ---------------------------------------------------------------------------
# build_normalized_payload — full active Unit vocabulary (TASK-101a)
# ---------------------------------------------------------------------------


class BuildNormalizedPayloadUnitVocabularyTests(TestCase):
    """TASK-101a end-to-end: build_normalized_payload must thread the live
    Unit vocabulary into parse_supplier_price_row so package-kind units
    (уп/кор/ящ/...) are recognized, not just the fixed шт/кг/г/л/мл set.
    Uses update_or_create (matching core/migrations/0012_seed_extended_units.py's
    own idempotent pattern) since "уп" is very likely already seeded by that
    data migration in the test database — a bare .create() could collide."""

    def test_package_unit_from_db_is_recognized_end_to_end(self):
        from api.v1.suppliers.normalization import ExtractedRow, build_normalized_payload

        supplier = _make_supplier()
        upload = _make_upload(supplier)
        Unit.objects.update_or_create(
            code="уп", defaults={"name": "Упаковка", "kind": "package", "status": EntityStatus.ACTIVE}
        )
        row = ExtractedRow(row_number=1, cells={"наименование": "Салфетки", "цена": "50", "ед": "уп"})

        payload, _diagnostics = build_normalized_payload(upload=upload, rows=[row], use_ai=False)

        self.assertEqual(payload["products"][0]["unit_code"], "уп")


# ---------------------------------------------------------------------------
# rollback_supplier_price_upload
# ---------------------------------------------------------------------------


class RollbackSupplierPriceUploadServiceTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.upload = _make_upload(self.supplier, status=UploadStatus.PROCESSING)

    def _apply(self, upload, products):
        from api.v1.suppliers.services import apply_supplier_price_ai_result

        payload = _make_payload(self.supplier, upload, products)
        return apply_supplier_price_ai_result(upload_id=upload.id, data=payload)

    def _rollback(self, upload):
        from api.v1.suppliers.services import rollback_supplier_price_upload

        return rollback_supplier_price_upload(upload_id=upload.id)

    def test_archives_product_created_by_this_upload(self):
        self._apply(self.upload, [_minimal_product_data()])
        sp = SupplierProduct.objects.get(supplier=self.supplier)
        result = self._rollback(self.upload)
        self.assertEqual(result["products_archived"], 1)
        sp.refresh_from_db()
        self.assertEqual(sp.status, EntityStatus.ARCHIVED)

    def test_cancels_active_price_of_archived_product(self):
        self._apply(self.upload, [_minimal_product_data(price="250.00")])
        sp = SupplierProduct.objects.get(supplier=self.supplier)
        self._rollback(self.upload)
        price = SupplierProductPrice.objects.get(supplier_product=sp, price=Decimal("250.00"))
        self.assertEqual(price.status, PriceStatus.CANCELLED)

    def test_reverts_price_on_updated_product(self):
        sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("100.00"),
            currency="RUB",
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )
        self._apply(self.upload, [_minimal_product_data(price="200.00")])
        result = self._rollback(self.upload)
        self.assertEqual(result["prices_reverted"], 1)
        active = SupplierProductPrice.objects.get(
            supplier_product=sp,
            status=PriceStatus.ACTIVE,
            valid_to__isnull=True,
        )
        self.assertEqual(active.price, Decimal("100.00"))
        superseded = SupplierProductPrice.objects.get(supplier_product=sp, price=Decimal("200.00"))
        self.assertEqual(superseded.status, PriceStatus.CANCELLED)

    def test_upload_status_becomes_archived(self):
        self._apply(self.upload, [_minimal_product_data()])
        self._rollback(self.upload)
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.ARCHIVED)

    def test_idempotent_on_repeated_rollback(self):
        self._apply(self.upload, [_minimal_product_data()])
        self._rollback(self.upload)
        result = self._rollback(self.upload)
        self.assertTrue(result["already_rolled_back"])
        self.assertEqual(result["products_archived"], 0)

    def test_skips_product_touched_by_a_newer_upload(self):
        self._apply(self.upload, [_minimal_product_data()])
        sp = SupplierProduct.objects.get(supplier=self.supplier)
        upload2 = _make_upload(self.supplier, status=UploadStatus.PROCESSING)
        self._apply(upload2, [_minimal_product_data(price="300.00")])
        result = self._rollback(self.upload)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["products_archived"], 0)
        sp.refresh_from_db()
        self.assertEqual(sp.status, EntityStatus.ACTIVE)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierPriceUploadRejectHTTPTests(APITestCase):
    """HTTP-level coverage for the reject endpoint — the tests above only
    call rollback_supplier_price_upload() directly, never through the view."""

    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.upload = _make_upload(self.supplier, status=UploadStatus.PROCESSING)

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def reject_url(self, upload):
        return f"/api/v1/suppliers/uploads/{upload.id}/reject"

    def test_requires_service_token(self):
        response = self.client.post(self.reject_url(self.upload))
        self.assertEqual(response.status_code, 401)

    def test_supplier_cabinet_jwt_can_reject_own_upload(self):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        response = self.client.post(self.reject_url(self.upload))
        self.assertEqual(response.status_code, 200)

    def test_supplier_cabinet_jwt_cannot_reject_other_suppliers_upload(self):
        other = _make_supplier(name="Other Co")

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(other))
        response = self.client.post(self.reject_url(self.upload))

        self.assertEqual(response.status_code, 404)
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.PROCESSING)

    def test_rejects_and_archives_new_product(self):
        self.authenticate()
        from api.v1.suppliers.services import apply_supplier_price_ai_result

        payload = _make_payload(self.supplier, self.upload, [_minimal_product_data()])
        apply_supplier_price_ai_result(upload_id=self.upload.id, data=payload)

        response = self.client.post(self.reject_url(self.upload))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["products_archived"], 1)
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.ARCHIVED)


# ---------------------------------------------------------------------------
# initiate_supplier_account_transfer / claim_supplier_account_transfer
# ---------------------------------------------------------------------------


class SupplierAccountTransferServiceTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()

    def _initiate(self, email="new-owner@example.com"):
        from api.v1.suppliers.services import initiate_supplier_account_transfer

        return initiate_supplier_account_transfer(supplier_id=self.supplier.id, target_email=email)

    def _claim(self, token, password="newpassword123", confirm=None):
        from api.v1.suppliers.services import claim_supplier_account_transfer

        return claim_supplier_account_transfer(token=token, new_password=password)

    def test_initiate_creates_pending_transfer(self):
        transfer = self._initiate()
        self.assertEqual(transfer.status, SupplierAccountTransferStatus.PENDING)
        self.assertEqual(transfer.target_email, "new-owner@example.com")

    def test_smtp_failure_raises_email_delivery_error_and_rolls_back(self):
        import smtplib

        from api.v1.auth.services import EmailDeliveryError

        with patch("api.v1.suppliers.services.registration.send_mail", side_effect=smtplib.SMTPRecipientsRefused({})):
            with self.assertRaises(EmailDeliveryError):
                self._initiate(email="smtp-fail@example.com")
        self.assertFalse(
            SupplierAccountTransfer.objects.filter(
                supplier=self.supplier, target_email="smtp-fail@example.com"
            ).exists()
        )

    def test_initiate_sends_email(self):
        from django.core import mail

        self._initiate(email="supplier@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("supplier@example.com", mail.outbox[0].to)

    def test_initiate_cancels_previous_pending_transfer(self):
        first = self._initiate(email="first@example.com")
        second = self._initiate(email="second@example.com")
        first.refresh_from_db()
        self.assertEqual(first.status, SupplierAccountTransferStatus.CANCELLED)
        self.assertEqual(second.status, SupplierAccountTransferStatus.PENDING)

    def test_claim_creates_user_and_sets_password(self):
        transfer = self._initiate(email="claim-me@example.com")
        result = self._claim(transfer.token)
        user = User.objects.get(email="claim-me@example.com")
        self.assertEqual(result["user_id"], user.id)
        self.assertTrue(user.password)
        self.assertNotEqual(user.password, "newpassword123")

    def test_claim_does_not_reset_password_for_existing_account(self):
        from django.contrib.auth.hashers import check_password, make_password

        existing = User.objects.create(
            email="existing-owner@example.com",
            full_name="Existing",
            role=UserRole.SUPPLIER,
            password=make_password("original-password"),
        )
        transfer = self._initiate(email="existing-owner@example.com")
        self._claim(transfer.token, password="attacker-supplied-password")

        existing.refresh_from_db()
        self.assertTrue(check_password("original-password", existing.password))

    def test_claim_makes_user_active_owner(self):
        transfer = self._initiate(email="owner@example.com")
        self._claim(transfer.token)
        user = User.objects.get(email="owner@example.com")
        su = SupplierUser.objects.get(supplier=self.supplier, user=user)
        self.assertEqual(su.role, SupplierUserRole.OWNER)
        self.assertEqual(su.status, EntityStatus.ACTIVE)

    def test_claim_deactivates_previous_owner(self):
        old_owner = User.objects.create(email="old@example.com", full_name="Old Owner", role="supplier")
        SupplierUser.objects.create(
            supplier=self.supplier, user=old_owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )

        transfer = self._initiate(email="new@example.com")
        self._claim(transfer.token)

        old_su = SupplierUser.objects.get(supplier=self.supplier, user=old_owner)
        self.assertEqual(old_su.status, EntityStatus.INACTIVE)

    def test_claim_marks_transfer_claimed(self):
        transfer = self._initiate()
        self._claim(transfer.token)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, SupplierAccountTransferStatus.CLAIMED)
        self.assertIsNotNone(transfer.claimed_at)

    def test_claim_twice_raises_validation_error(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        transfer = self._initiate()
        self._claim(transfer.token)
        with self.assertRaises(DRFValidationError):
            self._claim(transfer.token)

    def test_claim_cancelled_transfer_raises_validation_error(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        first = self._initiate(email="first@example.com")
        self._initiate(email="second@example.com")  # cancels `first`
        with self.assertRaises(DRFValidationError):
            self._claim(first.token)

    def test_claim_expired_transfer_raises_validation_error(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        transfer = self._initiate()
        transfer.expires_at = timezone.now() - timezone.timedelta(hours=1)
        transfer.save(update_fields=["expires_at"])
        with self.assertRaises(DRFValidationError):
            self._claim(transfer.token)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, SupplierAccountTransferStatus.EXPIRED)

    def test_claim_unknown_token_returns_404(self):
        from django.http import Http404

        with self.assertRaises(Http404):
            self._claim("does-not-exist-token")


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierAccountTransferHTTPTests(APITestCase):
    """HTTP-level coverage: url wiring, serializer validation, permissions —
    the service-level tests above never actually hit these views."""

    def setUp(self):
        self.supplier = _make_supplier()

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def initiate_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/account-transfer"

    def claim_url(self, token):
        return f"/api/v1/suppliers/account-transfer/{token}/claim"

    def test_initiate_requires_service_token(self):
        # 401, not 403: the view now also registers a JWT authenticator, so DRF's
        # permission_denied() reports "unauthenticated" (has a WWW-Authenticate
        # challenge) rather than plain "forbidden" for a request with no credentials.
        response = self.client.post(self.initiate_url(), {"target_email": "x@example.com"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_initiate_with_token_creates_transfer(self):
        self.authenticate()
        response = self.client.post(self.initiate_url(), {"target_email": "new-owner@example.com"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["target_email"], "new-owner@example.com")
        self.assertEqual(response.data["status"], SupplierAccountTransferStatus.PENDING)

    def test_initiate_rejects_invalid_email(self):
        self.authenticate()
        response = self.client.post(self.initiate_url(), {"target_email": "not-an-email"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_claim_does_not_require_service_token(self):
        self.authenticate()
        create_response = self.client.post(self.initiate_url(), {"target_email": "claimer@example.com"}, format="json")
        self.client.credentials()  # a real claimant hits this link with no auth at all

        token = SupplierAccountTransfer.objects.get(id=create_response.data["id"]).token
        response = self.client.post(
            self.claim_url(token),
            {"new_password": "password123", "confirm_password": "password123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("user_id", response.data)

    def test_claim_rejects_mismatched_passwords(self):
        self.authenticate()
        create_response = self.client.post(self.initiate_url(), {"target_email": "mismatch@example.com"}, format="json")
        token = SupplierAccountTransfer.objects.get(id=create_response.data["id"]).token
        response = self.client.post(
            self.claim_url(token),
            {"new_password": "password123", "confirm_password": "different456"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


def _jwt_bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    return f"Bearer {RefreshToken.for_user(user).access_token}"


def _supplier_cabinet_bearer(supplier, user=None):
    from api.v1.suppliers.services import issue_supplier_cabinet_tokens

    return f"Bearer {issue_supplier_cabinet_tokens(supplier, user=user)['access']}"


def _supplier_cabinet_owner_bearer(supplier):
    """Cabinet JWT with a user_id claim resolving to an active owner —
    needed since 2026-08-01 for owner-gated cabinet actions (delete,
    bank accounts, registry fields)."""
    owner = User.objects.create(email=f"owner-{uuid4()}@example.com", full_name="Owner", role=UserRole.SUPPLIER)
    SupplierUser.objects.create(supplier=supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE)
    return _supplier_cabinet_bearer(supplier, user=owner)


def _make_admin_user(email="admin@example.com"):
    return User.objects.create(
        email=email,
        full_name="Admin",
        role=UserRole.ADMIN,
        status=EntityStatus.ACTIVE,
    )


# ---------------------------------------------------------------------------
# create_supplier_by_staff
# ---------------------------------------------------------------------------


class CreateSupplierByStaffServiceTests(APITestCase):
    def setUp(self):
        self.staff = _make_admin_user()

    def test_creates_active_supplier_and_owner(self):
        from api.v1.suppliers.services import create_supplier_by_staff

        supplier = create_supplier_by_staff(created_by_user=self.staff, display_name="Новый Поставщик")
        self.assertEqual(supplier.status, EntityStatus.ACTIVE)
        self.assertEqual(supplier.legal_name, "Новый Поставщик")
        membership = SupplierUser.objects.get(supplier=supplier, user=self.staff)
        self.assertEqual(membership.role, SupplierUserRole.OWNER)
        self.assertEqual(membership.status, EntityStatus.ACTIVE)

    def test_rejects_invalid_inn(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from api.v1.suppliers.services import create_supplier_by_staff

        with self.assertRaises(DRFValidationError):
            create_supplier_by_staff(created_by_user=self.staff, display_name="X", inn="123")

    def test_rejects_duplicate_active_inn(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from api.v1.suppliers.services import create_supplier_by_staff

        Supplier.objects.create(
            display_name="Existing", legal_name="Existing", inn="7707083893", status=EntityStatus.ACTIVE
        )
        with self.assertRaises(DRFValidationError):
            create_supplier_by_staff(created_by_user=self.staff, display_name="New", inn="7707083893")


class SupplierDedupServiceTests(APITestCase):
    """create_supplier_by_staff/create_supplier_by_owner both go through
    _create_supplier_record, so testing via create_supplier_by_staff covers
    both paths' shared dedup logic."""

    def setUp(self):
        self.staff = _make_admin_user()

    def _create(self, **kwargs):
        from api.v1.suppliers.services import create_supplier_by_staff

        return create_supplier_by_staff(created_by_user=self.staff, **kwargs)

    def test_same_inn_and_kpp_is_blocked_as_same_branch(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        Supplier.objects.create(
            display_name="Existing",
            legal_name="Existing",
            inn="7707083893",
            kpp="770701001",
            status=EntityStatus.ACTIVE,
        )
        with self.assertRaises(DRFValidationError):
            self._create(display_name="New", inn="7707083893", kpp="770701001")

    def test_same_ogrn_is_blocked_as_same_company(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        Supplier.objects.create(
            display_name="Existing", legal_name="Existing", ogrn="1027700132195", status=EntityStatus.ACTIVE
        )
        with self.assertRaises(DRFValidationError):
            self._create(display_name="New", ogrn="1027700132195")

    def test_matching_name_and_address_without_inn_is_blocked_as_possible_duplicate(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        Supplier.objects.create(
            display_name="Ромашка",
            legal_name="ООО Ромашка",
            legal_address="Москва, ул. Ленина, 1",
            status=EntityStatus.ACTIVE,
        )
        with self.assertRaises(DRFValidationError):
            self._create(
                display_name="Ромашка 2",
                legal_name="ООО Ромашка",
                legal_address="г. Москва, улица Ленина, дом 1",
            )

    def test_possible_duplicate_bypassed_with_confirm_flag(self):
        Supplier.objects.create(
            display_name="Ромашка",
            legal_name="ООО Ромашка",
            legal_address="Москва, ул. Ленина, 1",
            status=EntityStatus.ACTIVE,
        )
        supplier = self._create(
            display_name="Ромашка 2",
            legal_name="ООО Ромашка",
            legal_address="г. Москва, улица Ленина, дом 1",
            confirm_not_duplicate=True,
        )
        self.assertEqual(supplier.display_name, "Ромашка 2")

    def test_confirm_flag_does_not_bypass_exact_inn_match(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        Supplier.objects.create(
            display_name="Existing", legal_name="Existing", inn="7707083893", status=EntityStatus.ACTIVE
        )
        with self.assertRaises(DRFValidationError):
            self._create(display_name="New", inn="7707083893", confirm_not_duplicate=True)

    def test_unrelated_company_is_not_blocked(self):
        Supplier.objects.create(
            display_name="Ромашка", legal_name="ООО Ромашка", legal_address="Москва", status=EntityStatus.ACTIVE
        )
        supplier = self._create(display_name="Василёк", legal_name="ООО Василёк", legal_address="Новосибирск")
        self.assertEqual(supplier.display_name, "Василёк")

    def test_archived_supplier_does_not_block_new_one(self):
        Supplier.objects.create(
            display_name="Existing", legal_name="Existing", inn="7707083893", status=EntityStatus.ARCHIVED
        )
        supplier = self._create(display_name="New", inn="7707083893")
        self.assertEqual(supplier.inn, "7707083893")


class SupplierCreateHTTPTests(APITestCase):
    def setUp(self):
        self.staff = _make_admin_user()
        self.supplier_user = User.objects.create(
            email="supplier-jwt@example.com",
            full_name="Supplier",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )

    def url(self):
        return "/api/v1/suppliers/"

    def test_requires_authentication(self):
        response = self.client.post(self.url(), {"display_name": "X"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_rejects_non_staff_role(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.supplier_user))
        response = self.client.post(self.url(), {"display_name": "X"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_supplier(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.post(self.url(), {"display_name": "Компания X"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["display_name"], "Компания X")
        self.assertEqual(response.data["status"], EntityStatus.ACTIVE)
        self.assertTrue(
            SupplierUser.objects.filter(
                supplier_id=response.data["id"], user=self.staff, role=SupplierUserRole.OWNER
            ).exists()
        )


class SupplierMineHTTPTests(APITestCase):
    def setUp(self):
        self.staff = _make_admin_user()

    def url(self):
        return "/api/v1/suppliers/mine"

    def test_requires_authentication(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 401)

    def test_returns_list_with_own_companies_only(self):
        from api.v1.suppliers.services import create_supplier_by_staff

        mine = create_supplier_by_staff(created_by_user=self.staff, display_name="Моя компания")
        _make_supplier(name="Чужая компания")

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertEqual([item["id"] for item in response.data], [str(mine.id)])

    def test_my_role_reflects_actual_membership_role(self):
        """2026-08-07 (запрос Вити): нужна роль в таблице — owner/manager."""
        from api.v1.suppliers.services import create_supplier_by_staff

        owned = create_supplier_by_staff(created_by_user=self.staff, display_name="Моя владелица")
        managed = _make_supplier(name="Где я менеджер")
        SupplierUser.objects.create(
            supplier=managed, user=self.staff, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        role_by_id = {item["id"]: item["my_role"] for item in response.data}
        self.assertEqual(role_by_id[str(owned.id)], SupplierUserRole.OWNER)
        self.assertEqual(role_by_id[str(managed.id)], SupplierUserRole.MANAGER)

    def test_returns_empty_list_when_no_company(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_returns_all_memberships_most_recent_first(self):
        from api.v1.suppliers.services import create_supplier_by_staff

        first = create_supplier_by_staff(created_by_user=self.staff, display_name="Первая компания")
        second = create_supplier_by_staff(created_by_user=self.staff, display_name="Вторая компания")

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.data], [str(second.id), str(first.id)])


# ---------------------------------------------------------------------------
# create_supplier_by_owner — self-service после регистрации
# ---------------------------------------------------------------------------


class CreateSupplierByOwnerServiceTests(APITestCase):
    def setUp(self):
        self.supplier_user = User.objects.create(
            email="fresh-supplier@example.com",
            full_name="Fresh Supplier",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )

    def test_creates_active_supplier_and_owner(self):
        from api.v1.suppliers.services import create_supplier_by_owner

        supplier = create_supplier_by_owner(user=self.supplier_user, display_name="Моя компания")
        self.assertEqual(supplier.status, EntityStatus.ACTIVE)
        membership = SupplierUser.objects.get(supplier=supplier, user=self.supplier_user)
        self.assertEqual(membership.role, SupplierUserRole.OWNER)
        self.assertEqual(membership.status, EntityStatus.ACTIVE)

    def test_saves_registry_fields_from_dadata_lookup(self):
        from api.v1.suppliers.services import create_supplier_by_owner

        supplier = create_supplier_by_owner(
            user=self.supplier_user,
            display_name="Моя компания",
            okved="46.90",
            okpo="12345678",
            registration_date="2015-03-12",
            general_director="Иванов Иван Иванович",
        )
        supplier.refresh_from_db()
        self.assertEqual(supplier.okved, "46.90")
        self.assertEqual(supplier.okpo, "12345678")
        self.assertEqual(str(supplier.registration_date), "2015-03-12")
        self.assertEqual(supplier.general_director, "Иванов Иван Иванович")

    def test_allows_creating_several_companies_for_same_user(self):
        from api.v1.suppliers.services import create_supplier_by_owner

        first = create_supplier_by_owner(user=self.supplier_user, display_name="Первая компания")
        second = create_supplier_by_owner(user=self.supplier_user, display_name="Вторая компания")

        self.assertNotEqual(first.id, second.id)
        memberships = SupplierUser.objects.filter(user=self.supplier_user, status=EntityStatus.ACTIVE)
        self.assertEqual(set(memberships.values_list("supplier_id", flat=True)), {first.id, second.id})
        self.assertTrue(all(m.role == SupplierUserRole.OWNER for m in memberships))

    def test_rejects_duplicate_active_inn(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from api.v1.suppliers.services import create_supplier_by_owner

        Supplier.objects.create(
            display_name="Existing", legal_name="Existing", inn="7707083893", status=EntityStatus.ACTIVE
        )
        with self.assertRaises(DRFValidationError):
            create_supplier_by_owner(user=self.supplier_user, display_name="New", inn="7707083893")


class CreateSupplierByOwnerConcurrencyTests(TransactionTestCase):
    """TransactionTestCase (real commits, not TestCase's wrapping transaction)
    — needed because this test spawns real threads with their own DB
    connections to exercise select_for_update() locking, which a
    TestCase-wrapped transaction can't observe across threads."""

    def _run_pair(self, user, kwargs_a, kwargs_b):
        from api.v1.suppliers.services import create_supplier_by_owner

        results = []
        errors = []

        def worker(kwargs):
            connections.close_all()
            try:
                supplier = create_supplier_by_owner(user=user, **kwargs)
                results.append(supplier.id)
            except Exception as exc:  # noqa: BLE001 — capturing for assertion below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(kwargs,)) for kwargs in (kwargs_a, kwargs_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results, errors

    def test_two_concurrent_calls_with_same_inn_create_only_one_company(self):
        # Multi-company is allowed now, but the *dedup* check must still be
        # race-safe for repeated submissions from the same user — two
        # near-simultaneous calls with the same INN (double-click, retry)
        # must not both create a company with that INN.
        user = User.objects.create(
            email="race-supplier-inn@example.com",
            full_name="Race Supplier",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        results, errors = self._run_pair(
            user,
            {"display_name": "Company A", "inn": "7707083893"},
            {"display_name": "Company B", "inn": "7707083893"},
        )

        self.assertEqual(len(results), 1, f"expected exactly one company created, got {results} errors={errors}")
        self.assertEqual(
            SupplierUser.objects.filter(user=user, status=EntityStatus.ACTIVE).count(),
            1,
            "the losing request should have been rejected by dedup, not silently created a second company",
        )

    def test_two_concurrent_calls_with_different_data_both_succeed(self):
        # Confirms multi-company creation itself isn't accidentally
        # serialized/blocked by the user-row lock — only genuine dedup
        # collisions should be.
        user = User.objects.create(
            email="race-supplier-multi@example.com",
            full_name="Race Supplier",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        results, errors = self._run_pair(
            user,
            {"display_name": "Ромашка", "legal_address": "Москва"},
            {"display_name": "Василёк", "legal_address": "Новосибирск"},
        )

        self.assertEqual(len(results), 2, f"expected both companies created, got {results} errors={errors}")
        self.assertEqual(SupplierUser.objects.filter(user=user, status=EntityStatus.ACTIVE).count(), 2)


class SingleOwnerConcurrencyTests(TransactionTestCase):
    """2026-08-06 (перепроверка по запросу пользователя после первого прохода):
    _demote_other_active_owners() (тогда называлась _deactivate_other_active_owners,
    переименована 2026-08-07 когда пользователь исправил деактивацию на понижение
    до manager — сам race guard не изменился) изначально не лочило Supplier целиком,
    только строки SupplierUser — эмпирически подтверждена гонка (5/5 запусков на
    реальном Postgres, до фикса): два одновременных "сделать owner'ом" для двух
    РАЗНЫХ пользователей (например, owner и staff одновременно) обе транзакции видят
    исходного owner'а активным в своём снапшоте и понижают его, но ни одна не
    видит нового owner'а другой транзакции — в итоге оставались два активных
    owner'а. Фикс — select_for_update() на самой строке Supplier, тот же приём, что
    у invite_supplier_user/initiate_supplier_account_transfer, форсирует
    последовательное выполнение. См. CreateSupplierByOwnerConcurrencyTests выше за
    объяснением, зачем TransactionTestCase + реальные потоки."""

    def _run_pair(self, supplier_id, owner_id, target_ids):
        from api.v1.suppliers.services import change_supplier_user_role

        errors = []

        def worker(target_id):
            connections.close_all()
            try:
                change_supplier_user_role(
                    supplier_id=supplier_id,
                    target_user_id=target_id,
                    new_role=SupplierUserRole.OWNER,
                    requested_by_user_id=owner_id,
                )
            except Exception as exc:  # noqa: BLE001 — capturing for assertion below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(target_id,)) for target_id in target_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    def test_two_concurrent_promotions_to_owner_leave_exactly_one_active_owner(self):
        supplier = Supplier.objects.create(legal_name="Race Owner LLC", display_name="Race Owner")
        real_owner = User.objects.create(email="race-real-owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=supplier, user=real_owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        manager_a = User.objects.create(email="race-mgr-a@example.com", full_name="Manager A", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=supplier, user=manager_a, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        manager_b = User.objects.create(email="race-mgr-b@example.com", full_name="Manager B", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=supplier, user=manager_b, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

        errors = self._run_pair(supplier.id, real_owner.id, (manager_a.id, manager_b.id))

        self.assertEqual(errors, [])
        active_owners = list(
            SupplierUser.objects.filter(
                supplier=supplier, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
            ).values_list("user_id", flat=True)
        )
        self.assertEqual(
            len(active_owners),
            1,
            f"expected exactly one active owner after concurrent promotion, got {active_owners}",
        )


class SupplierPriceUploadDedupConcurrencyTests(TransactionTestCase):
    """See CreateSupplierByOwnerConcurrencyTests docstring above for why
    TransactionTestCase + real threads is needed here."""

    def test_two_concurrent_text_uploads_with_same_fingerprint_dedup_to_one(self):
        from api.v1.suppliers.services import create_supplier_price_upload_from_text

        supplier = _make_supplier()
        results = []
        errors = []

        def worker(label):
            connections.close_all()
            try:
                upload = create_supplier_price_upload_from_text(
                    data={
                        "supplier": supplier,
                        "source_channel": SourceChannel.TELEGRAM,
                        "text": "Молоко 100 руб шт",
                    }
                )
                results.append(upload.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(label,)) for label in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"expected no errors, got {errors}")
        self.assertEqual(len(set(results)), 1, f"expected both calls to dedup to the same upload, got {results}")
        self.assertEqual(SupplierPriceUpload.objects.filter(supplier=supplier).count(), 1)

    def test_dedup_check_does_not_block_on_row_locked_by_in_flight_normalization(self):
        """Regression guard for the 2026-08-16 hang: a byte-identical
        re-upload used to block for as long as apply_supplier_price_ai_result()
        (Celery, run_async default True since 2026-08-15) holds
        select_for_update() on the SAME upload row for its whole per-row
        loop — a live incident hit exactly this via gunicorn's --timeout 120
        on the synchronous intake request. Simulates that lock with a real
        second thread/connection instead of the Celery task itself."""
        from api.v1.suppliers.services import _get_existing_upload_for_fingerprint

        supplier = _make_supplier()
        upload = SupplierPriceUpload.objects.create(
            supplier=supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.PROCESSING,
            raw_payload={"kind": "file", "metadata": {"content_sha256": "deadbeef"}},
        )

        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def holder():
            connections.close_all()
            with transaction.atomic():
                SupplierPriceUpload.objects.select_for_update().get(pk=upload.pk)
                lock_acquired.set()
                release_lock.wait(timeout=5)
            connections.close_all()

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        self.assertTrue(lock_acquired.wait(timeout=5), "holder thread never acquired the row lock")

        try:
            start = time.monotonic()
            with transaction.atomic():
                found = _get_existing_upload_for_fingerprint(
                    supplier=supplier,
                    source_channel=SourceChannel.TELEGRAM,
                    kind="file",
                    fingerprint_field="content_sha256",
                    fingerprint="deadbeef",
                )
            elapsed = time.monotonic() - start
        finally:
            release_lock.set()
            holder_thread.join(timeout=5)

        self.assertIsNotNone(found)
        self.assertEqual(found.id, upload.id)
        self.assertLess(
            elapsed, 2.0, f"dedup check blocked on the locked row for {elapsed:.1f}s instead of falling back"
        )


class ApplySupplierPriceAIResultRowLockRegressionTests(TransactionTestCase):
    """Regression guard for the 2026-08-16 admin-delete hang: apply_supplier_price_ai_result()
    used to hold select_for_update() on the SupplierPriceUpload row for its whole per-product
    loop, blocking anything else touching that row (normalize() retries, deleting the upload
    from /admin/) for as long as the loop ran. Fixed by switching to an advisory lock
    (_acquire_upload_apply_lock) that still serializes concurrent applies for the same upload
    but never locks the row itself."""

    def test_upload_row_stays_writable_while_apply_is_mid_loop(self):
        from api.v1.suppliers import services as suppliers_services

        supplier = _make_supplier()
        unit = _make_unit()
        upload = _make_upload(supplier, status=UploadStatus.PROCESSING)

        entered_loop = threading.Event()
        release_loop = threading.Event()
        # 2026-08-18: was patching _upsert_upload_item — apply()'s per-row
        # loop no longer writes to the DB at all (classify-then-bulk-write,
        # see the module comment above _ApplyBatchState in services.py).
        # _classify_price_upload_row is the new per-row hook, called once
        # per row from inside the same still-open transaction, same
        # blocking-hook role: prove the upload row isn't locked while it's
        # mid-flight.
        original = suppliers_services._classify_price_upload_row
        call_count = {"n": 0}

        def blocking_first_call(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                entered_loop.set()
                release_loop.wait(timeout=5)
            return original(**kwargs)

        results = []
        errors = []

        def worker():
            connections.close_all()
            try:
                products = [_minimal_product_data(raw_name=f"Товар {i}", unit_code=unit.code) for i in range(2)]
                payload = _make_payload(supplier, upload, products)
                with patch.object(suppliers_services.price_apply, "_classify_price_upload_row", blocking_first_call):
                    result = suppliers_services.apply_supplier_price_ai_result(upload_id=upload.id, data=payload)
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        apply_thread = threading.Thread(target=worker)
        apply_thread.start()
        self.assertTrue(entered_loop.wait(timeout=5), "apply never reached the per-product loop")

        try:
            start = time.monotonic()
            deleted_count, _ = SupplierPriceUpload.objects.filter(pk=upload.pk).delete()
            elapsed = time.monotonic() - start
        finally:
            release_loop.set()
            apply_thread.join(timeout=5)

        # The point of this test is that the DELETE itself doesn't hang — not what
        # happens to the apply() call racing against its own row disappearing
        # underneath it (that's a separate, expected consequence of deleting an
        # in-progress upload, not something this fix promises to handle).
        self.assertGreaterEqual(deleted_count, 1, "expected the upload row to actually be deleted")
        self.assertLess(
            elapsed, 2.0, f"deleting the upload row blocked for {elapsed:.1f}s instead of proceeding immediately"
        )


class ApplyProgressReporterTests(TransactionTestCase):
    """_apply_progress_reporter exists to solve exactly one problem: write
    progress that's visible to OTHER connections (e.g. the request serving
    GET /uploads/{id}) WHILE apply_supplier_price_ai_result's own big
    transaction is still open — a plain save() inside that transaction
    would just be a savepoint, invisible to anyone else until the whole
    thing commits (see the function's docstring). TransactionTestCase (real
    commits, no test-wrapping rollback) is required to prove that, the same
    reason the concurrency tests elsewhere in this file need it."""

    def test_progress_is_visible_from_another_connection_before_the_outer_transaction_commits(self):
        from api.v1.suppliers.services import _apply_progress_reporter

        supplier = _make_supplier()
        upload = _make_upload(supplier, status=UploadStatus.PROCESSING)

        written = threading.Event()
        checked = threading.Event()
        reader_result = {}

        def reader():
            connections.close_all()
            written.wait(timeout=5)
            try:
                seen = SupplierPriceUpload.objects.get(pk=upload.pk)
                progress = (seen.raw_payload or {}).get("normalization", {}).get("progress", {})
                reader_result["rows_done"] = progress.get("rows_done")
                reader_result["rows_total"] = progress.get("rows_total")
            except Exception as exc:  # noqa: BLE001
                reader_result["error"] = repr(exc)
            finally:
                connections.close_all()
                checked.set()

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        try:
            with _apply_progress_reporter(upload.id) as progress_callback:
                with transaction.atomic():
                    progress_callback(rows_done=17, rows_total=834)
                    written.set()
                    self.assertTrue(checked.wait(timeout=5), "reader thread never finished checking")
        finally:
            reader_thread.join(timeout=5)

        self.assertEqual(reader_result.get("rows_done"), 17)
        self.assertEqual(reader_result.get("rows_total"), 834)


class _FakeTelegramResponse:
    """Mirrors the FakeResponse pattern used for ai_client/dadata_client
    urllib.request.urlopen mocks elsewhere in this file — _send_telegram_message
    only checks the call succeeded, never reads a body."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok": true}'


@override_settings(TELEGRAM_BOT_TOKEN="test-bot-token")
class SupplierCabinetBotLoginConfirmViewTests(APITestCase):
    """2026-08-18: Ярослав (фронт) — после веб-логина внутри Telegram Mini
    App сайт не сообщал боту, что пользователь вошёл, бот не мог написать
    «Авторизация прошла успешно». bot-user-bindings/direct-register не
    годится — требует HasServiceToken (DEFAULT_PERMISSION_CLASSES), которого
    у фронта нет. Эта вьюха — cabinet-JWT вариант, supplier из токена."""

    def setUp(self):
        self.supplier = _make_supplier()

    def url(self):
        return "/api/v1/suppliers/cabinet/bot-link/confirm-login"

    def _auth(self, supplier=None):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier or self.supplier))

    def test_requires_cabinet_auth(self):
        response = self.client.post(self.url(), {"telegram_user_id": "111", "chat_id": "222"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_creates_new_binding_and_sends_message(self):
        self._auth()
        with patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()) as urlopen_mock:
            response = self.client.post(self.url(), {"telegram_user_id": "111", "chat_id": "222"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["created"])
        self.assertTrue(response.data["notified"])
        self.assertTrue(response.data["message_sent"])
        urlopen_mock.assert_called_once()

        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="222")
        self.assertEqual(binding.supplier_id, self.supplier.id)
        self.assertEqual(binding.external_user_id, "111")
        self.assertEqual(binding.status, EntityStatus.ACTIVE)

    def test_supplier_comes_from_jwt_not_request_body(self):
        """Serializer has no supplier_id field at all — extra keys in the
        body are just ignored by DRF, so the binding can only ever end up
        pointing at request.user (the JWT's own supplier)."""
        other_supplier = _make_supplier(name="Other Co")
        self._auth()
        with patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()):
            response = self.client.post(
                self.url(),
                {"telegram_user_id": "111", "chat_id": "222", "supplier_id": str(other_supplier.id)},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="222")
        self.assertEqual(binding.supplier_id, self.supplier.id)

    def test_second_call_within_dedup_window_does_not_resend(self):
        self._auth()
        with patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()) as urlopen_mock:
            first = self.client.post(self.url(), {"telegram_user_id": "111", "chat_id": "222"}, format="json")
            second = self.client.post(self.url(), {"telegram_user_id": "111", "chat_id": "222"}, format="json")

        self.assertTrue(first.data["notified"])
        self.assertFalse(second.data["created"])
        self.assertFalse(second.data["notified"])
        self.assertFalse(second.data["message_sent"])
        urlopen_mock.assert_called_once()
        self.assertEqual(BotUserBinding.objects.filter(channel=SourceChannel.TELEGRAM, chat_id="222").count(), 1)

    def test_call_after_dedup_window_resends(self):
        self._auth()
        with patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()):
            self.client.post(self.url(), {"telegram_user_id": "111", "chat_id": "222"}, format="json")

        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="222")
        stale = timezone.now() - timedelta(seconds=120)
        binding.metadata = {**binding.metadata, "last_login_confirmed_at": stale.isoformat()}
        binding.save(update_fields=["metadata"])

        with patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()) as urlopen_mock:
            response = self.client.post(self.url(), {"telegram_user_id": "111", "chat_id": "222"}, format="json")

        self.assertTrue(response.data["notified"])
        self.assertTrue(response.data["message_sent"])
        urlopen_mock.assert_called_once()

    def test_conflict_when_chat_bound_to_other_supplier(self):
        other_supplier = _make_supplier(name="Other Co")
        BotUserBinding.objects.create(
            supplier=other_supplier,
            channel=SourceChannel.TELEGRAM,
            chat_id="222",
            external_user_id="999",
            status=EntityStatus.ACTIVE,
        )
        self._auth()
        response = self.client.post(self.url(), {"telegram_user_id": "111", "chat_id": "222"}, format="json")
        self.assertEqual(response.status_code, 409)

    def test_message_send_failure_does_not_fail_the_request(self):
        self._auth()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            response = self.client.post(self.url(), {"telegram_user_id": "111", "chat_id": "222"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["created"])
        self.assertFalse(response.data["message_sent"])
        self.assertTrue(BotUserBinding.objects.filter(channel=SourceChannel.TELEGRAM, chat_id="222").exists())


@override_settings(TELEGRAM_BOT_TOKEN="123:main-bot-token")
class SupplierBotAccountLoginConfirmViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create(
            email="telegram-onboarding@example.com",
            full_name="Telegram Supplier",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )

    def url(self):
        return "/api/v1/suppliers/bot-link/confirm-account"

    def init_data(self, user_id=123456):
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps(
                {"id": user_id, "first_name": "Test", "username": "supplier_test"},
                separators=(",", ":"),
            ),
        }
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", b"123:main-bot-token", hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        return urlencode(fields)

    def test_links_account_without_company_and_notifies_bot(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        with patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()) as urlopen_mock:
            response = self.client.post(self.url(), {"init_data": self.init_data()}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="123456")
        self.assertEqual(binding.user_id, self.user.id)
        self.assertIsNone(binding.supplier_id)
        self.assertEqual(binding.external_user_id, "123456")
        state = BotUserState.objects.get(platform=SourceChannel.TELEGRAM, chat_id="123456")
        self.assertEqual(state.state, "registered")
        urlopen_mock.assert_called_once()

    def test_existing_company_becomes_notification_destination(self):
        from notifications.models import NotificationDestination, NotificationRule
        from notifications.services import SUPPLIER_EVENTS

        supplier = _make_supplier(name="Linked Supplier")
        SupplierUser.objects.create(
            supplier=supplier,
            user=self.user,
            role=SupplierUserRole.OWNER,
            status=EntityStatus.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        with patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()):
            response = self.client.post(self.url(), {"init_data": self.init_data()}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        destination = NotificationDestination.objects.get(chat_id="123456", supplier=supplier)
        self.assertEqual(destination.created_by_id, self.user.id)
        self.assertTrue(destination.is_active)
        self.assertEqual(
            set(NotificationRule.objects.filter(destination=destination).values_list("event", flat=True)),
            set(SUPPLIER_EVENTS),
        )

        disabled_rule = NotificationRule.objects.filter(destination=destination).first()
        disabled_rule.is_enabled = False
        disabled_rule.save(update_fields=["is_enabled"])
        with patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()):
            self.client.post(self.url(), {"init_data": self.init_data()}, format="json")
        disabled_rule.refresh_from_db()
        self.assertFalse(disabled_rule.is_enabled)

    def test_rejects_untrusted_telegram_identity(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.post(self.url(), {"init_data": "auth_date=1&hash=wrong"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(BotUserBinding.objects.exists())

    def test_rejects_malformed_init_data_as_bad_request(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.post(self.url(), {"init_data": "malformed"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(BotUserBinding.objects.exists())


class SupplierMineCreateHTTPTests(APITestCase):
    def setUp(self):
        self.supplier_user = User.objects.create(
            email="fresh-supplier2@example.com",
            full_name="Fresh Supplier",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )

    def url(self):
        return "/api/v1/suppliers/mine"

    def test_requires_authentication(self):
        response = self.client.post(self.url(), {"display_name": "X"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_supplier_creates_own_company(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.supplier_user))
        response = self.client.post(self.url(), {"display_name": "Моя компания"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["display_name"], "Моя компания")
        self.assertEqual(response.data["my_role"], SupplierUserRole.OWNER)
        self.assertTrue(
            SupplierUser.objects.filter(
                supplier_id=response.data["id"], user=self.supplier_user, role=SupplierUserRole.OWNER
            ).exists()
        )

    @override_settings(TELEGRAM_BOT_TOKEN="test-bot-token")
    def test_web_company_creation_selects_company_and_notifies_linked_bot(self):
        from notifications.models import NotificationDestination, NotificationRule
        from notifications.services import SUPPLIER_EVENTS

        binding = BotUserBinding.objects.create(
            supplier=None,
            user=self.supplier_user,
            channel=SourceChannel.TELEGRAM,
            chat_id="222",
            external_user_id="222",
            status=EntityStatus.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.supplier_user))
        with patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()) as urlopen_mock:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url(), {"display_name": "Компания из веба"}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        binding.refresh_from_db()
        self.assertEqual(str(binding.supplier_id), response.data["id"])
        state = BotUserState.objects.get(platform=SourceChannel.TELEGRAM, chat_id="222")
        self.assertEqual(state.state, "supplier_registered")
        self.assertEqual(state.payload["selected_company_id"], response.data["id"])
        destination = NotificationDestination.objects.get(chat_id="222", supplier_id=response.data["id"])
        self.assertEqual(destination.created_by_id, self.supplier_user.id)
        self.assertEqual(
            set(NotificationRule.objects.filter(destination=destination).values_list("event", flat=True)),
            set(SUPPLIER_EVENTS),
        )
        urlopen_mock.assert_called_once()

    def test_venue_user_cannot_self_create(self):
        venue_user = User.objects.create(
            email="venue@example.com", full_name="Venue", role=UserRole.VENUE_USER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(venue_user))
        response = self.client.post(self.url(), {"display_name": "X"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_second_call_creates_an_additional_company(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.supplier_user))
        first = self.client.post(self.url(), {"display_name": "Первая"}, format="json")
        second = self.client.post(self.url(), {"display_name": "Вторая"}, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(first.data["id"], second.data["id"])

        get_response = self.client.get(self.url())
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual({item["id"] for item in get_response.data}, {first.data["id"], second.data["id"]})

    def test_created_company_is_immediately_visible_via_get(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.supplier_user))
        create_response = self.client.post(self.url(), {"display_name": "Моя компания"}, format="json")
        get_response = self.client.get(self.url())
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual([item["id"] for item in get_response.data], [create_response.data["id"]])


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierAccountTransferJWTHTTPTests(APITestCase):
    """The account-transfer endpoint also accepts a snabzhenets JWT instead
    of the shared service token — covers the OR-permission and the
    initiated_by_user_id-from-request.user wiring end to end."""

    def setUp(self):
        self.staff = _make_admin_user()

    def test_owner_can_initiate_without_service_token(self):
        from api.v1.suppliers.services import create_supplier_by_staff

        supplier = create_supplier_by_staff(created_by_user=self.staff, display_name="Моя компания")

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.post(
            f"/api/v1/suppliers/{supplier.id}/account-transfer",
            {"target_email": "real-owner@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        transfer = SupplierAccountTransfer.objects.get(id=response.data["id"])
        self.assertEqual(transfer.initiated_by_id, self.staff.id)

    def test_unrelated_supplier_user_cannot_initiate(self):
        other_owner = _make_admin_user(email="other@example.com")
        supplier = _make_supplier()
        SupplierUser.objects.create(
            supplier=supplier,
            user=other_owner,
            role=SupplierUserRole.OWNER,
            status=EntityStatus.ACTIVE,
        )
        outsider = User.objects.create(
            email="outsider@example.com",
            full_name="Outsider",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(outsider))
        response = self.client.post(
            f"/api/v1/suppliers/{supplier.id}/account-transfer",
            {"target_email": "real-owner@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class SupplierRestoreHTTPTests(APITestCase):
    """2026-07-29: "а можно ли восстановить удалённую компанию через
    кабинет" — нет, cabinet-JWT заблокирован при status != active, поэтому
    восстановление идёт через обычный core.User JWT + членство, как и
    account-transfer (тот же уровень доступа — CanInitiateAccountTransfer /
    CanRestoreSupplier построены по одному образцу)."""

    def url(self, supplier_id):
        return f"/api/v1/suppliers/{supplier_id}/restore"

    def _archived_supplier_with_owner(self, owner):
        supplier = _make_supplier()
        SupplierUser.objects.create(
            supplier=supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        supplier.status = EntityStatus.ARCHIVED
        supplier.is_published = False
        supplier.save(update_fields=["status", "is_published", "updated_at"])
        return supplier

    def test_owner_can_restore(self):
        owner = User.objects.create(email="restore-owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        supplier = self._archived_supplier_with_owner(owner)

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.post(self.url(supplier.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "active")
        self.assertTrue(response.data["is_published"])
        supplier.refresh_from_db()
        self.assertEqual(supplier.status, "active")
        self.assertTrue(supplier.is_published)

    def test_manager_member_can_restore_too(self):
        """2026-08-05: owner и manager имеют одинаковые права."""
        owner = User.objects.create(email="restore-owner2@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        supplier = self._archived_supplier_with_owner(owner)
        manager = User.objects.create(email="restore-manager@example.com", full_name="Manager", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(manager))
        response = self.client.post(self.url(supplier.id))

        self.assertEqual(response.status_code, 200)
        supplier.refresh_from_db()
        self.assertEqual(supplier.status, "active")

    def test_unrelated_user_cannot_restore(self):
        owner = User.objects.create(email="restore-owner3@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        supplier = self._archived_supplier_with_owner(owner)
        outsider = User.objects.create(
            email="restore-outsider@example.com", full_name="Outsider", role=UserRole.SUPPLIER
        )

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(outsider))
        response = self.client.post(self.url(supplier.id))

        self.assertEqual(response.status_code, 403)

    def test_staff_can_restore_without_being_owner(self):
        owner = User.objects.create(email="restore-owner4@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        supplier = self._archived_supplier_with_owner(owner)
        staff = _make_admin_user(email="restore-staff@example.com")

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(staff))
        response = self.client.post(self.url(supplier.id))

        self.assertEqual(response.status_code, 200)

    def test_rejects_restoring_a_non_archived_company(self):
        owner = User.objects.create(email="restore-owner5@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        supplier = _make_supplier()
        SupplierUser.objects.create(
            supplier=supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.post(self.url(supplier.id))

        self.assertEqual(response.status_code, 400)

    def test_requires_auth(self):
        owner = User.objects.create(email="restore-owner6@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        supplier = self._archived_supplier_with_owner(owner)

        response = self.client.post(self.url(supplier.id))

        self.assertEqual(response.status_code, 401)

    def test_restored_company_can_issue_working_cabinet_session(self):
        """Замыкает цикл: после восстановления cabinet-JWT снова работает —
        то, что было заблокировано сразу после удаления."""
        from api.v1.suppliers.services import issue_supplier_cabinet_tokens

        owner = User.objects.create(email="restore-owner7@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        supplier = self._archived_supplier_with_owner(owner)

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        restore_response = self.client.post(self.url(supplier.id))
        self.assertEqual(restore_response.status_code, 200)

        supplier.refresh_from_db()
        tokens = issue_supplier_cabinet_tokens(supplier)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        cabinet_response = self.client.get("/api/v1/suppliers/cabinet/me")
        self.assertEqual(cabinet_response.status_code, 200)


# ---------------------------------------------------------------------------
# archive_supplier_product
# ---------------------------------------------------------------------------


class ArchiveSupplierProductServiceTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()

    def _make_sp(self, status=EntityStatus.ACTIVE):
        return SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            status=status,
        )

    def test_archives_active_product(self):
        from api.v1.suppliers.services import archive_supplier_product

        sp = self._make_sp()
        archive_supplier_product(supplier_product_id=sp.id)
        sp.refresh_from_db()
        self.assertEqual(sp.status, EntityStatus.ARCHIVED)

    def test_cancels_active_price(self):
        from api.v1.suppliers.services import archive_supplier_product

        sp = self._make_sp()
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("99.00"),
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )
        archive_supplier_product(supplier_product_id=sp.id)
        price = SupplierProductPrice.objects.get(supplier_product=sp)
        self.assertEqual(price.status, PriceStatus.CANCELLED)
        self.assertIsNotNone(price.valid_to)

    def test_leaves_superseded_prices_untouched(self):
        from api.v1.suppliers.services import archive_supplier_product

        sp = self._make_sp()
        SupplierProductPrice.objects.create(
            supplier_product=sp,
            price=Decimal("50.00"),
            status=PriceStatus.SUPERSEDED,
            price_type=PriceType.PRICE_LIST,
        )
        archive_supplier_product(supplier_product_id=sp.id)
        price = SupplierProductPrice.objects.get(supplier_product=sp)
        self.assertEqual(price.status, PriceStatus.SUPERSEDED)

    def test_idempotent_on_already_archived(self):
        from api.v1.suppliers.services import archive_supplier_product

        sp = self._make_sp(status=EntityStatus.ARCHIVED)
        archive_supplier_product(supplier_product_id=sp.id)
        sp.refresh_from_db()
        self.assertEqual(sp.status, EntityStatus.ARCHIVED)

    def test_raises_404_for_unknown_product(self):
        from django.http import Http404

        from api.v1.suppliers.services import archive_supplier_product

        with self.assertRaises(Http404):
            archive_supplier_product(supplier_product_id=uuid4())

    def test_cancels_multiple_active_prices(self):
        from api.v1.suppliers.services import archive_supplier_product

        sp = self._make_sp()
        for price_val in ["10.00", "20.00", "30.00"]:
            SupplierProductPrice.objects.create(
                supplier_product=sp,
                price=Decimal(price_val),
                status=PriceStatus.ACTIVE,
                price_type=PriceType.PRICE_LIST,
            )
        archive_supplier_product(supplier_product_id=sp.id)
        active_count = SupplierProductPrice.objects.filter(supplier_product=sp, status=PriceStatus.ACTIVE).count()
        self.assertEqual(active_count, 0)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductAuditLogWriteTests(APITestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(legal_name="ООО Ромашка", display_name="Ромашка")

    def test_archive_records_audit_log_with_scope_id(self):
        from api.v1.suppliers.services import archive_supplier_product

        sp = SupplierProduct.objects.create(supplier=self.supplier, normalized_name="Мука", status=EntityStatus.ACTIVE)
        archive_supplier_product(supplier_product_id=sp.id, actor=self.supplier)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.SUPPLIER_PRODUCT, entity_id=sp.id)
        self.assertEqual(log.action, AuditLogAction.ARCHIVED)
        self.assertEqual(log.scope_id, self.supplier.id)
        self.assertEqual(log.actor_type, AuditActorType.SUPPLIER)

    def test_delete_records_audit_log_as_archived(self):
        from api.v1.suppliers.services import delete_supplier_product

        sp = SupplierProduct.objects.create(supplier=self.supplier, normalized_name="Крупа", status=EntityStatus.ACTIVE)
        sp_id = sp.id
        delete_supplier_product(supplier_product_id=sp_id, actor=self.supplier)
        sp.refresh_from_db()
        self.assertEqual(sp.status, EntityStatus.ARCHIVED)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.SUPPLIER_PRODUCT, entity_id=sp_id)
        self.assertEqual(log.action, AuditLogAction.ARCHIVED)
        self.assertEqual(log.summary, "Крупа")
        self.assertEqual(log.scope_id, self.supplier.id)

    def test_delete_via_api_records_actor_from_cabinet_jwt(self):
        sp = SupplierProduct.objects.create(supplier=self.supplier, normalized_name="Овёс", status=EntityStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        response = self.client.delete(f"/api/v1/suppliers/products/{sp.id}")
        self.assertEqual(response.status_code, 204)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.SUPPLIER_PRODUCT, entity_id=sp.id)
        self.assertEqual(log.actor_id, self.supplier.id)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductAuditLogReadTests(APITestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(legal_name="ООО Ромашка", display_name="Ромашка")

    def test_owner_reads_history_after_delete(self):
        from api.v1.suppliers.services import delete_supplier_product

        sp = SupplierProduct.objects.create(
            supplier=self.supplier, normalized_name="Гвоздика", status=EntityStatus.ACTIVE
        )
        sp_id = sp.id
        delete_supplier_product(supplier_product_id=sp_id, actor=self.supplier)
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        response = self.client.get(f"/api/v1/suppliers/products/{sp_id}/audit-log")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["action"], "archived")

    def test_different_supplier_gets_404_after_delete(self):
        from api.v1.suppliers.services import delete_supplier_product

        sp = SupplierProduct.objects.create(
            supplier=self.supplier, normalized_name="Бадьян", status=EntityStatus.ACTIVE
        )
        sp_id = sp.id
        delete_supplier_product(supplier_product_id=sp_id, actor=self.supplier)
        other_supplier = Supplier.objects.create(legal_name="ООО Полынь", display_name="Полынь")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(other_supplier))
        response = self.client.get(f"/api/v1/suppliers/products/{sp_id}/audit-log")
        self.assertEqual(response.status_code, 404)

    def test_service_token_reads_any_history(self):
        from api.v1.suppliers.services import delete_supplier_product

        sp = SupplierProduct.objects.create(
            supplier=self.supplier, normalized_name="Кардамон", status=EntityStatus.ACTIVE
        )
        sp_id = sp.id
        delete_supplier_product(supplier_product_id=sp_id, actor=self.supplier)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.get(f"/api/v1/suppliers/products/{sp_id}/audit-log")
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# update_supplier_product
# ---------------------------------------------------------------------------


class UpdateSupplierProductServiceTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            status=EntityStatus.ACTIVE,
        )

    def _update(self, data):
        from api.v1.suppliers.services import update_supplier_product

        return update_supplier_product(supplier_product_id=self.sp.id, data=data)

    def test_updates_supplier_raw_name(self):
        self._update({"supplier_raw_name": "Болт М10"})
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.supplier_name, "Болт М10")

    def test_updates_comment(self):
        self._update({"comment": "Оригинал"})
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.comment, "Оригинал")

    def test_updates_package(self):
        self._update({"package": "упаковка 10 шт"})
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.package, "упаковка 10 шт")

    def test_applies_new_price(self):
        self._update({"price": Decimal("500.00")})
        price = SupplierProductPrice.objects.filter(supplier_product=self.sp, status=PriceStatus.ACTIVE).first()
        self.assertIsNotNone(price)
        self.assertEqual(price.price, Decimal("500.00"))

    def test_supersedes_old_price_on_update(self):
        SupplierProductPrice.objects.create(
            supplier_product=self.sp,
            price=Decimal("100.00"),
            status=PriceStatus.ACTIVE,
            price_type=PriceType.PRICE_LIST,
        )
        self._update({"price": Decimal("200.00")})
        old = SupplierProductPrice.objects.get(supplier_product=self.sp, price=Decimal("100.00"))
        self.assertEqual(old.status, PriceStatus.SUPERSEDED)

    def test_sets_brand(self):
        self._update({"brand": "SKF"})
        self.sp.refresh_from_db()
        self.assertIsNotNone(self.sp.brand)
        self.assertEqual(self.sp.brand.canonical_name, "SKF")

    def test_clears_brand_when_empty_string(self):
        brand = Brand.objects.create(canonical_name="Old Brand", normalized_name="old brand")
        self.sp.brand = brand
        self.sp.save()
        self._update({"brand": ""})
        self.sp.refresh_from_db()
        self.assertIsNone(self.sp.brand)

    def test_sets_category_path(self):
        self._update({"category_path": ["Автозапчасти", "Фильтры"]})
        self.sp.refresh_from_db()
        self.assertIsNotNone(self.sp.category)
        self.assertEqual(self.sp.category.name, "Фильтры")
        self.assertEqual(self.sp.category.parent.name, "Автозапчасти")

    def test_patch_image_url_syncs_gallery_position_zero_row(self):
        # 2026-09-21 final-review fix wave, Fix 3: PATCH .../products/{id}'s
        # image_url branch predates Task 5.2's gallery and used to write
        # image_url directly, with zero gallery rows.
        self._update({"image_url": "https://example.com/legacy-photo.jpg"})
        self.sp.refresh_from_db()
        main_image = SupplierProductImage.objects.filter(supplier_product=self.sp, position=0).first()
        self.assertIsNotNone(main_image)
        self.assertEqual(main_image.image_url, "https://example.com/legacy-photo.jpg")

    def test_patch_image_url_updates_existing_position_zero_row_not_create_second(self):
        SupplierProductImage.objects.create(
            supplier_product=self.sp, image_url="https://example.com/old.jpg", position=0
        )
        self._update({"image_url": "https://example.com/new.jpg"})
        images = list(SupplierProductImage.objects.filter(supplier_product=self.sp, position=0))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].image_url, "https://example.com/new.jpg")

    def test_raises_404_for_unknown_product(self):
        from django.http import Http404

        from api.v1.suppliers.services import update_supplier_product

        with self.assertRaises(Http404):
            update_supplier_product(supplier_product_id=uuid4(), data={"comment": "x"})

    def test_no_op_when_empty_data(self):
        self._update({})
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.supplier_name, "Болт М8")


# ---------------------------------------------------------------------------
# create_supplier_price_upload_from_text
# ---------------------------------------------------------------------------


class CreateSupplierPriceUploadFromTextTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier("Text Upload Supplier")

    def _create(self, text="Болт М8 150 руб шт", **kwargs):
        from api.v1.suppliers.services import create_supplier_price_upload_from_text

        data = {
            "supplier": self.supplier,
            "source_channel": SourceChannel.TELEGRAM,
            "text": text,
            **kwargs,
        }
        return create_supplier_price_upload_from_text(data=data)

    def test_creates_upload_with_received_status(self):
        upload = self._create()
        self.assertEqual(upload.status, UploadStatus.RECEIVED)

    def test_stores_text_in_raw_payload(self):
        upload = self._create(text="Гайка М10 75 руб шт")
        self.assertEqual((upload.raw_payload or {}).get("text"), "Гайка М10 75 руб шт")

    def test_raw_payload_kind_is_text(self):
        upload = self._create()
        self.assertEqual((upload.raw_payload or {}).get("kind"), "text")

    def test_sha256_stored_in_metadata(self):
        upload = self._create()
        metadata = (upload.raw_payload or {}).get("metadata", {})
        self.assertIn("text_sha256", metadata)

    def test_deduplicates_by_text_sha256(self):
        upload1 = self._create(text="одинаковый текст")
        upload2 = self._create(text="одинаковый текст")
        self.assertEqual(upload1.id, upload2.id)

    def test_different_text_creates_new_upload(self):
        upload1 = self._create(text="текст один")
        upload2 = self._create(text="текст два")
        self.assertNotEqual(upload1.id, upload2.id)

    def test_deduplicates_by_n8n_execution_id(self):
        upload1 = self._create(text="текст", n8n_execution_id="exec-abc")
        upload2 = self._create(text="текст иной", n8n_execution_id="exec-abc")
        self.assertEqual(upload1.id, upload2.id)

    def test_optional_source_file_name_stored(self):
        upload = self._create(source_file_name="price.txt")
        self.assertEqual(upload.source_file_name, "price.txt")


# ---------------------------------------------------------------------------
# 2026-07-11: задачи с созвона (availability toggle, staff-список поставщиков,
# новые поля карточки компании, инвайт сотрудника, регенерация invite-кода,
# dadata lookup по ИНН)
# ---------------------------------------------------------------------------


class UpdateSupplierProductAvailabilityTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.unit = _make_unit()
        self.sp = SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Болт М8",
            normalized_name="болт м8",
            status=EntityStatus.ACTIVE,
            availability=ProductAvailability.IN_STOCK,
        )

    def test_service_updates_availability(self):
        from api.v1.suppliers.services import update_supplier_product

        update_supplier_product(supplier_product_id=self.sp.id, data={"availability": ProductAvailability.OUT_OF_STOCK})
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.availability, ProductAvailability.OUT_OF_STOCK)

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_http_patch_updates_availability(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.patch(
            f"/api/v1/suppliers/products/{self.sp.id}",
            {"availability": "out_of_stock"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.availability, ProductAvailability.OUT_OF_STOCK)

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_http_rejects_invalid_availability_value(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.patch(
            f"/api/v1/suppliers/products/{self.sp.id}",
            {"availability": "kind-of-in-stock"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class SupplierListStaffHTTPTests(APITestCase):
    url = "/api/v1/suppliers/"

    def setUp(self):
        self.staff = _make_admin_user()

    def test_requires_authentication(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_non_staff_forbidden(self):
        supplier_user = User.objects.create(
            email="not-staff@example.com", full_name="X", role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(supplier_user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_staff_sees_all_suppliers_not_just_own(self):
        from api.v1.suppliers.services import create_supplier_by_staff

        create_supplier_by_staff(created_by_user=self.staff, display_name="Моя компания")
        _make_supplier(name="Чужая компания")

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        names = {r["display_name"] for r in response.data["results"]}
        self.assertEqual(names, {"Моя компания", "Чужая компания"})

    def test_pagination_limit(self):
        for i in range(3):
            _make_supplier(name=f"Company {i}")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.get(self.url, {"limit": 2})
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 2)


class SupplierCompanyFieldsTests(APITestCase):
    """delivery_cost / delivery_zone — задачи 6+7."""

    def setUp(self):
        self.staff = _make_admin_user()

    def test_create_accepts_new_fields(self):
        from api.v1.suppliers.services import create_supplier_by_staff

        supplier = create_supplier_by_staff(
            created_by_user=self.staff,
            display_name="ООО Ромашка",
            delivery_cost=Decimal("300.00"),
            delivery_zone="Москва и МО",
        )
        self.assertEqual(supplier.delivery_cost, Decimal("300.00"))
        self.assertEqual(supplier.delivery_zone, "Москва и МО")

    def test_cabinet_update_patches_new_fields(self):
        from api.v1.suppliers.services import update_supplier_cabinet

        supplier = _make_supplier()
        update_supplier_cabinet(
            supplier=supplier,
            data={"delivery_cost": Decimal("150.50"), "delivery_zone": "СПб"},
        )
        supplier.refresh_from_db()
        self.assertEqual(supplier.delivery_cost, Decimal("150.50"))
        self.assertEqual(supplier.delivery_zone, "СПб")

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_http_create_returns_new_fields(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.post(
            "/api/v1/suppliers/",
            {"display_name": "ООО Тест", "delivery_cost": "99.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["delivery_cost"], "99.00")

    def test_cabinet_update_patches_registry_fields(self):
        """2026-07-27: дорастили Supplier под поля из dadata (ОКВЭД/ОКПО/дата
        регистрации/гендиректор) — они должны быть редактируемы через
        PATCH cabinet/me, не только заполняемы при создании."""
        from api.v1.suppliers.services import update_supplier_cabinet

        supplier = _make_supplier()
        update_supplier_cabinet(
            supplier=supplier,
            data={
                "kpp": "770701001",
                "ogrn": "1027700132195",
                "legal_address": "г. Москва, ул. Тестовая, д. 1",
                "okved": "46.90",
                "okpo": "12345678",
                "registration_date": "2015-03-12",
                "general_director": "Петров Пётр Петрович",
            },
        )
        supplier.refresh_from_db()
        self.assertEqual(supplier.kpp, "770701001")
        self.assertEqual(supplier.ogrn, "1027700132195")
        self.assertEqual(supplier.legal_address, "г. Москва, ул. Тестовая, д. 1")
        self.assertEqual(supplier.okved, "46.90")
        self.assertEqual(supplier.okpo, "12345678")
        self.assertEqual(str(supplier.registration_date), "2015-03-12")
        self.assertEqual(supplier.general_director, "Петров Пётр Петрович")


@override_settings(SERVICE_API_TOKEN="test-token")
class CabinetTokenViewTests(APITestCase):
    url = "/api/v1/suppliers/cabinet/token"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_binding(self, *, chat_id="chat-1", external_user_id="user-1", status=EntityStatus.ACTIVE):
        supplier = Supplier.objects.create(legal_name="Supplier LLC", display_name="Supplier")
        return BotUserBinding.objects.create(
            supplier=supplier,
            channel=SourceChannel.TELEGRAM,
            chat_id=chat_id,
            external_user_id=external_user_id,
            status=status,
        )

    def test_rejects_without_service_token(self):
        response = self.client.post(self.url, {"chat_id": "chat-1"}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_rejects_without_chat_id_or_telegram_user_id(self):
        self.authenticate()

        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_returns_token_by_chat_id(self):
        binding = self.create_binding()
        self.authenticate()

        response = self.client.post(self.url, {"chat_id": "chat-1"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["supplier_id"], str(binding.supplier_id))
        self.assertTrue(CabinetSession.objects.filter(token=response.json()["token"], binding=binding).exists())

    def test_falls_back_to_telegram_user_id_when_chat_id_is_stale(self):
        # Reproduces the "token=undefined" bug: BotUserBindingResolveView
        # already matches by external_user_id when chat_id is stale, so
        # Route Message considers the user registered — but the old
        # chat_id-only lookup here 404'd regardless, breaking the cabinet
        # link. Generate Cabinet Token (n8n) must send telegram_user_id too.
        binding = self.create_binding(chat_id="old-chat", external_user_id="user-1")
        self.authenticate()

        response = self.client.post(self.url, {"chat_id": "new-chat", "telegram_user_id": "user-1"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["supplier_id"], str(binding.supplier_id))

    def test_returns_404_when_no_binding_matches(self):
        self.authenticate()

        response = self.client.post(self.url, {"chat_id": "missing"}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_ignores_inactive_binding(self):
        self.create_binding(status=EntityStatus.INACTIVE)
        self.authenticate()

        response = self.client.post(self.url, {"chat_id": "chat-1"}, format="json")

        self.assertEqual(response.status_code, 404)


class SupplierBotLinkTokenTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()

    def test_service_creates_token_and_deep_link(self):
        from api.v1.suppliers.services import create_supplier_bot_link_token

        result = create_supplier_bot_link_token(supplier=self.supplier)
        self.assertEqual(SupplierBotLinkToken.objects.count(), 1)
        link_token = SupplierBotLinkToken.objects.get()
        self.assertEqual(result["token"], link_token.token)
        self.assertEqual(result["deep_link"], f"https://t.me/ddas_autosnab_price_bot?start={link_token.token}")
        self.assertFalse(link_token.used)

    def test_service_issues_a_fresh_token_each_call(self):
        from api.v1.suppliers.services import create_supplier_bot_link_token

        first = create_supplier_bot_link_token(supplier=self.supplier)
        second = create_supplier_bot_link_token(supplier=self.supplier)
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual(SupplierBotLinkToken.objects.count(), 2)

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_http_requires_service_token_or_owner(self):
        response = self.client.post(f"/api/v1/suppliers/{self.supplier.id}/bot-link-token")
        self.assertEqual(response.status_code, 401)

    @override_settings(SERVICE_API_TOKEN="test-token")
    def test_http_with_service_token(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.post(f"/api/v1/suppliers/{self.supplier.id}/bot-link-token")
        self.assertEqual(response.status_code, 201)
        self.assertIn("deep_link", response.data)
        self.assertIn("token", response.data)
        self.assertIn("expires_at", response.data)

    def test_owner_can_request_link_without_service_token(self):
        owner = User.objects.create(email="link-owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.post(f"/api/v1/suppliers/{self.supplier.id}/bot-link-token")
        self.assertEqual(response.status_code, 201)
        link_token = SupplierBotLinkToken.objects.get()
        self.assertEqual(link_token.created_by_id, owner.id)

    def test_unrelated_supplier_user_cannot_request_link(self):
        outsider = User.objects.create(email="link-outsider@example.com", full_name="X", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(outsider))
        response = self.client.post(f"/api/v1/suppliers/{self.supplier.id}/bot-link-token")
        self.assertEqual(response.status_code, 403)


class SupplierUserInviteServiceTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()

    def _invite(self, email="employee@example.com", role=SupplierUserRole.MANAGER):
        from api.v1.suppliers.services import invite_supplier_user

        return invite_supplier_user(supplier_id=self.supplier.id, target_email=email, role=role)

    def _accept(self, token, user=None):
        """2026-08-07: accept теперь требует уже авторизованного user — по
        умолчанию заводит (или переиспользует) supplier-аккаунт с email,
        совпадающим с target_email инвайта, чтобы не дублировать это в
        каждом тесте, которому неважно, кто именно принимает."""
        from api.v1.suppliers.services import accept_supplier_user_invite

        if user is None:
            invite = SupplierUserInvite.objects.filter(token=token).first()
            target_email = invite.target_email if invite else "unused@example.com"
            user = User.objects.filter(email__iexact=target_email, role=UserRole.SUPPLIER).first()
            if user is None:
                user = User.objects.create(
                    email=target_email, full_name=target_email, role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
                )
        return accept_supplier_user_invite(token=token, user=user)

    def _preview(self, token):
        from api.v1.suppliers.services import get_supplier_user_invite_preview

        return get_supplier_user_invite_preview(token=token)

    def test_preview_returns_target_email_without_mutating_status(self):
        invite = self._invite(email="preview@example.com")
        result = self._preview(invite.token)
        self.assertEqual(result["target_email"], "preview@example.com")
        self.assertEqual(result["supplier_name"], self.supplier.display_name)
        self.assertIsNone(result["supplier_inn"])
        self.assertEqual(result["status"], SupplierUserInviteStatus.PENDING)
        self.assertFalse(result["is_expired"])
        self.assertFalse(result["account_exists"])
        invite.refresh_from_db()
        self.assertEqual(invite.status, SupplierUserInviteStatus.PENDING)

    def test_preview_includes_supplier_inn(self):
        self.supplier.inn = "7707083893"
        self.supplier.save(update_fields=["inn"])
        invite = self._invite(email="preview-inn@example.com")
        result = self._preview(invite.token)
        self.assertEqual(result["supplier_inn"], "7707083893")

    def test_preview_account_exists_true_for_registered_email(self):
        """2026-08-06: не залогинен, но target_email уже зарегистрирован — фронт
        не должен показывать форму создания пароля, accept всё равно его
        проигнорирует."""
        User.objects.create(email="already-registered@example.com", full_name="Existing", role=UserRole.SUPPLIER)
        invite = self._invite(email="already-registered@example.com")
        result = self._preview(invite.token)
        self.assertTrue(result["account_exists"])

    def test_preview_account_exists_case_insensitive(self):
        User.objects.create(email="Mixed-Case@example.com", full_name="Existing", role=UserRole.SUPPLIER)
        invite = self._invite(email="mixed-case@example.com")
        result = self._preview(invite.token)
        self.assertTrue(result["account_exists"])

    def test_preview_is_expired_true_but_status_untouched(self):
        invite = self._invite(email="expired-preview@example.com")
        invite.expires_at = timezone.now() - timezone.timedelta(hours=1)
        invite.save(update_fields=["expires_at"])

        result = self._preview(invite.token)
        self.assertTrue(result["is_expired"])
        self.assertEqual(result["status"], SupplierUserInviteStatus.PENDING)  # preview doesn't mutate

        invite.refresh_from_db()
        self.assertEqual(invite.status, SupplierUserInviteStatus.PENDING)

    def test_preview_unknown_token_returns_404(self):
        from django.http import Http404

        with self.assertRaises(Http404):
            self._preview("unknown-token")

    def test_invite_creates_pending(self):
        invite = self._invite()
        self.assertEqual(invite.status, SupplierUserInviteStatus.PENDING)
        self.assertEqual(invite.role, SupplierUserRole.MANAGER)

    def test_smtp_failure_raises_email_delivery_error_and_rolls_back(self):
        import smtplib

        from api.v1.auth.services import EmailDeliveryError

        with patch("api.v1.suppliers.services.team.send_mail", side_effect=smtplib.SMTPRecipientsRefused({})):
            with self.assertRaises(EmailDeliveryError):
                self._invite(email="smtp-fail@example.com")
        self.assertFalse(
            SupplierUserInvite.objects.filter(supplier=self.supplier, target_email="smtp-fail@example.com").exists()
        )

    def test_invite_sends_email(self):
        from django.core import mail

        self._invite(email="teammate@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("teammate@example.com", mail.outbox[0].to)

    def test_invite_cancels_previous_pending_invite_same_email(self):
        first = self._invite(email="dup@example.com")
        second = self._invite(email="dup@example.com")
        first.refresh_from_db()
        self.assertEqual(first.status, SupplierUserInviteStatus.CANCELLED)
        self.assertEqual(second.status, SupplierUserInviteStatus.PENDING)

    def test_accept_creates_manager_without_touching_owner(self):
        owner = User.objects.create(email="owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )

        invite = self._invite(email="manager@example.com")
        self._accept(invite.token)

        owner_su = SupplierUser.objects.get(supplier=self.supplier, user=owner)
        self.assertEqual(owner_su.status, EntityStatus.ACTIVE)  # untouched

        new_user = User.objects.get(email="manager@example.com")
        manager_su = SupplierUser.objects.get(supplier=self.supplier, user=new_user)
        self.assertEqual(manager_su.role, SupplierUserRole.MANAGER)
        self.assertEqual(manager_su.status, EntityStatus.ACTIVE)

    def test_invite_role_defaults_to_manager(self):
        invite = self._invite()
        self.assertEqual(invite.role, SupplierUserRole.MANAGER)

    def test_invite_accepts_owner_role(self):
        invite = self._invite(email="new-owner@example.com", role=SupplierUserRole.OWNER)
        self.assertEqual(invite.role, SupplierUserRole.OWNER)

    def test_accept_owner_invite_demotes_previous_owner_to_manager(self):
        """2026-08-07 (исправлено по явному указанию пользователя): владелец
        компании только один, но при передаче прав старый owner понижается
        до manager, а не теряет доступ к компании."""
        old_owner = User.objects.create(email="old-owner@example.com", full_name="Old Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=old_owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )

        invite = self._invite(email="new-owner@example.com", role=SupplierUserRole.OWNER)
        self._accept(invite.token)

        old_owner_su = SupplierUser.objects.get(supplier=self.supplier, user=old_owner)
        self.assertEqual(old_owner_su.role, SupplierUserRole.MANAGER)
        self.assertEqual(old_owner_su.status, EntityStatus.ACTIVE)

        new_owner = User.objects.get(email="new-owner@example.com")
        new_owner_su = SupplierUser.objects.get(supplier=self.supplier, user=new_owner)
        self.assertEqual(new_owner_su.role, SupplierUserRole.OWNER)
        self.assertEqual(new_owner_su.status, EntityStatus.ACTIVE)

    def test_accept_attaches_already_registered_matching_account(self):
        """2026-08-07 (по явному решению пользователя): accept больше не
        создаёт/логинит аккаунт по паролю — только прикрепляет уже
        зарегистрированного (обычной регистрацией) авторизованного юзера,
        чей email совпадает с target_email."""
        existing = User.objects.create(
            email="already-registered@example.com",
            full_name="Already Registered",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        invite = self._invite(email="already-registered@example.com")
        result = self._accept(invite.token, user=existing)

        self.assertEqual(result["user_id"], existing.id)
        self.assertNotIn("created", result)
        self.assertTrue(
            SupplierUser.objects.filter(supplier=self.supplier, user=existing, status=EntityStatus.ACTIVE).exists()
        )

    def test_accept_authenticated_different_email_raises_error(self):
        """Залогинен, но не тем email, на который адресован инвайт — 400,
        не тихая подмена аккаунта."""
        from rest_framework.exceptions import ValidationError as DRFValidationError

        someone_else = User.objects.create(
            email="someone-else@example.com", full_name="Someone Else", role=UserRole.SUPPLIER
        )
        invite = self._invite(email="intended-recipient@example.com")
        with self.assertRaises(DRFValidationError):
            self._accept(invite.token, user=someone_else)
        invite.refresh_from_db()
        self.assertEqual(invite.status, SupplierUserInviteStatus.PENDING)

    def test_accept_manager_invite_blocked_when_recipient_is_last_owner(self):
        """2026-08-07, реальный баг от Вити: "если юзер сам себе ссылку менеджера
        отправит под ролью владельца, в компании не будет владельца" — тот же
        баг воспроизведён и для email-инвайта. update_or_create молча
        перезаписывал role уже существующего членства; если принимающий уже
        единственный active owner, а инвайт role=manager, компания оставалась
        без owner'а. Подтверждено эмпирически на реальном Postgres до фикса."""
        from rest_framework.exceptions import ValidationError as DRFValidationError

        owner = User.objects.create(email="self-invite-owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        invite = self._invite(email="self-invite-owner@example.com", role=SupplierUserRole.MANAGER)

        with self.assertRaises(DRFValidationError):
            self._accept(invite.token, user=owner)

        membership = SupplierUser.objects.get(supplier=self.supplier, user=owner)
        self.assertEqual(membership.role, SupplierUserRole.OWNER)
        self.assertEqual(membership.status, EntityStatus.ACTIVE)
        invite.refresh_from_db()
        self.assertEqual(invite.status, SupplierUserInviteStatus.PENDING)

    def test_accept_marks_invite_claimed(self):
        invite = self._invite()
        self._accept(invite.token)
        invite.refresh_from_db()
        self.assertEqual(invite.status, SupplierUserInviteStatus.CLAIMED)
        self.assertIsNotNone(invite.claimed_at)

    def test_accept_twice_raises_validation_error(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        invite = self._invite()
        self._accept(invite.token)
        with self.assertRaises(DRFValidationError):
            self._accept(invite.token)

    def test_accept_cancelled_invite_raises_validation_error(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        stale = self._invite(email="same@example.com")
        fresh = self._invite(email="same@example.com")  # cancels `stale` — same (supplier, email)
        stale.refresh_from_db()
        self.assertEqual(stale.status, SupplierUserInviteStatus.CANCELLED)

        with self.assertRaises(DRFValidationError):
            self._accept(stale.token)
        # the superseding invite still works fine
        self._accept(fresh.token)

    def test_accept_expired_invite_raises_validation_error(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        invite = self._invite()
        invite.expires_at = timezone.now() - timezone.timedelta(hours=1)
        invite.save(update_fields=["expires_at"])
        with self.assertRaises(DRFValidationError):
            self._accept(invite.token)
        invite.refresh_from_db()
        self.assertEqual(invite.status, SupplierUserInviteStatus.EXPIRED)

    def test_accept_unknown_token_returns_404(self):
        from django.http import Http404

        with self.assertRaises(Http404):
            self._accept("does-not-exist-token")

    def test_accept_uses_supplier_account_even_when_venue_user_shares_email(self):
        """2026-08-06: "человек с одним email может быть и поставщиком, и
        покупателем" — venue_user-аккаунт с тем же email не должен путать
        accept: он прикрепляет именно переданного авторизованного
        supplier-пользователя, а не какой-то посторонний аккаунт по email."""
        venue_user = User.objects.create(
            email="dual-invite@example.com",
            full_name="Venue Person",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
        )
        supplier_account = User.objects.create(
            email="dual-invite@example.com",
            full_name="Supplier Person",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        invite = self._invite(email="dual-invite@example.com")
        result = self._accept(invite.token, user=supplier_account)

        self.assertEqual(result["user_id"], supplier_account.id)
        self.assertNotEqual(supplier_account.id, venue_user.id)
        self.assertTrue(
            SupplierUser.objects.filter(
                supplier=self.supplier, user=supplier_account, status=EntityStatus.ACTIVE
            ).exists()
        )
        # venue-аккаунт остаётся посторонним для этой компании
        self.assertFalse(SupplierUser.objects.filter(supplier=self.supplier, user=venue_user).exists())

    def test_preview_account_exists_only_checks_supplier_role(self):
        """account_exists в превью должен смотреть только на supplier-роль —
        venue_user-аккаунт с тем же email тут ни при чём (2026-08-06)."""
        User.objects.create(
            email="dual-invite-2@example.com",
            full_name="Venue Person",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
        )
        preview = self._preview(self._invite(email="dual-invite-2@example.com").token)
        self.assertFalse(preview["account_exists"])  # только venue_user существует, не supplier


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierUserListAndRevokeHTTPTests(APITestCase):
    """2026-08-01: "кто имеет доступ" + "отозвать доступ" — аналог диалога
    "Поделиться доступом" в Google Docs, найденный как отсутствующий
    функционал при аудите owner/manager-прав."""

    def setUp(self):
        self.supplier = _make_supplier()
        self.owner = User.objects.create(email="team_owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.manager = User.objects.create(
            email="team_manager@example.com", full_name="Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

    def list_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/users"

    def revoke_url(self, user_id):
        return f"/api/v1/suppliers/{self.supplier.id}/users/{user_id}/revoke"

    def test_list_requires_auth(self):
        response = self.client.get(self.list_url())
        self.assertEqual(response.status_code, 401)

    def test_manager_can_list_team_too(self):
        """2026-08-05: owner и manager имеют одинаковые права по решению пользователя."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.manager))
        response = self.client.get(self.list_url())
        self.assertEqual(response.status_code, 200)

    def test_owner_can_list_team(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get(self.list_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        emails = {row["email"] for row in response.data}
        self.assertEqual(emails, {"team_owner@example.com", "team_manager@example.com"})

    def test_service_token_can_list_team(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.get(self.list_url())
        self.assertEqual(response.status_code, 200)

    def test_owner_can_revoke_manager(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.post(self.revoke_url(self.manager.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            SupplierUser.objects.get(supplier=self.supplier, user=self.manager).status, EntityStatus.INACTIVE
        )

    def test_revoked_manager_disappears_from_team_list(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.post(self.revoke_url(self.manager.id))
        response = self.client.get(self.list_url())
        self.assertEqual(len(response.data), 1)

    def test_manager_can_revoke_another_member(self):
        """2026-08-05: owner и manager имеют одинаковые права. Отзывает доступ у
        второго manager (не owner), чтобы не задевать инвариант "последний owner"."""
        second_manager = User.objects.create(
            email="team_manager2@example.com", full_name="Manager 2", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=second_manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.manager))
        response = self.client.post(self.revoke_url(second_manager.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            SupplierUser.objects.get(supplier=self.supplier, user=second_manager).status, EntityStatus.INACTIVE
        )

    def test_owner_cannot_revoke_self(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.post(self.revoke_url(self.owner.id))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SupplierUser.objects.get(supplier=self.supplier, user=self.owner).status, EntityStatus.ACTIVE)

    def test_cannot_revoke_last_owner_via_staff(self):
        """Даже staff/service-token не может оставить компанию без owner'а —
        это защита данных, не про то, кто вызывает."""
        staff = _make_admin_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(staff))
        response = self.client.post(self.revoke_url(self.owner.id))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SupplierUser.objects.get(supplier=self.supplier, user=self.owner).status, EntityStatus.ACTIVE)

    def test_can_revoke_one_of_two_owners(self):
        second_owner = User.objects.create(email="team_owner2@example.com", full_name="Owner 2", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=second_owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.post(self.revoke_url(second_owner.id))
        self.assertEqual(response.status_code, 200)

    def test_revoke_already_inactive_member_returns_404(self):
        SupplierUser.objects.filter(supplier=self.supplier, user=self.manager).update(status=EntityStatus.INACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.post(self.revoke_url(self.manager.id))
        self.assertEqual(response.status_code, 404)


class SupplierUserRoleChangeHTTPTests(APITestCase):
    """2026-08-03: пробел из Figma-гайда — invite/join-approve всегда
    создают role=manager, сменить роль после было нечем."""

    def setUp(self):
        self.supplier = _make_supplier()
        self.owner = User.objects.create(email="role_owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.manager = User.objects.create(
            email="role_manager@example.com", full_name="Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

    def url(self, user_id):
        return f"/api/v1/suppliers/{self.supplier.id}/users/{user_id}"

    def test_requires_auth(self):
        response = self.client.patch(self.url(self.manager.id), {"role": "owner"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_manager_cannot_grant_owner_role(self):
        """2026-08-06 (находка ревью): раньше (2026-08-05, owner/manager паритет)
        manager мог назначить кого угодно owner'ом наравне с owner. Теперь
        назначение role=owner деактивирует предыдущего owner'а полностью — если бы
        manager мог это делать, он мог бы одним запросом выселить настоящего
        владельца из его собственной компании. Назначать owner'а может только
        текущий owner/staff/сервис-токен — 403 иначе."""
        second_manager = User.objects.create(
            email="role_manager2@example.com", full_name="Manager 2", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=second_manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.manager))
        response = self.client.patch(self.url(second_manager.id), {"role": "owner"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            SupplierUser.objects.get(supplier=self.supplier, user=second_manager).role, SupplierUserRole.MANAGER
        )

    def test_manager_can_change_role_between_non_owner_targets(self):
        """2026-08-05: owner и manager имеют одинаковые права — сохранено для всего,
        кроме назначения owner'а (см. test_manager_cannot_grant_owner_role).
        Демонстрирует это на смене роли owner->manager второго owner'а, которая не
        требует владельческих прав вызывающего."""
        second_owner = User.objects.create(email="role_owner3@example.com", full_name="Owner 3", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=second_owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.manager))
        response = self.client.patch(self.url(second_owner.id), {"role": "manager"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], SupplierUserRole.MANAGER)

    def test_staff_can_grant_owner_role(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        with override_settings(SERVICE_API_TOKEN="test-token"):
            response = self.client.patch(self.url(self.manager.id), {"role": "owner"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], SupplierUserRole.OWNER)

    def test_owner_can_promote_manager_to_owner(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.patch(self.url(self.manager.id), {"role": "owner"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], SupplierUserRole.OWNER)
        self.assertEqual(
            SupplierUser.objects.get(supplier=self.supplier, user=self.manager).role, SupplierUserRole.OWNER
        )

    def test_cannot_demote_last_owner(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.patch(self.url(self.owner.id), {"role": "manager"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SupplierUser.objects.get(supplier=self.supplier, user=self.owner).role, SupplierUserRole.OWNER)

    def test_can_demote_one_of_two_owners(self):
        second_owner = User.objects.create(email="role_owner2@example.com", full_name="Owner 2", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=second_owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.patch(self.url(second_owner.id), {"role": "manager"}, format="json")
        self.assertEqual(response.status_code, 200)

    def test_rejects_invalid_role(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.patch(self.url(self.manager.id), {"role": "superadmin"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_promoting_new_owner_demotes_previous_owner_to_manager(self):
        """2026-08-07 (исправлено по явному указанию пользователя): владелец
        компании только один, но при передаче прав старый owner понижается
        до manager, сохраняя доступ к компании (не теряет его целиком)."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.patch(self.url(self.manager.id), {"role": "owner"}, format="json")
        self.assertEqual(response.status_code, 200)

        previous_owner_su = SupplierUser.objects.get(supplier=self.supplier, user=self.owner)
        self.assertEqual(previous_owner_su.role, SupplierUserRole.MANAGER)
        self.assertEqual(previous_owner_su.status, EntityStatus.ACTIVE)

        new_owner_su = SupplierUser.objects.get(supplier=self.supplier, user=self.manager)
        self.assertEqual(new_owner_su.role, SupplierUserRole.OWNER)
        self.assertEqual(new_owner_su.status, EntityStatus.ACTIVE)

    def test_inactive_member_returns_404(self):
        SupplierUser.objects.filter(supplier=self.supplier, user=self.manager).update(status=EntityStatus.INACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.patch(self.url(self.manager.id), {"role": "owner"}, format="json")
        self.assertEqual(response.status_code, 404)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierUserInvitePendingListHTTPTests(APITestCase):
    """2026-08-03: пробел из Figma-гайда — не было ручки для списка
    неподтверждённых email-приглашений (отдельно от join-requests/pending)."""

    def setUp(self):
        self.supplier = _make_supplier()
        self.owner = User.objects.create(email="pending_owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.manager = User.objects.create(
            email="pending_manager@example.com", full_name="Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

    def invite_create_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/invites"

    def pending_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/invites/pending"

    def test_requires_auth(self):
        response = self.client.get(self.pending_url())
        self.assertEqual(response.status_code, 401)

    def test_manager_can_list_pending_invites_too(self):
        """2026-08-05: owner и manager имеют одинаковые права."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.manager))
        response = self.client.get(self.pending_url())
        self.assertEqual(response.status_code, 200)

    def test_empty_when_no_invites(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get(self.pending_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_owner_sees_pending_invite(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        create_response = self.client.post(
            self.invite_create_url(), {"target_email": "pending-invitee@example.com"}, format="json"
        )
        self.assertEqual(create_response.status_code, 201)

        response = self.client.get(self.pending_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["target_email"], "pending-invitee@example.com")
        self.assertEqual(response.data[0]["invited_by_email"], "pending_owner@example.com")

    def test_accepted_invite_disappears_from_pending_list(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        create_response = self.client.post(
            self.invite_create_url(), {"target_email": "accepted-invitee@example.com"}, format="json"
        )
        token = SupplierUserInvite.objects.get(id=create_response.data["id"]).token

        invitee = User.objects.create(
            email="accepted-invitee@example.com",
            full_name="Invitee",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(invitee))
        self.client.post(f"/api/v1/suppliers/invites/{token}/accept", {}, format="json")

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get(self.pending_url())
        self.assertEqual(response.data, [])


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierUserInviteHTTPTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/invites"

    def accept_url(self, token):
        return f"/api/v1/suppliers/invites/{token}/accept"

    def preview_url(self, token):
        return f"/api/v1/suppliers/invites/{token}"

    def test_preview_does_not_require_auth(self):
        self.authenticate()
        create_response = self.client.post(
            self.create_url(), {"target_email": "preview-http@example.com"}, format="json"
        )
        self.client.credentials()

        token = SupplierUserInvite.objects.get(id=create_response.data["id"]).token
        response = self.client.get(self.preview_url(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["target_email"], "preview-http@example.com")
        self.assertFalse(response.data["is_expired"])
        self.assertFalse(response.data["account_exists"])

    def test_create_defaults_role_to_manager(self):
        self.authenticate()
        response = self.client.post(self.create_url(), {"target_email": "default-role-http@example.com"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["role"], SupplierUserRole.MANAGER)

    def test_create_accepts_owner_role(self):
        self.authenticate()
        response = self.client.post(
            self.create_url(), {"target_email": "owner-role-http@example.com", "role": "owner"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["role"], SupplierUserRole.OWNER)
        self.assertEqual(
            SupplierUserInvite.objects.get(id=response.data["id"]).role,
            SupplierUserRole.OWNER,
        )

    def test_manager_cannot_create_owner_invite(self):
        """2026-08-06 (находка ревью): accept такого приглашения деактивирует
        текущего owner'а — если бы manager мог его создать, он мог бы одним
        письмом сообщнику выселить настоящего владельца."""
        owner = User.objects.create(email="invite_owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        manager = User.objects.create(email="invite_manager@example.com", full_name="Manager", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(manager))
        response = self.client.post(
            self.create_url(), {"target_email": "rogue-owner@example.com", "role": "owner"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SupplierUserInvite.objects.filter(target_email="rogue-owner@example.com").exists())

    def test_owner_can_create_owner_invite(self):
        owner = User.objects.create(email="invite_owner2@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.post(
            self.create_url(), {"target_email": "legit-owner@example.com", "role": "owner"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["role"], SupplierUserRole.OWNER)

    def test_preview_account_exists_true_for_registered_email(self):
        self.authenticate()
        create_response = self.client.post(
            self.create_url(), {"target_email": "preview-existing-http@example.com"}, format="json"
        )
        self.client.credentials()
        User.objects.create(email="preview-existing-http@example.com", full_name="Existing", role=UserRole.SUPPLIER)

        token = SupplierUserInvite.objects.get(id=create_response.data["id"]).token
        response = self.client.get(self.preview_url(token))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["account_exists"])

    def test_preview_unknown_token_returns_404(self):
        response = self.client.get(self.preview_url("does-not-exist"))
        self.assertEqual(response.status_code, 404)

    def test_create_requires_auth(self):
        response = self.client.post(self.create_url(), {"target_email": "x@example.com"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_create_with_service_token(self):
        self.authenticate()
        response = self.client.post(self.create_url(), {"target_email": "new@example.com"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["target_email"], "new@example.com")

    def test_owner_can_create_without_service_token(self):
        owner = User.objects.create(email="owner2@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.post(self.create_url(), {"target_email": "colleague@example.com"}, format="json")
        self.assertEqual(response.status_code, 201)

    def test_manager_can_create_invite_too(self):
        """2026-08-01: изначально было owner-only (по запросу пользователя,
        проверка что owner и manager ведут себя по-разному). 2026-08-05: пользователь
        явно решил, что у компаний только два уровня и права у них одинаковые — manager
        теперь тоже может приглашать."""
        manager = User.objects.create(email="manager-invite@example.com", full_name="Manager", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(manager))
        response = self.client.post(self.create_url(), {"target_email": "colleague@example.com"}, format="json")
        self.assertEqual(response.status_code, 201)

    def test_accept_requires_auth(self):
        """2026-08-07 (по явному решению пользователя): никаких email/паролей
        в теле — ручка требует core.User JWT. У юзера без аккаунта путь —
        сначала обычная регистрация, потом переход по ссылке залогиненным."""
        self.authenticate()
        create_response = self.client.post(self.create_url(), {"target_email": "claimer2@example.com"}, format="json")
        self.client.credentials()

        token = SupplierUserInvite.objects.get(id=create_response.data["id"]).token
        response = self.client.post(self.accept_url(token), {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_accept_requires_supplier_role(self):
        """venue_user-JWT не годится, даже если email совпадает с target_email
        — это отдельный аккаунт (email уникален по (email, role), не глобально)."""
        self.authenticate()
        create_response = self.client.post(
            self.create_url(), {"target_email": "venue-session-accept@example.com"}, format="json"
        )
        token = SupplierUserInvite.objects.get(id=create_response.data["id"]).token

        venue_user = User.objects.create(
            email="venue-session-accept@example.com", full_name="Venue Person", role=UserRole.VENUE_USER
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(venue_user))
        response = self.client.post(self.accept_url(token), {}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SupplierUser.objects.filter(user=venue_user).exists())

    def test_accept_attaches_matching_authenticated_supplier(self):
        self.authenticate()
        create_response = self.client.post(
            self.create_url(), {"target_email": "already-in-http@example.com"}, format="json"
        )
        token = SupplierUserInvite.objects.get(id=create_response.data["id"]).token

        existing = User.objects.create(
            email="already-in-http@example.com", full_name="Existing", role=UserRole.SUPPLIER
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(existing))
        response = self.client.post(self.accept_url(token), {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user_id"], existing.id)
        self.assertTrue(
            SupplierUser.objects.filter(supplier=self.supplier, user=existing, status=EntityStatus.ACTIVE).exists()
        )

    def test_accept_authenticated_different_email_returns_400(self):
        self.authenticate()
        create_response = self.client.post(
            self.create_url(), {"target_email": "intended-http@example.com"}, format="json"
        )
        token = SupplierUserInvite.objects.get(id=create_response.data["id"]).token

        someone_else = User.objects.create(
            email="someone-else-http@example.com", full_name="Someone Else", role=UserRole.SUPPLIER
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(someone_else))
        response = self.client.post(self.accept_url(token), {}, format="json")
        self.assertEqual(response.status_code, 400)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierInviteLinkHTTPTests(APITestCase):
    """2026-08-04: "Пригласите по ссылке" — референс от Вити (Bitrix24),
    многоразовая альтернатива email-инвайту (SupplierUserInvite)."""

    def setUp(self):
        self.supplier = _make_supplier()
        self.owner = User.objects.create(email="il_owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.manager = User.objects.create(email="il_manager@example.com", full_name="Manager", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def link_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/invite-link"

    def regenerate_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/invite-link/regenerate"

    def join_url(self, token):
        return f"/api/v1/suppliers/invite-link/{token}/join"

    def preview_url(self, token):
        return f"/api/v1/suppliers/invite-link/{token}"

    def test_get_requires_auth(self):
        response = self.client.get(self.link_url())
        self.assertEqual(response.status_code, 401)

    def test_manager_can_get_link_too(self):
        """2026-08-05: owner и manager имеют одинаковые права."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.manager))
        response = self.client.get(self.link_url())
        self.assertEqual(response.status_code, 200)

    def test_owner_get_creates_link_on_first_call(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get(self.link_url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["token"])
        self.assertIn(response.data["token"], response.data["url"])
        self.assertEqual(response.data["role"], SupplierUserRole.MANAGER)
        self.assertEqual(response.data["status"], EntityStatus.ACTIVE)
        self.assertIsNone(response.data["expires_at"])

    def test_get_is_idempotent_same_token(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        first = self.client.get(self.link_url())
        second = self.client.get(self.link_url())
        self.assertEqual(first.data["token"], second.data["token"])

    def test_owner_can_update_settings(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.get(self.link_url())
        response = self.client.patch(self.link_url(), {"role": "owner", "status": "inactive"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], SupplierUserRole.OWNER)
        self.assertEqual(response.data["status"], EntityStatus.INACTIVE)

    def test_manager_can_update_non_owner_settings_too(self):
        """2026-08-05: owner и manager имеют одинаковые права — кроме назначения
        role=owner (2026-08-06, находка ревью, см. test_manager_cannot_set_owner_role)."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.get(self.link_url())
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.manager))
        response = self.client.patch(self.link_url(), {"status": "inactive"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], EntityStatus.INACTIVE)

    def test_manager_cannot_set_owner_role(self):
        """2026-08-06 (находка ревью): присоединение по ссылке с role=owner
        деактивирует текущего owner'а — если бы manager мог это настроить, он мог
        бы подготовить ссылку, которая при первом же join выселяет владельца."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.client.get(self.link_url())
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.manager))
        response = self.client.patch(self.link_url(), {"role": "owner"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SupplierInviteLink.objects.get(supplier=self.supplier).role, SupplierUserRole.MANAGER)

    def test_staff_can_set_owner_role(self):
        self.authenticate()
        self.client.get(self.link_url())
        response = self.client.patch(self.link_url(), {"role": "owner"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], SupplierUserRole.OWNER)

    def test_regenerate_changes_token_keeps_role(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        before = self.client.get(self.link_url()).data["token"]
        self.client.patch(self.link_url(), {"role": "owner"}, format="json")

        response = self.client.post(self.regenerate_url())

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.data["token"], before)
        self.assertEqual(response.data["role"], SupplierUserRole.OWNER)

    def test_join_requires_auth(self):
        """2026-08-07 (по явному решению пользователя): ссылка ни к какому
        email не привязана, никаких email/паролей в теле — только JWT.
        У юзера без аккаунта путь — сначала обычная регистрация, потом
        переход по ссылке залогиненным."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.credentials()

        response = self.client.post(self.join_url(token), {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_join_requires_supplier_role(self):
        venue_user = User.objects.create(
            email="venue-session-join@example.com", full_name="Venue Person", role=UserRole.VENUE_USER
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(venue_user))

        response = self.client.post(self.join_url(token), {}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SupplierUser.objects.filter(supplier=self.supplier, user=venue_user).exists())

    def test_join_attaches_authenticated_supplier_to_company(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]

        newbie = User.objects.create(
            email="linked-newbie@example.com", full_name="Newbie", role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(newbie))
        response = self.client.post(self.join_url(token), {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], SupplierUserRole.MANAGER)
        self.assertEqual(response.data["user_id"], newbie.id)
        membership = SupplierUser.objects.get(supplier=self.supplier, user=newbie)
        self.assertEqual(membership.role, SupplierUserRole.MANAGER)
        self.assertEqual(membership.status, EntityStatus.ACTIVE)

    def test_join_own_manager_link_blocked_when_joiner_is_last_owner(self):
        """2026-08-07, реальный баг от Вити: "если юзер сам себе ссылку
        менеджера отправит под ролью владельца, в компании не будет
        владельца — он будет только менеджером". Ссылка по умолчанию
        role=manager (см. test_owner_get_creates_link_on_first_call) — если
        owner открывает/принимает свою же ссылку, update_or_create молча
        перезаписывал его собственное membership на manager, оставляя
        компанию без owner'а. Подтверждено эмпирически на реальном Postgres
        до фикса."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]

        response = self.client.post(self.join_url(token), {}, format="json")
        self.assertEqual(response.status_code, 400)

        membership = SupplierUser.objects.get(supplier=self.supplier, user=self.owner)
        self.assertEqual(membership.role, SupplierUserRole.OWNER)
        self.assertEqual(membership.status, EntityStatus.ACTIVE)

    def test_join_attaches_correct_supplier_account_when_venue_user_shares_email(self):
        """2026-08-06: та же гарантия, что у email-инвайта — venue_user с тем
        же email не мешает join прикрепить именно supplier-аккаунт."""
        venue_user = User.objects.create(
            email="dual-link-join@example.com",
            full_name="Venue Person",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
        )
        supplier_account = User.objects.create(
            email="dual-link-join@example.com",
            full_name="Supplier Person",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(supplier_account))

        response = self.client.post(self.join_url(token), {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user_id"], supplier_account.id)
        self.assertNotEqual(supplier_account.id, venue_user.id)
        self.assertTrue(SupplierUser.objects.filter(supplier=self.supplier, user=supplier_account).exists())
        self.assertFalse(SupplierUser.objects.filter(supplier=self.supplier, user=venue_user).exists())

    def test_join_with_owner_role_link_demotes_previous_owner_to_manager(self):
        """2026-08-07 (исправлено по явному указанию пользователя): владелец
        компании только один, но при передаче прав старый owner понижается
        до manager, сохраняя доступ — та же логика, что у email-инвайта,
        применяется и к присоединению по ссылке."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.patch(self.link_url(), {"role": "owner"}, format="json")

        new_owner = User.objects.create(
            email="new-owner-via-link@example.com",
            full_name="New Owner",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(new_owner))
        response = self.client.post(self.join_url(token), {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], SupplierUserRole.OWNER)

        previous_owner_su = SupplierUser.objects.get(supplier=self.supplier, user=self.owner)
        self.assertEqual(previous_owner_su.role, SupplierUserRole.MANAGER)
        self.assertEqual(previous_owner_su.status, EntityStatus.ACTIVE)

        new_owner_su = SupplierUser.objects.get(supplier=self.supplier, user=new_owner)
        self.assertEqual(new_owner_su.role, SupplierUserRole.OWNER)
        self.assertEqual(new_owner_su.status, EntityStatus.ACTIVE)

    def test_link_is_reusable_by_multiple_people(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]

        first = User.objects.create(email="multi-1@example.com", full_name="Multi 1", role=UserRole.SUPPLIER)
        second = User.objects.create(email="multi-2@example.com", full_name="Multi 2", role=UserRole.SUPPLIER)

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(first))
        self.client.post(self.join_url(token), {}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(second))
        response = self.client.post(self.join_url(token), {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SupplierUser.objects.filter(supplier=self.supplier).count(), 4)  # owner+manager+2 joined

    def test_join_rejects_inactive_link(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.patch(self.link_url(), {"status": "inactive"}, format="json")

        blocked = User.objects.create(email="blocked@example.com", full_name="Blocked", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(blocked))
        response = self.client.post(self.join_url(token), {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_join_rejects_expired_link(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.patch(
            self.link_url(), {"expires_at": (timezone.now() - timezone.timedelta(hours=1)).isoformat()}, format="json"
        )

        too_late = User.objects.create(email="toolate@example.com", full_name="Too Late", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(too_late))
        response = self.client.post(self.join_url(token), {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_join_unknown_token_404(self):
        ghost = User.objects.create(email="ghost@example.com", full_name="Ghost", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(ghost))
        response = self.client.post(self.join_url("not-a-real-token"), {}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_preview_does_not_require_auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.credentials()

        response = self.client.get(self.preview_url(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["supplier_name"], self.supplier.display_name)
        self.assertIsNone(response.data["supplier_inn"])
        self.assertEqual(response.data["role"], SupplierUserRole.MANAGER)
        self.assertEqual(response.data["status"], EntityStatus.ACTIVE)
        self.assertFalse(response.data["is_expired"])

    def test_preview_includes_supplier_inn(self):
        self.supplier.inn = "7707083893"
        self.supplier.save(update_fields=["inn"])
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.credentials()

        response = self.client.get(self.preview_url(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["supplier_inn"], "7707083893")

    def test_preview_reflects_role_setting(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.patch(self.link_url(), {"role": "owner"}, format="json")
        self.client.credentials()

        response = self.client.get(self.preview_url(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], SupplierUserRole.OWNER)

    def test_preview_reflects_inactive_status_without_mutating(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.patch(self.link_url(), {"status": "inactive"}, format="json")
        self.client.credentials()

        response = self.client.get(self.preview_url(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], EntityStatus.INACTIVE)
        self.assertEqual(SupplierInviteLink.objects.get(supplier=self.supplier).status, EntityStatus.INACTIVE)

    def test_preview_is_expired_true_but_status_untouched(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        token = self.client.get(self.link_url()).data["token"]
        self.client.patch(
            self.link_url(), {"expires_at": (timezone.now() - timezone.timedelta(hours=1)).isoformat()}, format="json"
        )
        self.client.credentials()

        response = self.client.get(self.preview_url(token))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["is_expired"])
        self.assertEqual(response.data["status"], EntityStatus.ACTIVE)  # separate signal, untouched

    def test_preview_unknown_token_returns_404(self):
        response = self.client.get(self.preview_url("not-a-real-token"))
        self.assertEqual(response.status_code, 404)


class SupplierSearchByInnHTTPTests(APITestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            legal_name="Findable LLC", display_name="Findable Co", inn="7707083893", status=EntityStatus.ACTIVE
        )
        self.user = User.objects.create(email="searcher@example.com", full_name="Searcher", role=UserRole.SUPPLIER)

    def url(self, inn):
        return f"/api/v1/suppliers/search-by-inn?inn={inn}"

    def test_requires_auth(self):
        response = self.client.get(self.url("7707083893"))
        self.assertEqual(response.status_code, 401)

    def test_finds_active_supplier_by_inn(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.get(self.url("7707083893"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], str(self.supplier.id))
        self.assertEqual(response.data["display_name"], "Findable Co")

    def test_404_when_inn_not_found(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.get(self.url("500100732259"))  # valid checksum, no matching supplier
        self.assertEqual(response.status_code, 404)

    def test_400_when_inn_invalid(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.get(self.url("1234567890"))  # fails checksum
        self.assertEqual(response.status_code, 400)

    def test_ignores_inactive_supplier(self):
        self.supplier.status = EntityStatus.INACTIVE
        self.supplier.save(update_fields=["status"])
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.get(self.url("7707083893"))
        self.assertEqual(response.status_code, 404)


class SupplierJoinRequestServiceTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.owner = User.objects.create(email="join_owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.requester = User.objects.create(
            email="join_requester@example.com", full_name="Requester", role=UserRole.SUPPLIER
        )

    def _request_join(self, message=None):
        from api.v1.suppliers.services import create_supplier_join_request

        return create_supplier_join_request(
            supplier_id=self.supplier.id, requested_by_user_id=self.requester.id, message=message
        )

    def test_creates_pending_request(self):
        join_request = self._request_join(message="хочу присоединиться")
        self.assertEqual(join_request.status, SupplierJoinRequestStatus.PENDING)
        self.assertEqual(join_request.message, "хочу присоединиться")

    def test_notifies_fixed_support_email_not_company_members(self):
        from django.conf import settings
        from django.core import mail

        self._request_join()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [settings.SUPPLIER_JOIN_REQUEST_NOTIFICATION_EMAIL])
        self.assertNotIn("join_owner@example.com", mail.outbox[0].to)

    def test_notifies_support_email_even_when_no_active_owner(self):
        from django.conf import settings
        from django.core import mail

        SupplierUser.objects.filter(supplier=self.supplier, user=self.owner).update(status=EntityStatus.INACTIVE)
        self._request_join()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [settings.SUPPLIER_JOIN_REQUEST_NOTIFICATION_EMAIL])

    def test_rejects_duplicate_pending_request(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        self._request_join()
        with self.assertRaises(DRFValidationError):
            self._request_join()

    def test_rejects_request_from_existing_active_member(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        SupplierUser.objects.create(
            supplier=self.supplier, user=self.requester, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        with self.assertRaises(DRFValidationError):
            self._request_join()

    def test_rejects_for_inactive_supplier(self):
        from django.http import Http404

        self.supplier.status = EntityStatus.INACTIVE
        self.supplier.save(update_fields=["status"])
        with self.assertRaises(Http404):
            self._request_join()

    def test_approve_adds_manager_without_touching_owner(self):
        from api.v1.suppliers.services import decide_supplier_join_request

        join_request = self._request_join()
        decide_supplier_join_request(
            supplier_id=self.supplier.id,
            request_id=join_request.id,
            decided_by_user_id=self.owner.id,
            approve=True,
        )
        join_request.refresh_from_db()
        self.assertEqual(join_request.status, SupplierJoinRequestStatus.APPROVED)
        self.assertEqual(join_request.decided_by_id, self.owner.id)

        owner_su = SupplierUser.objects.get(supplier=self.supplier, user=self.owner)
        self.assertEqual(owner_su.status, EntityStatus.ACTIVE)  # untouched

        new_member = SupplierUser.objects.get(supplier=self.supplier, user=self.requester)
        self.assertEqual(new_member.role, SupplierUserRole.MANAGER)
        self.assertEqual(new_member.status, EntityStatus.ACTIVE)

    def test_reject_does_not_create_membership(self):
        from api.v1.suppliers.services import decide_supplier_join_request

        join_request = self._request_join()
        decide_supplier_join_request(
            supplier_id=self.supplier.id,
            request_id=join_request.id,
            decided_by_user_id=self.owner.id,
            approve=False,
        )
        join_request.refresh_from_db()
        self.assertEqual(join_request.status, SupplierJoinRequestStatus.REJECTED)
        self.assertFalse(SupplierUser.objects.filter(supplier=self.supplier, user=self.requester).exists())

    def test_deciding_twice_raises_validation_error(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from api.v1.suppliers.services import decide_supplier_join_request

        join_request = self._request_join()
        decide_supplier_join_request(
            supplier_id=self.supplier.id, request_id=join_request.id, decided_by_user_id=self.owner.id, approve=True
        )
        with self.assertRaises(DRFValidationError):
            decide_supplier_join_request(
                supplier_id=self.supplier.id,
                request_id=join_request.id,
                decided_by_user_id=self.owner.id,
                approve=False,
            )

    def test_list_pending_only_returns_pending(self):
        from api.v1.suppliers.services import decide_supplier_join_request, list_supplier_join_requests

        decided = self._request_join()
        decide_supplier_join_request(
            supplier_id=self.supplier.id, request_id=decided.id, decided_by_user_id=self.owner.id, approve=True
        )
        other_requester = User.objects.create(email="join_other@example.com", full_name="Other", role=UserRole.SUPPLIER)
        from api.v1.suppliers.services import create_supplier_join_request

        pending = create_supplier_join_request(supplier_id=self.supplier.id, requested_by_user_id=other_requester.id)

        pending_ids = [r.id for r in list_supplier_join_requests(supplier_id=self.supplier.id)]
        self.assertEqual(pending_ids, [pending.id])

    def test_list_mine_returns_all_statuses_for_user(self):
        from api.v1.suppliers.services import list_my_supplier_join_requests

        join_request = self._request_join()
        mine = list(list_my_supplier_join_requests(user_id=self.requester.id))
        self.assertEqual([r.id for r in mine], [join_request.id])


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierJoinRequestHTTPTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.owner = User.objects.create(email="join_http_owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.requester = User.objects.create(
            email="join_http_requester@example.com", full_name="Requester", role=UserRole.SUPPLIER
        )

    def create_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/join-requests"

    def pending_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/join-requests/pending"

    def approve_url(self, request_id):
        return f"/api/v1/suppliers/{self.supplier.id}/join-requests/{request_id}/approve"

    def reject_url(self, request_id):
        return f"/api/v1/suppliers/{self.supplier.id}/join-requests/{request_id}/reject"

    def mine_url(self):
        return "/api/v1/suppliers/join-requests/mine"

    def test_create_requires_auth(self):
        response = self.client.post(self.create_url(), {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_any_authenticated_user_can_create(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        response = self.client.post(self.create_url(), {"message": "let me in"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], SupplierJoinRequestStatus.PENDING)
        self.assertEqual(response.data["requested_by_id"], str(self.requester.id))
        self.assertIsNone(response.data["phone"])
        self.assertIsNone(response.data["email"])
        self.assertEqual(response.data["documents"], [])

    def test_create_accepts_description_phone_email(self):
        """2026-07-29: "запрос доступа к существующей компании, там указывается
        описание ситуации, контактный телефон, электронная почта и подтверждающие
        документы" — JSON-путь (без документов), обратная совместимость сохранена."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        response = self.client.post(
            self.create_url(),
            {
                "message": "Я менеджер по продажам этой компании, нужен доступ к прайсам",
                "phone": "+7 999 123-45-67",
                "email": "requester-work@example.com",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["phone"], "+7 999 123-45-67")
        self.assertEqual(response.data["email"], "requester-work@example.com")

    def test_pending_list_requires_owner_not_just_any_user(self):
        SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)
        outsider = User.objects.create(email="outsider@example.com", full_name="Outsider", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(outsider))
        response = self.client.get(self.pending_url())
        self.assertEqual(response.status_code, 403)

    def test_owner_can_list_pending(self):
        SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get(self.pending_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["requested_by_id"], str(self.requester.id))

    def test_owner_can_approve(self):
        join_request = SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.post(self.approve_url(join_request.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], SupplierJoinRequestStatus.APPROVED)
        self.assertTrue(
            SupplierUser.objects.filter(
                supplier=self.supplier, user=self.requester, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
            ).exists()
        )

    def test_owner_of_other_supplier_cannot_decide_request_for_this_one(self):
        other_supplier = _make_supplier(name="Other Co")
        other_owner = User.objects.create(
            email="other_supplier_owner@example.com", full_name="Other Owner", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=other_supplier, user=other_owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        join_request = SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(other_owner))
        # other_owner legitimately owns *some* supplier, but not self.supplier — the URL's
        # supplier_id must match both the permission check AND the join request's own supplier.
        response = self.client.post(f"/api/v1/suppliers/{other_supplier.id}/join-requests/{join_request.id}/approve")
        self.assertEqual(response.status_code, 404)
        join_request.refresh_from_db()
        self.assertEqual(join_request.status, SupplierJoinRequestStatus.PENDING)

    def test_non_owner_cannot_approve(self):
        join_request = SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        response = self.client.post(self.approve_url(join_request.id))
        self.assertEqual(response.status_code, 403)

    def test_manager_of_this_supplier_can_list_pending_too(self):
        """2026-08-01: изначально было owner-only. 2026-08-05: пользователь явно
        решил, что owner и manager имеют одинаковые права — manager этой же
        компании теперь тоже видит заявки на доступ."""
        manager = User.objects.create(
            email="manager-join-list@example.com", full_name="Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(manager))
        response = self.client.get(self.pending_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_manager_of_this_supplier_can_approve_too(self):
        """2026-08-05: owner и manager имеют одинаковые права."""
        manager = User.objects.create(
            email="manager-join-approve@example.com", full_name="Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        join_request = SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(manager))
        response = self.client.post(self.approve_url(join_request.id))
        self.assertEqual(response.status_code, 200)
        join_request.refresh_from_db()
        self.assertEqual(join_request.status, SupplierJoinRequestStatus.APPROVED)

    def test_owner_can_reject(self):
        join_request = SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.post(self.reject_url(join_request.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], SupplierJoinRequestStatus.REJECTED)
        self.assertFalse(SupplierUser.objects.filter(supplier=self.supplier, user=self.requester).exists())

    def test_service_token_can_approve(self):
        join_request = SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.post(self.approve_url(join_request.id))
        self.assertEqual(response.status_code, 200)

    def test_mine_requires_auth(self):
        response = self.client.get(self.mine_url())
        self.assertEqual(response.status_code, 401)

    def test_mine_lists_only_own_requests(self):
        SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=self.requester)
        other_requester = User.objects.create(
            email="join_other_http@example.com", full_name="Other", role=UserRole.SUPPLIER
        )
        SupplierJoinRequest.objects.create(supplier=self.supplier, requested_by=other_requester)

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        response = self.client.get(self.mine_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["requested_by_id"], str(self.requester.id))


class SupplierJoinRequestDocumentUploadTests(TemporaryMediaRootMixin, APITestCase):
    """2026-07-29: "запрос доступа к существующей компании... и подтверждающиеся
    документы" — multipart-загрузка файлов вместе с созданием заявки."""

    def setUp(self):
        self.supplier = _make_supplier()
        self.owner = User.objects.create(email="join_doc_owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.requester = User.objects.create(
            email="join_doc_requester@example.com", full_name="Requester", role=UserRole.SUPPLIER
        )

    def create_url(self):
        return f"/api/v1/suppliers/{self.supplier.id}/join-requests"

    def test_uploads_documents_and_persists_urls(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        doc1 = SimpleUploadedFile("power_of_attorney.pdf", b"%PDF-fake", content_type="application/pdf")
        doc2 = SimpleUploadedFile("passport_scan.jpg", b"fake-jpeg-bytes", content_type="image/jpeg")
        response = self.client.post(
            self.create_url(),
            {"message": "Подтверждаю полномочия", "documents": [doc1, doc2]},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["documents"]), 2)
        for url in response.data["documents"]:
            self.assertTrue(default_storage.exists(url.removeprefix(settings.MEDIA_URL)))

        join_request = SupplierJoinRequest.objects.get(pk=response.data["id"])
        self.assertEqual(len(join_request.documents), 2)

    def test_rejects_disallowed_document_type(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        bad_file = SimpleUploadedFile("doc.exe", b"binary", content_type="application/octet-stream")
        response = self.client.post(self.create_url(), {"documents": [bad_file]}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_rejects_too_many_documents(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        files = [SimpleUploadedFile(f"doc{i}.pdf", b"%PDF-fake", content_type="application/pdf") for i in range(6)]
        response = self.client.post(self.create_url(), {"documents": files}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_rejects_document_over_20mb(self):
        """2026-08-03: лимит поднят с 10 до 20 МБ по Figma-макету формы заявки
        на доступ ("PDF, JPG, PNG до 20 МБ")."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        big_file = SimpleUploadedFile("scan.pdf", b"x" * (20 * 1024 * 1024 + 1), content_type="application/pdf")
        response = self.client.post(self.create_url(), {"documents": [big_file]}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_accepts_document_just_under_20mb(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        ok_file = SimpleUploadedFile("scan.pdf", b"x" * (20 * 1024 * 1024 - 1), content_type="application/pdf")
        response = self.client.post(self.create_url(), {"documents": [ok_file]}, format="multipart")
        self.assertEqual(response.status_code, 201)

    def test_json_request_without_documents_still_works(self):
        """Обратная совместимость: JSON без файлов не ломается multipart-парсерами."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.requester))
        response = self.client.post(self.create_url(), {"message": "без документов"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["documents"], [])


class DadataRegistrationDateConversionTests(SimpleTestCase):
    def test_converts_dadata_ms_timestamp_to_iso_date(self):
        from api.v1.dadata_client import _registration_date_from_dadata

        # 2015-03-12T00:00:00Z в миллисекундах — реальный формат dadata state.registration_date.
        self.assertEqual(_registration_date_from_dadata({"registration_date": 1426118400000}), "2015-03-12")

    def test_returns_none_when_missing_or_falsy(self):
        from api.v1.dadata_client import _registration_date_from_dadata

        self.assertIsNone(_registration_date_from_dadata({}))
        self.assertIsNone(_registration_date_from_dadata({"registration_date": None}))

    def test_returns_none_on_garbage_value(self):
        from api.v1.dadata_client import _registration_date_from_dadata

        self.assertIsNone(_registration_date_from_dadata({"registration_date": "not-a-timestamp"}))


class DadataLookupByINNClientTests(SimpleTestCase):
    """2026-07-28: Витя — нет поля "Сокращённое наименование" в API. dadata отдаёт
    его как name.short_with_opf, но lookup_company_by_inn() раньше извлекала только
    name.full_with_opf. Мокаем urlopen напрямую (не через views), чтобы проверить
    именно эту функцию — не просто то, что сериализатор пробрасывает готовый dict."""

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_extracts_short_name_alongside_legal_name(self):
        import json
        from unittest.mock import patch

        from api.v1.dadata_client import lookup_company_by_inn

        payload = {
            "suggestions": [
                {
                    "data": {
                        "inn": "7707083893",
                        "kpp": "770701001",
                        "ogrn": "1027700132195",
                        "name": {
                            "full_with_opf": "Общество с ограниченной ответственностью «Ромашка»",
                            "short_with_opf": "ООО «Ромашка»",
                        },
                        "management": {"name": "Иванов И.И.", "post": "Директор"},
                        "address": {"unrestricted_value": "г. Москва"},
                        "state": {"status": "ACTIVE", "registration_date": 1426118400000},
                        "okved": "46.90",
                        "okpo": "12345678",
                    }
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            result = lookup_company_by_inn("7707083893")

        self.assertEqual(result["legal_name"], "Общество с ограниченной ответственностью «Ромашка»")
        self.assertEqual(result["short_name"], "ООО «Ромашка»")

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_splits_address_into_type_city_street_postal_code(self):
        """2026-08-01: фронту (Витя) нужны компоненты адреса раздельно, не одной
        строкой — для автозаполнения типизированной SupplierAddress формы."""
        import json
        from unittest.mock import patch

        from api.v1.dadata_client import lookup_company_by_inn

        payload = {
            "suggestions": [
                {
                    "data": {
                        "inn": "2465123456",
                        "name": {"full_with_opf": "ООО «Ромашка»"},
                        "address": {
                            "unrestricted_value": "660049, Красноярский край, г Красноярск, ул Новосибирская, стр 4, оф 32",
                            "data": {
                                "postal_code": "660049",
                                "region_with_type": "Красноярский край",
                                "area_with_type": "Емельяновский район",
                                "city_type": "г",
                                "city_type_full": "город",
                                "city": "Красноярск",
                                "street_with_type": "ул Новосибирская",
                                "house_type": "стр",
                                "house": "4",
                                "flat_type": "оф",
                                "flat": "32",
                            },
                        },
                        "state": {"status": "ACTIVE"},
                    }
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            result = lookup_company_by_inn("2465123456")

        self.assertEqual(result["address_type"], "legal")
        self.assertEqual(result["region"], "Красноярский край")
        self.assertEqual(result["district"], "Емельяновский район")
        self.assertEqual(result["city_type"], "город")
        self.assertEqual(result["city"], "Красноярск")
        self.assertEqual(result["street"], "ул Новосибирская, стр 4")
        self.assertEqual(result["apartment_office"], "оф 32")
        self.assertEqual(result["postal_code"], "660049")

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_district_falls_back_to_city_district_when_no_area(self):
        """2026-08-03: Витя — район не парсился вообще. dadata различает
        area (муниципальный/сельский район) и city_district (район внутри
        города, например у Москвы/Питера) — берём area, а если его нет,
        city_district."""
        import json
        from unittest.mock import patch

        from api.v1.dadata_client import lookup_company_by_inn

        payload = {
            "suggestions": [
                {
                    "data": {
                        "inn": "2465123456",
                        "name": {"full_with_opf": "ООО «Ромашка»"},
                        "address": {
                            "unrestricted_value": "г Москва, Кировский район",
                            "data": {"city_district_with_type": "Кировский район"},
                        },
                        "state": {"status": "ACTIVE"},
                    }
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            result = lookup_company_by_inn("2465123456")

        self.assertEqual(result["district"], "Кировский район")

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_address_components_none_when_dadata_has_no_structured_data(self):
        import json
        from unittest.mock import patch

        from api.v1.dadata_client import lookup_company_by_inn

        payload = {
            "suggestions": [
                {
                    "data": {
                        "inn": "2465123456",
                        "name": {"full_with_opf": "ООО «Ромашка»"},
                        "address": {"unrestricted_value": "г. Москва"},
                        "state": {"status": "ACTIVE"},
                    }
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            result = lookup_company_by_inn("2465123456")

        self.assertEqual(result["address_type"], "legal")
        self.assertIsNone(result["region"])
        self.assertIsNone(result["district"])
        self.assertIsNone(result["city_type"])
        self.assertIsNone(result["city"])
        self.assertIsNone(result["street"])
        self.assertIsNone(result["apartment_office"])
        self.assertIsNone(result["postal_code"])

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_maps_dadata_type_to_entity_type(self):
        """2026-08-03: Витя — нужен тип организации (Организация/ИП) в ответе
        inn-lookup, для автозаполнения того же поля, что и остальные реестровые."""
        import json
        from unittest.mock import patch

        from api.v1.dadata_client import lookup_company_by_inn

        def fake_lookup(dadata_type):
            payload = {
                "suggestions": [
                    {
                        "data": {
                            "inn": "7707083893",
                            "type": dadata_type,
                            "name": {"full_with_opf": "Тест"},
                            "address": {"unrestricted_value": "г. Москва"},
                            "state": {"status": "ACTIVE"},
                        }
                    }
                ]
            }

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps(payload).encode("utf-8")

            with patch("urllib.request.urlopen", return_value=FakeResponse()):
                return lookup_company_by_inn("7707083893")

        self.assertEqual(fake_lookup("LEGAL")["entity_type"], "organization")
        self.assertEqual(fake_lookup("INDIVIDUAL")["entity_type"], "individual_entrepreneur")
        self.assertIsNone(fake_lookup(None)["entity_type"])


class DadataLookupByINNTests(APITestCase):
    def setUp(self):
        self.staff = _make_admin_user()

    def url(self):
        return "/api/v1/suppliers/inn-lookup"

    def test_rejects_bad_checksum_inn(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.get(self.url(), {"inn": "1234567890"})
        self.assertEqual(response.status_code, 400)

    @override_settings(DADATA_API_KEY="", DADATA_SECRET_KEY="")
    def test_returns_503_when_not_configured(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 503)

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_returns_data_when_dadata_finds_company(self):
        from unittest.mock import patch

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        with patch("api.v1.suppliers.views.registration.lookup_company_by_inn") as mock_lookup:
            mock_lookup.return_value = {
                "inn": "7707083893",
                "kpp": "770701001",
                "ogrn": "1027700132195",
                "legal_name": "ООО РОМАШКА",
                "short_name": "ООО «Ромашка»",
                "entity_type": "organization",
                "management_name": "Иванов И.И.",
                "management_post": "Директор",
                "address": "660049, Красноярский край, г Красноярск, ул Новосибирская, стр 4, оф 32",
                "address_type": "legal",
                "region": "Красноярский край",
                "district": "Емельяновский район",
                "city_type": "город",
                "city": "Красноярск",
                "street": "ул Новосибирская, стр 4",
                "apartment_office": "оф 32",
                "postal_code": "660049",
                "status": "ACTIVE",
                "okved": "46.90",
                "okpo": "12345678",
                "registration_date": "2015-03-12",
            }
            response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["legal_name"], "ООО РОМАШКА")
        self.assertEqual(response.data["short_name"], "ООО «Ромашка»")
        self.assertEqual(response.data["entity_type"], "organization")
        self.assertEqual(response.data["okved"], "46.90")
        self.assertEqual(response.data["okpo"], "12345678")
        self.assertEqual(response.data["registration_date"], "2015-03-12")
        self.assertEqual(response.data["address_type"], "legal")
        self.assertEqual(response.data["region"], "Красноярский край")
        self.assertEqual(response.data["district"], "Емельяновский район")
        self.assertEqual(response.data["city_type"], "город")
        self.assertEqual(response.data["city"], "Красноярск")
        self.assertEqual(response.data["street"], "ул Новосибирская, стр 4")
        self.assertEqual(response.data["apartment_office"], "оф 32")
        self.assertEqual(response.data["postal_code"], "660049")

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_returns_404_when_dadata_finds_nothing(self):
        from unittest.mock import patch

        from api.v1.dadata_client import INNLookupError

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        with patch("api.v1.suppliers.views.registration.lookup_company_by_inn") as mock_lookup:
            mock_lookup.side_effect = INNLookupError("Компания с таким ИНН не найдена в dadata.")
            response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 404)

    def test_rejects_venue_user_role(self):
        venue_user = User.objects.create(
            email="not-staff2@example.com", full_name="X", role=UserRole.VENUE_USER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(venue_user))
        response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 403)

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_allows_supplier_role(self):
        """2026-07-28: найдено по жалобе Вити — supplier получал 403, хотя
        именно поставщик должен пользоваться этой ручкой для автозаполнения
        формы создания своей компании (см. registry-fields writeback)."""
        from unittest.mock import patch

        supplier_user = User.objects.create(
            email="supplier-inn-lookup@example.com", full_name="X", role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(supplier_user))
        with patch("api.v1.suppliers.views.registration.lookup_company_by_inn") as mock_lookup:
            mock_lookup.return_value = {
                "inn": "7707083893",
                "kpp": None,
                "ogrn": None,
                "legal_name": "ООО РОМАШКА",
                "management_name": None,
                "management_post": None,
                "address": None,
                "status": "ACTIVE",
                "okved": None,
                "okpo": None,
                "registration_date": None,
            }
            response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 200)


class DadataSuggestAddressClientTests(SimpleTestCase):
    """2026-08-09: "карточка компании — поиск по адресу по тому же принципу,
    что по ИНН". Мокаем urlopen напрямую, как у lookup_company_by_inn."""

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_maps_suggestions_to_structured_fields(self):
        import json
        from unittest.mock import patch

        from api.v1.dadata_client import suggest_addresses

        payload = {
            "suggestions": [
                {
                    "value": "г Красноярск, ул Новосибирская, д 4",
                    "unrestricted_value": "660049, Красноярский край, г Красноярск, ул Новосибирская, д 4",
                    "data": {
                        "postal_code": "660049",
                        "country": "Россия",
                        "region_with_type": "Красноярский край",
                        "area_with_type": None,
                        "city_district_with_type": None,
                        "city_with_type": "г Красноярск",
                        "settlement_with_type": None,
                        "street_with_type": "ул Новосибирская",
                        "house_type": "д",
                        "house": "4",
                        "block_type": None,
                        "block": None,
                        "flat_type": "оф",
                        "flat": "32",
                    },
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            results = suggest_addresses("Новосибирская")

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["value"], "660049, Красноярский край, г Красноярск, ул Новосибирская, д 4")
        self.assertEqual(result["country"], "Россия")
        self.assertEqual(result["region"], "Красноярский край")
        self.assertEqual(result["locality"], "г Красноярск")
        self.assertEqual(result["street"], "ул Новосибирская, д 4")
        self.assertEqual(result["apartment_office"], "оф 32")
        self.assertEqual(result["postal_code"], "660049")

    @override_settings(DADATA_API_KEY="", DADATA_SECRET_KEY="")
    def test_raises_when_not_configured(self):
        from api.v1.dadata_client import AddressLookupError, suggest_addresses

        with self.assertRaises(AddressLookupError):
            suggest_addresses("Новосибирская")

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_empty_suggestions_is_not_an_error(self):
        """В отличие от ИНН (единственная точная запись или 404), пустой
        автокомплит — нормальное состояние, не ошибка."""
        import json
        from unittest.mock import patch

        from api.v1.dadata_client import suggest_addresses

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"suggestions": []}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            results = suggest_addresses("ыыы")

        self.assertEqual(results, [])


class DadataAddressLookupHTTPTests(APITestCase):
    def setUp(self):
        self.staff = _make_admin_user()

    def url(self):
        return "/api/v1/suppliers/address-lookup"

    def test_rejects_request_without_auth(self):
        response = self.client.get(self.url(), {"query": "Новосибирская"})
        self.assertEqual(response.status_code, 401)

    def test_rejects_venue_user_role(self):
        venue_user = User.objects.create(
            email="not-staff-addr@example.com", full_name="X", role=UserRole.VENUE_USER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(venue_user))
        response = self.client.get(self.url(), {"query": "Новосибирская"})
        self.assertEqual(response.status_code, 403)

    @override_settings(DADATA_API_KEY="", DADATA_SECRET_KEY="")
    def test_returns_503_when_not_configured(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        response = self.client.get(self.url(), {"query": "Новосибирская"})
        self.assertEqual(response.status_code, 503)

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_returns_suggestions_list(self):
        from unittest.mock import patch

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.staff))
        with patch("api.v1.suppliers.views.registration.suggest_addresses") as mock_suggest:
            mock_suggest.return_value = [
                {
                    "value": "660049, Красноярский край, г Красноярск, ул Новосибирская, д 4",
                    "country": "Россия",
                    "region": "Красноярский край",
                    "district": None,
                    "locality": "г Красноярск",
                    "street": "ул Новосибирская, д 4",
                    "apartment_office": None,
                    "postal_code": "660049",
                }
            ]
            response = self.client.get(self.url(), {"query": "Новосибирская"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["locality"], "г Красноярск")
        self.assertEqual(response.data[0]["postal_code"], "660049")

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_allows_supplier_role(self):
        from unittest.mock import patch

        supplier_user = User.objects.create(
            email="supplier-address-lookup@example.com",
            full_name="X",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(supplier_user))
        with patch("api.v1.suppliers.views.registration.suggest_addresses") as mock_suggest:
            mock_suggest.return_value = []
            response = self.client.get(self.url(), {"query": "Новосибирская"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])


# ---------------------------------------------------------------------------
# 2026-07-28 — "РЕАЛИЗУЙ ВСЁ": логотип/описание/несколько адресов/контактов/
# банковские реквизиты/условия самовывоза/структурированная доставка/график —
# полный разбор Figma-макета из docs/wiki/supplier-cabinet-figma-design-2026-07.md
# ---------------------------------------------------------------------------


class SupplierScalarFieldsFullSetTests(APITestCase):
    """Логотип/описание/самовывоз/структурированная доставка — новые скалярные
    поля Supplier, доступны при создании и редактировании."""

    def setUp(self):
        self.user = User.objects.create(
            email="scalar-fields@example.com", full_name="Owner", role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
        )

    def test_create_supplier_by_owner_accepts_new_scalar_fields(self):
        from api.v1.suppliers.services import create_supplier_by_owner

        supplier = create_supplier_by_owner(
            user=self.user,
            display_name="Моя компания",
            logo_url="https://example.com/logo.png",
            description="Поставщик свежих овощей и фруктов",
            fulfillment_method="supplier_delivery",
            shipping_point_name="Основной склад",
            shipping_point_address="г. Екатеринбург, ул. Складская, д. 1",
            standard_lead_time="1-2 рабочих дня",
            pickup_available=True,
            pickup_conditions="Самовывоз по будням с 9 до 18",
            pickup_address="г. Екатеринбург, ул. Складская, д. 1",
        )
        supplier.refresh_from_db()
        self.assertEqual(supplier.logo_url, "https://example.com/logo.png")
        self.assertEqual(supplier.description, "Поставщик свежих овощей и фруктов")
        self.assertEqual(supplier.fulfillment_method, "supplier_delivery")
        self.assertEqual(supplier.shipping_point_name, "Основной склад")
        self.assertTrue(supplier.pickup_available)
        self.assertEqual(supplier.pickup_conditions, "Самовывоз по будням с 9 до 18")

    def test_create_and_patch_accept_short_name(self):
        """2026-07-28: Витя — нет поля "Сокращённое наименование". Оно есть в
        макете отдельно от Полного наименования (legal_name) и Публичного
        названия (display_name) — третье, самостоятельное поле."""
        from api.v1.suppliers.services import create_supplier_by_owner, update_supplier_cabinet

        supplier = create_supplier_by_owner(
            user=self.user,
            display_name="Моя компания",
            legal_name="Общество с ограниченной ответственностью «Ромашка»",
            short_name="ООО «Ромашка»",
        )
        supplier.refresh_from_db()
        self.assertEqual(supplier.legal_name, "Общество с ограниченной ответственностью «Ромашка»")
        self.assertEqual(supplier.short_name, "ООО «Ромашка»")

        update_supplier_cabinet(supplier=supplier, data={"short_name": "ООО «Ромашка-2»"})
        supplier.refresh_from_db()
        self.assertEqual(supplier.short_name, "ООО «Ромашка-2»")

    def test_is_published_defaults_to_true_and_is_toggleable(self):
        """2026-07-28: Витя — "опубликована компания или нет" видно только в
        ответе (status), в ручках создания/обновления его не было. Первая
        попытка — переиспользовать status для этого — оказалась опасной:
        SupplierCabinetJWTAuthentication отклоняет ЛЮБОЙ вход в кабинет при
        status != active, значит self-service "снять с публикации" сразу же
        лишало бы поставщика доступа к собственному кабинету без пути назад
        (обнаружено на живой проверке). Решение пользователя — отдельное
        is_published, не влияющее на status/вход в кабинет."""
        from api.v1.suppliers.services import create_supplier_by_owner, update_supplier_cabinet

        supplier = create_supplier_by_owner(user=self.user, display_name="Моя компания")
        supplier.refresh_from_db()
        self.assertTrue(supplier.is_published)
        self.assertEqual(supplier.status, "active")

        supplier2 = create_supplier_by_owner(user=self.user, display_name="Черновик компании", is_published=False)
        supplier2.refresh_from_db()
        self.assertFalse(supplier2.is_published)
        self.assertEqual(supplier2.status, "active")

        update_supplier_cabinet(supplier=supplier, data={"is_published": False})
        supplier.refresh_from_db()
        self.assertFalse(supplier.is_published)
        self.assertEqual(supplier.status, "active")

        update_supplier_cabinet(supplier=supplier, data={"is_published": True})
        supplier.refresh_from_db()
        self.assertTrue(supplier.is_published)

    def test_unpublishing_does_not_lock_supplier_out_of_own_cabinet(self):
        """Регресс на находку выше: is_published=False не должен трогать
        status, иначе SupplierCabinetJWTAuthentication заблокирует вход."""
        from api.v1.suppliers.services import (
            create_supplier_by_owner,
            issue_supplier_cabinet_tokens,
            update_supplier_cabinet,
        )

        supplier = create_supplier_by_owner(user=self.user, display_name="Моя компания")
        update_supplier_cabinet(supplier=supplier, data={"is_published": False})
        supplier.refresh_from_db()

        tokens = issue_supplier_cabinet_tokens(supplier)
        response = self.client.get(
            "/api/v1/suppliers/cabinet/me",
            HTTP_AUTHORIZATION=f"Bearer {tokens['access']}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["supplier"]["is_published"])

    def test_create_supplier_by_owner_accepts_entity_type_and_currency(self):
        """2026-07-28: перепроверка по скриншоту макета выявила пропуск —
        Валюта* и Тип организации (Организация/ИП/Физ. лицо) были в макете,
        но не реализованы в первом заходе. Позже (найдено по вопросу
        пользователя) валюта/мин. сумма разъехались на delivery_*/pickup_*
        — макет показывает их отдельно для курьерской доставки и для
        самовывоза, не общими на компанию."""
        from api.v1.suppliers.services import create_supplier_by_owner

        supplier = create_supplier_by_owner(
            user=self.user,
            display_name="ИП Иванов",
            entity_type="individual_entrepreneur",
            delivery_currency="USD",
            pickup_currency="EUR",
        )
        supplier.refresh_from_db()
        self.assertEqual(supplier.entity_type, "individual_entrepreneur")
        self.assertEqual(supplier.delivery_currency, "USD")
        self.assertEqual(supplier.pickup_currency, "EUR")

    def test_currency_defaults_to_rub_when_not_specified(self):
        from api.v1.suppliers.services import create_supplier_by_owner

        supplier = create_supplier_by_owner(user=self.user, display_name="Без валюты")
        supplier.refresh_from_db()
        self.assertEqual(supplier.delivery_currency, "RUB")
        self.assertEqual(supplier.pickup_currency, "RUB")

    def test_cabinet_update_patches_new_scalar_fields(self):
        from api.v1.suppliers.services import update_supplier_cabinet

        supplier = _make_supplier()
        update_supplier_cabinet(
            supplier=supplier,
            data={
                "logo_url": "https://example.com/logo2.png",
                "description": "Обновлённое описание",
                "fulfillment_method": "pickup",
                "pickup_available": True,
                "pickup_address": "г. Москва, ул. Тестовая, 5",
                "standard_lead_time": "3 дня",
            },
        )
        supplier.refresh_from_db()
        self.assertEqual(supplier.logo_url, "https://example.com/logo2.png")
        self.assertEqual(supplier.description, "Обновлённое описание")
        self.assertEqual(supplier.fulfillment_method, "pickup")
        self.assertTrue(supplier.pickup_available)
        self.assertEqual(supplier.pickup_address, "г. Москва, ул. Тестовая, 5")

    def test_delivery_and_pickup_min_order_and_currency_are_independent(self):
        """2026-07-28: по явному решению пользователя — "Минимальная сумма
        заказа"/"Валюта" в макете показаны отдельно для курьерской доставки
        и отдельно для самовывоза, это два независимых значения, не одно
        общее на компанию."""
        from decimal import Decimal

        from api.v1.suppliers.services import update_supplier_cabinet

        supplier = _make_supplier()
        update_supplier_cabinet(
            supplier=supplier,
            data={
                "delivery_min_order_amount": Decimal("5000.00"),
                "delivery_currency": "RUB",
                "pickup_min_order_amount": Decimal("0.00"),
                "pickup_currency": "RUB",
            },
        )
        supplier.refresh_from_db()
        self.assertEqual(supplier.delivery_min_order_amount, Decimal("5000.00"))
        self.assertEqual(supplier.pickup_min_order_amount, Decimal("0.00"))

        update_supplier_cabinet(supplier=supplier, data={"delivery_min_order_amount": Decimal("10000.00")})
        supplier.refresh_from_db()
        self.assertEqual(supplier.delivery_min_order_amount, Decimal("10000.00"))
        # pickup не должен был измениться от правки delivery-поля.
        self.assertEqual(supplier.pickup_min_order_amount, Decimal("0.00"))

    def test_http_create_and_read_new_scalar_fields(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.post(
            "/api/v1/suppliers/mine",
            {
                "display_name": "ООО Тест",
                "logo_url": "https://example.com/logo.png",
                "description": "Описание",
                "pickup_available": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["logo_url"], "https://example.com/logo.png")
        self.assertEqual(response.data["description"], "Описание")
        self.assertTrue(response.data["pickup_available"])


class SupplierDetailSerializerNestedListsTests(APITestCase):
    """SupplierDetailSerializer отдаёт вложенные списки addresses/contacts/
    bank_accounts/schedule_rules — регрессия на related-manager sourcing."""

    def test_empty_lists_when_nothing_added(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get("/api/v1/suppliers/cabinet/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["supplier"]["addresses"], [])
        self.assertEqual(response.data["supplier"]["contacts"], [])
        self.assertEqual(response.data["supplier"]["bank_accounts"], [])
        self.assertEqual(response.data["supplier"]["schedule_rules"], [])

    def test_nested_items_appear_in_supplier_detail(self):
        supplier = _make_supplier()
        SupplierAddress.objects.create(supplier=supplier, address_type="legal", locality="Екатеринбург")
        SupplierContact.objects.create(supplier=supplier, contact_type="messenger", messenger_type="MAX", value="+7900")
        SupplierBankAccount.objects.create(
            supplier=supplier, bank_name="Сбербанк", account_number="40702810", bik="044525225"
        )
        SupplierScheduleRule.objects.create(
            supplier=supplier,
            schedule_type="delivery",
            weekday=0,
            accepts_from="09:00",
            accepts_until="18:00",
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.get("/api/v1/suppliers/cabinet/me")
        self.assertEqual(len(response.data["supplier"]["addresses"]), 1)
        self.assertEqual(response.data["supplier"]["addresses"][0]["locality"], "Екатеринбург")
        self.assertEqual(len(response.data["supplier"]["contacts"]), 1)
        self.assertEqual(response.data["supplier"]["contacts"][0]["messenger_type"], "MAX")
        self.assertEqual(len(response.data["supplier"]["bank_accounts"]), 1)
        self.assertEqual(response.data["supplier"]["bank_accounts"][0]["bank_name"], "Сбербанк")
        self.assertEqual(len(response.data["supplier"]["schedule_rules"]), 1)
        self.assertEqual(response.data["supplier"]["schedule_rules"][0]["weekday"], 0)


class SupplierCabinetLogoUploadTests(TemporaryMediaRootMixin, APITestCase):
    """2026-07-28: Витя — "загрузка логотипа не работает". До этого logo_url
    был только текстовым полем (вставить готовую ссылку), реальной загрузки
    файла в API не было — только у легаси-HTML-страницы товаров
    (supplier_product_image_upload в api/v1/views.py), не в этом кабинете.

    2026-09-13: логотип расширен до галереи из нескольких фото (по прямой
    просьбе пользователя — "везде, включая оба лого") — POST теперь
    добавляет фото (201, объект {id, image_url, created_at}), не заменяет
    единственный logo_url (было 200, {"logo_url": ...})."""

    def setUp(self):
        self.supplier = _make_supplier()

    def _auth(self, supplier):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

    def test_upload_sets_logo_url_and_persists_file(self):
        self._auth(self.supplier)
        image = SimpleUploadedFile("logo.png", b"fake-png-bytes", content_type="image/png")
        response = self.client.post("/api/v1/suppliers/cabinet/logo", {"logo": image}, format="multipart")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["image_url"])

        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.logo_url, response.data["image_url"])
        self.assertTrue(default_storage.exists(response.data["image_url"].removeprefix(settings.MEDIA_URL)))

    def test_second_upload_adds_photo_without_replacing_logo_url(self):
        self._auth(self.supplier)
        first = self.client.post(
            "/api/v1/suppliers/cabinet/logo",
            {"logo": SimpleUploadedFile("logo1.png", b"first", content_type="image/png")},
            format="multipart",
        ).data
        second = self.client.post(
            "/api/v1/suppliers/cabinet/logo",
            {"logo": SimpleUploadedFile("logo2.png", b"second", content_type="image/png")},
            format="multipart",
        ).data
        self.assertNotEqual(first["id"], second["id"])

        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.logo_url, first["image_url"])

        list_response = self.client.get("/api/v1/suppliers/cabinet/logo")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual({row["id"] for row in list_response.data}, {first["id"], second["id"]})

    def test_delete_photo_that_is_current_logo_falls_back_to_next_oldest(self):
        self._auth(self.supplier)
        first = self.client.post(
            "/api/v1/suppliers/cabinet/logo",
            {"logo": SimpleUploadedFile("logo1.png", b"first", content_type="image/png")},
            format="multipart",
        ).data
        second = self.client.post(
            "/api/v1/suppliers/cabinet/logo",
            {"logo": SimpleUploadedFile("logo2.png", b"second", content_type="image/png")},
            format="multipart",
        ).data

        response = self.client.delete(f"/api/v1/suppliers/cabinet/logo/{first['id']}")
        self.assertEqual(response.status_code, 204)

        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.logo_url, second["image_url"])

    def test_delete_last_photo_clears_logo_url(self):
        self._auth(self.supplier)
        only = self.client.post(
            "/api/v1/suppliers/cabinet/logo",
            {"logo": SimpleUploadedFile("logo.png", b"only", content_type="image/png")},
            format="multipart",
        ).data
        self.client.delete(f"/api/v1/suppliers/cabinet/logo/{only['id']}")
        self.supplier.refresh_from_db()
        self.assertIsNone(self.supplier.logo_url)

    def test_rejects_disallowed_content_type(self):
        self._auth(self.supplier)
        bad_file = SimpleUploadedFile("logo.exe", b"binary", content_type="application/octet-stream")
        response = self.client.post("/api/v1/suppliers/cabinet/logo", {"logo": bad_file}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_rejects_oversized_file(self):
        self._auth(self.supplier)
        big_file = SimpleUploadedFile("logo.png", b"x" * (3 * 1024 * 1024 + 1), content_type="image/png")
        response = self.client.post("/api/v1/suppliers/cabinet/logo", {"logo": big_file}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_rejects_webp_content_type(self):
        """2026-08-03: лимит логотипа сужен до макета (3 МБ, PNG/JPG) —
        WebP/GIF, ранее допустимые, теперь отклоняются."""
        self._auth(self.supplier)
        image = SimpleUploadedFile("logo.webp", b"fake-webp-bytes", content_type="image/webp")
        response = self.client.post("/api/v1/suppliers/cabinet/logo", {"logo": image}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_requires_cabinet_authentication(self):
        image = SimpleUploadedFile("logo.png", b"fake-png-bytes", content_type="image/png")
        response = self.client.post("/api/v1/suppliers/cabinet/logo", {"logo": image}, format="multipart")
        self.assertEqual(response.status_code, 401)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductImageUploadTests(TemporaryMediaRootMixin, APITestCase):
    """2026-08-03: Витя — таблица товаров нуждается в реальной загрузке
    картинки файлом, не только в текстовом поле image_url (PATCH). Тот же
    паттерн, что и у логотипа компании (2026-07-28) — через default_storage,
    не легаси-прокси supplier_product_image_upload (пишет мимо S3).

    2026-09-13: фото товара расширено до галереи (по прямой просьбе
    пользователя — "везде") — POST теперь добавляет фото (201), не заменяет
    единственный image_url (было 200)."""

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier=None):
        supplier = supplier or Supplier.objects.create(legal_name="Image Supplier LLC", display_name="Image Supplier")
        unit = Unit.objects.create(code=f"kg-{Unit.objects.count()}", name="kg", kind=UnitKind.WEIGHT)
        n = Unit.objects.count()
        product = Product.objects.create(
            canonical_name=f"Canonical image {n}", normalized_name=f"image-{n}", base_unit=unit
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            product=product,
            unit=unit,
            supplier_name=f"Image raw {n}",
            normalized_name=f"image-{n}",
            status=EntityStatus.ACTIVE,
        )

    def url(self, supplier_product):
        return f"/api/v1/suppliers/products/{supplier_product.id}/image"

    def test_rejects_without_credentials(self):
        supplier_product = self.create_supplier_product()
        image = SimpleUploadedFile("product.png", b"fake-png-bytes", content_type="image/png")
        response = self.client.post(self.url(supplier_product), {"image": image}, format="multipart")
        self.assertEqual(response.status_code, 401)

    def test_uploads_with_service_token_and_sets_image_url(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        image = SimpleUploadedFile("product.png", b"fake-png-bytes", content_type="image/png")

        response = self.client.post(self.url(supplier_product), {"image": image}, format="multipart")

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["image_url"])
        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.image_url, response.data["image_url"])
        self.assertTrue(default_storage.exists(response.data["image_url"].removeprefix(settings.MEDIA_URL)))

    def test_upload_creates_gallery_position_zero_row(self):
        # 2026-09-21 (§4 ТЗ): the gallery gained an explicit `position` field
        # (needed for reorder) instead of relying on created_at ordering —
        # this pins that the first upload lands at position=0, not just
        # "oldest by created_at".
        supplier_product = self.create_supplier_product()
        self.authenticate()
        image = SimpleUploadedFile("product.png", b"fake-png-bytes", content_type="image/png")

        response = self.client.post(self.url(supplier_product), {"image": image}, format="multipart")

        self.assertEqual(response.status_code, 201)
        main_image = SupplierProductImage.objects.filter(supplier_product=supplier_product, position=0).first()
        self.assertIsNotNone(main_image)
        self.assertEqual(main_image.image_url, response.data["image_url"])

    def test_second_upload_adds_photo_without_replacing_image_url(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        first = self.client.post(
            self.url(supplier_product),
            {"image": SimpleUploadedFile("p1.png", b"first", content_type="image/png")},
            format="multipart",
        ).data
        second = self.client.post(
            self.url(supplier_product),
            {"image": SimpleUploadedFile("p2.png", b"second", content_type="image/png")},
            format="multipart",
        ).data
        self.assertNotEqual(first["id"], second["id"])

        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.image_url, first["image_url"])

        list_response = self.client.get(self.url(supplier_product))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual({row["id"] for row in list_response.data}, {first["id"], second["id"]})

    def test_delete_photo_that_is_current_image_url_falls_back_to_next_oldest(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        first = self.client.post(
            self.url(supplier_product),
            {"image": SimpleUploadedFile("p1.png", b"first", content_type="image/png")},
            format="multipart",
        ).data
        second = self.client.post(
            self.url(supplier_product),
            {"image": SimpleUploadedFile("p2.png", b"second", content_type="image/png")},
            format="multipart",
        ).data

        response = self.client.delete(f"{self.url(supplier_product)}/{first['id']}")
        self.assertEqual(response.status_code, 204)

        supplier_product.refresh_from_db()
        self.assertEqual(supplier_product.image_url, second["image_url"])

    def test_supplier_cabinet_jwt_can_upload_own_product_image(self):
        supplier_product = self.create_supplier_product()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier_product.supplier))
        image = SimpleUploadedFile("product.png", b"fake-png-bytes", content_type="image/png")

        response = self.client.post(self.url(supplier_product), {"image": image}, format="multipart")

        self.assertEqual(response.status_code, 201)

    def test_supplier_cabinet_jwt_cannot_upload_other_suppliers_product_image(self):
        own_product = self.create_supplier_product()
        other_product = self.create_supplier_product()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_product.supplier))
        image = SimpleUploadedFile("product.png", b"fake-png-bytes", content_type="image/png")

        response = self.client.post(self.url(other_product), {"image": image}, format="multipart")

        self.assertEqual(response.status_code, 404)
        other_product.refresh_from_db()
        self.assertIsNone(other_product.image_url)

    def test_supplier_cabinet_jwt_cannot_delete_other_suppliers_product_image(self):
        own_product = self.create_supplier_product()
        other_product = self.create_supplier_product()
        self.authenticate()
        other_image = self.client.post(
            self.url(other_product),
            {"image": SimpleUploadedFile("p.png", b"x", content_type="image/png")},
            format="multipart",
        ).data

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own_product.supplier))
        response = self.client.delete(f"{self.url(other_product)}/{other_image['id']}")
        self.assertEqual(response.status_code, 404)

    def test_rejects_disallowed_content_type(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        bad_file = SimpleUploadedFile("product.exe", b"binary", content_type="application/octet-stream")

        response = self.client.post(self.url(supplier_product), {"image": bad_file}, format="multipart")

        self.assertEqual(response.status_code, 400)

    def test_rejects_oversized_file(self):
        supplier_product = self.create_supplier_product()
        self.authenticate()
        big_file = SimpleUploadedFile("product.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png")

        response = self.client.post(self.url(supplier_product), {"image": big_file}, format="multipart")

        self.assertEqual(response.status_code, 400)


class SupplierCabinetAddressCRUDTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.other = _make_supplier("Other LLC")

    def _auth(self, supplier):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

    def test_create_list_update_delete_cycle(self):
        self._auth(self.supplier)
        create_response = self.client.post(
            "/api/v1/suppliers/cabinet/addresses",
            {"address_type": "legal", "country": "Россия", "locality": "Москва", "street": "Ленина, 1"},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        address_id = create_response.data["id"]

        list_response = self.client.get("/api/v1/suppliers/cabinet/addresses")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.data), 1)

        patch_response = self.client.patch(
            f"/api/v1/suppliers/cabinet/addresses/{address_id}", {"locality": "Санкт-Петербург"}, format="json"
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.data["locality"], "Санкт-Петербург")

        delete_response = self.client.delete(f"/api/v1/suppliers/cabinet/addresses/{address_id}")
        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(SupplierAddress.objects.filter(supplier=self.supplier).count(), 0)

    def test_address_field_is_a_single_combined_string(self):
        """2026-07-28, найдено Витей: в макете есть отдельное поле "Адрес"
        одной строкой ("Красноярский край, г. Красноярск, ул. Новосибирская,
        стр.4, оф. 32") — не только разбивка по country/region/... Оно
        независимо от структурированных полей, не производное от них."""
        self._auth(self.supplier)
        response = self.client.post(
            "/api/v1/suppliers/cabinet/addresses",
            {
                "address_type": "legal",
                "address": "Красноярский край, г. Красноярск, ул. Новосибирская, стр.4, оф. 32",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["address"], "Красноярский край, г. Красноярск, ул. Новосибирская, стр.4, оф. 32")
        self.assertIsNone(response.data["country"])

    def test_cannot_touch_another_suppliers_address(self):
        address = SupplierAddress.objects.create(supplier=self.other, address_type="legal", locality="Казань")
        self._auth(self.supplier)
        patch_response = self.client.patch(
            f"/api/v1/suppliers/cabinet/addresses/{address.id}", {"locality": "Пермь"}, format="json"
        )
        self.assertEqual(patch_response.status_code, 404)
        delete_response = self.client.delete(f"/api/v1/suppliers/cabinet/addresses/{address.id}")
        self.assertEqual(delete_response.status_code, 404)
        address.refresh_from_db()
        self.assertEqual(address.locality, "Казань")

    def test_multiple_addresses_allowed(self):
        self._auth(self.supplier)
        self.client.post(
            "/api/v1/suppliers/cabinet/addresses", {"address_type": "legal", "locality": "Москва"}, format="json"
        )
        self.client.post(
            "/api/v1/suppliers/cabinet/addresses", {"address_type": "actual", "locality": "Пермь"}, format="json"
        )
        response = self.client.get("/api/v1/suppliers/cabinet/addresses")
        self.assertEqual(len(response.data), 2)


class SupplierCabinetContactCRUDTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.other = _make_supplier("Other LLC")

    def test_link_contact_type_supports_multiple_website_and_social_links(self):
        """2026-07-28: в макете "Ссылка на сайт или социальные сети" — с
        кнопкой "Добавить", т.е. несколько значений, не одно поле website."""
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        first = self.client.post(
            "/api/v1/suppliers/cabinet/contacts",
            {"contact_type": "link", "value": "https://example.com"},
            format="json",
        )
        self.assertEqual(first.status_code, 201)
        second = self.client.post(
            "/api/v1/suppliers/cabinet/contacts",
            {"contact_type": "link", "value": "https://vk.com/example"},
            format="json",
        )
        self.assertEqual(second.status_code, 201)
        response = self.client.get("/api/v1/suppliers/cabinet/contacts")
        self.assertEqual(len(response.data), 2)

    def test_create_list_update_delete_cycle(self):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        create_response = self.client.post(
            "/api/v1/suppliers/cabinet/contacts",
            {"contact_type": "messenger", "messenger_type": "Telegram", "value": "@supplier_bot"},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        contact_id = create_response.data["id"]

        list_response = self.client.get("/api/v1/suppliers/cabinet/contacts")
        self.assertEqual(len(list_response.data), 1)

        patch_response = self.client.patch(
            f"/api/v1/suppliers/cabinet/contacts/{contact_id}", {"value": "@updated_bot"}, format="json"
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.data["value"], "@updated_bot")

        delete_response = self.client.delete(f"/api/v1/suppliers/cabinet/contacts/{contact_id}")
        self.assertEqual(delete_response.status_code, 204)

    def test_cannot_touch_another_suppliers_contact(self):
        contact = SupplierContact.objects.create(supplier=self.other, contact_type="email", value="x@example.com")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        response = self.client.delete(f"/api/v1/suppliers/cabinet/contacts/{contact.id}")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(SupplierContact.objects.filter(pk=contact.id).exists())


class SupplierCabinetBankAccountCRUDTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.other = _make_supplier("Other LLC")

    def test_create_list_update_delete_cycle(self):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(self.supplier))
        create_response = self.client.post(
            "/api/v1/suppliers/cabinet/bank-accounts",
            {"bank_name": "Тинькофф", "account_number": "40702810000000000001", "bik": "044525974"},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        account_id = create_response.data["id"]

        second_response = self.client.post(
            "/api/v1/suppliers/cabinet/bank-accounts",
            {
                "bank_name": "Сбербанк",
                "account_number": "40702810000000000002",
                "bik": "044525225",
                "is_primary": True,
            },
            format="json",
        )
        self.assertEqual(second_response.status_code, 201)

        list_response = self.client.get("/api/v1/suppliers/cabinet/bank-accounts")
        self.assertEqual(len(list_response.data), 2)

        patch_response = self.client.patch(
            f"/api/v1/suppliers/cabinet/bank-accounts/{account_id}", {"is_primary": True}, format="json"
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertTrue(patch_response.data["is_primary"])

        delete_response = self.client.delete(f"/api/v1/suppliers/cabinet/bank-accounts/{account_id}")
        self.assertEqual(delete_response.status_code, 204)

    def test_is_primary_is_exclusive_across_accounts_on_create(self):
        """2026-07-28, найдено сеньор-ревью: без явного сброса чужого флага
        два счёта могли одновременно оказаться is_primary=True."""
        first = SupplierBankAccount.objects.create(
            supplier=self.supplier, bank_name="Банк 1", account_number="1", bik="044525225", is_primary=True
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(self.supplier))
        response = self.client.post(
            "/api/v1/suppliers/cabinet/bank-accounts",
            {"bank_name": "Банк 2", "account_number": "2", "bik": "044525974", "is_primary": True},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        first.refresh_from_db()
        self.assertFalse(first.is_primary)

    def test_is_primary_is_exclusive_across_accounts_on_update(self):
        first = SupplierBankAccount.objects.create(
            supplier=self.supplier, bank_name="Банк 1", account_number="1", bik="044525225", is_primary=True
        )
        second = SupplierBankAccount.objects.create(
            supplier=self.supplier, bank_name="Банк 2", account_number="2", bik="044525974"
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(self.supplier))
        response = self.client.patch(
            f"/api/v1/suppliers/cabinet/bank-accounts/{second.id}", {"is_primary": True}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        self.assertFalse(first.is_primary)
        self.assertEqual(SupplierBankAccount.objects.filter(supplier=self.supplier, is_primary=True).count(), 1)

    def test_cannot_touch_another_suppliers_bank_account(self):
        account = SupplierBankAccount.objects.create(
            supplier=self.other, bank_name="Банк", account_number="123", bik="044525225"
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_owner_bearer(self.supplier))
        response = self.client.patch(
            f"/api/v1/suppliers/cabinet/bank-accounts/{account.id}", {"bank_name": "Другой"}, format="json"
        )
        self.assertEqual(response.status_code, 404)

    def test_manager_can_create_bank_account_too(self):
        """2026-08-01: изначально было owner-only (банковские реквизиты меняют,
        куда уходят деньги). 2026-08-05: пользователь явно решил, что owner и
        manager имеют одинаковые права — требуется лишь идентифицированная
        активная сессия (не bot без linked user), не конкретно owner."""
        manager = User.objects.create(
            email="manager-bank-create@example.com", full_name="Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier, user=manager))
        response = self.client.post(
            "/api/v1/suppliers/cabinet/bank-accounts",
            {"bank_name": "Тинькофф", "account_number": "40702810000000000001", "bik": "044525974"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_bot_session_without_linked_user_cannot_create_bank_account(self):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        response = self.client.post(
            "/api/v1/suppliers/cabinet/bank-accounts",
            {"bank_name": "Тинькофф", "account_number": "40702810000000000001", "bik": "044525974"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_any_active_member_can_still_list_bank_accounts(self):
        """GET остаётся открытым любому участнику — ограничение только на изменение."""
        SupplierBankAccount.objects.create(
            supplier=self.supplier, bank_name="Банк 1", account_number="1", bik="044525225"
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        response = self.client.get("/api/v1/suppliers/cabinet/bank-accounts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)


class SupplierCabinetScheduleRuleCRUDTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.other = _make_supplier("Other LLC")

    def test_create_list_update_delete_cycle(self):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        create_response = self.client.post(
            "/api/v1/suppliers/cabinet/schedule-rules",
            {
                "schedule_type": "delivery",
                "weekday": 1,
                "accepts_from": "09:00",
                "accepts_until": "18:00",
                "fulfillment_weekday": 3,
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        rule_id = create_response.data["id"]
        self.assertEqual(create_response.data["fulfillment_weekday"], 3)

        list_response = self.client.get("/api/v1/suppliers/cabinet/schedule-rules")
        self.assertEqual(len(list_response.data), 1)

        patch_response = self.client.patch(
            f"/api/v1/suppliers/cabinet/schedule-rules/{rule_id}", {"accepts_until": "20:00"}, format="json"
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.data["accepts_until"], "20:00:00")

        delete_response = self.client.delete(f"/api/v1/suppliers/cabinet/schedule-rules/{rule_id}")
        self.assertEqual(delete_response.status_code, 204)

    def test_pickup_schedule_type_supported(self):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        response = self.client.post(
            "/api/v1/suppliers/cabinet/schedule-rules",
            {"schedule_type": "pickup", "weekday": 5, "accepts_from": "10:00", "accepts_until": "16:00"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["schedule_type"], "pickup")

    def test_cannot_touch_another_suppliers_schedule_rule(self):
        rule = SupplierScheduleRule.objects.create(
            supplier=self.other, schedule_type="delivery", weekday=0, accepts_from="09:00", accepts_until="18:00"
        )
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        response = self.client.delete(f"/api/v1/suppliers/cabinet/schedule-rules/{rule.id}")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(SupplierScheduleRule.objects.filter(pk=rule.id).exists())

    def test_is_active_defaults_to_true_and_is_toggleable(self):
        """2026-07-29: "в графиках работы надо статусы еще добавить активны/
        неактивны" — можно временно выключить день/правило, не удаляя строку."""
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        create_response = self.client.post(
            "/api/v1/suppliers/cabinet/schedule-rules",
            {"schedule_type": "delivery", "weekday": 2, "accepts_from": "09:00", "accepts_until": "18:00"},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertTrue(create_response.data["is_active"])
        rule_id = create_response.data["id"]

        patch_response = self.client.patch(
            f"/api/v1/suppliers/cabinet/schedule-rules/{rule_id}", {"is_active": False}, format="json"
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertFalse(patch_response.data["is_active"])

        rule = SupplierScheduleRule.objects.get(pk=rule_id)
        self.assertFalse(rule.is_active)

    def test_bulk_cabinet_update_accepts_is_active_per_rule(self):
        """Массовая замена (PATCH cabinet/me) тоже должна пробрасывать
        is_active — форма сохраняется одной кнопкой, не по одному правилу."""
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))
        response = self.client.patch(
            "/api/v1/suppliers/cabinet/me",
            {
                "schedule_rules": [
                    {
                        "schedule_type": "delivery",
                        "weekday": 0,
                        "accepts_from": "09:00",
                        "accepts_until": "18:00",
                        "is_active": False,
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        rule = SupplierScheduleRule.objects.get(supplier=self.supplier)
        self.assertFalse(rule.is_active)


class SupplierCabinetSubResourcesRequireCabinetAuthTests(APITestCase):
    """Обычный core.User JWT (не cabinet) не должен проходить ни в одну из
    новых суб-ресурсных ручек — те же правила, что и у /cabinet/me."""

    def setUp(self):
        self.user = User.objects.create(
            email="not-cabinet@example.com", full_name="X", role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
        )

    def test_rejects_core_user_jwt_on_addresses(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.get("/api/v1/suppliers/cabinet/addresses")
        self.assertEqual(response.status_code, 401)

    def test_rejects_unauthenticated_on_bank_accounts(self):
        response = self.client.get("/api/v1/suppliers/cabinet/bank-accounts")
        self.assertEqual(response.status_code, 401)


class SupplierListNestedFieldsQueryCountTests(APITestCase):
    """2026-07-28, найдено сеньор-ревью: SupplierDetailSerializer тянет
    вложенные списки (addresses/contacts/bank_accounts/schedule_rules,
    2026-09-13 — ещё и images) через related-менеджеры — без
    prefetch_related это N+1 на каждую компанию в списке. Проверяем, что
    число запросов не растёт с числом компаний."""

    def _make_suppliers_with_nested_data(self, count):
        for i in range(count):
            supplier = _make_supplier(f"Query Count LLC {i}")
            SupplierAddress.objects.create(supplier=supplier, address_type="legal", locality="Москва")
            SupplierContact.objects.create(supplier=supplier, contact_type="email", value=f"{i}@example.com")
            SupplierBankAccount.objects.create(
                supplier=supplier, bank_name="Банк", account_number=str(i), bik="044525225"
            )
            SupplierScheduleRule.objects.create(
                supplier=supplier, schedule_type="delivery", weekday=0, accepts_from="09:00", accepts_until="18:00"
            )

    def test_staff_list_query_count_does_not_grow_with_supplier_count(self):
        staff = _make_admin_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(staff))
        self._make_suppliers_with_nested_data(2)
        with self.assertNumQueries(8):
            response = self.client.get("/api/v1/suppliers/")
        self.assertEqual(response.status_code, 200)
        self._make_suppliers_with_nested_data(3)
        with self.assertNumQueries(8):
            response = self.client.get("/api/v1/suppliers/")
        self.assertEqual(response.status_code, 200)

    def test_mine_list_query_count_does_not_grow_with_supplier_count(self):
        """2026-08-07: +1 запрос (было 6, стало 7) — my_role (запрос Вити)
        читает role вместе с supplier_id одним запросом к SupplierUser
        отдельно от списка компаний, вместо lazy id__in-подзапроса.
        2026-09-13: +1 запрос ещё раз (7 → 8) — prefetch_related("images") для
        новой галереи логотипа. Фиксированная стоимость, не растёт с числом
        компаний — проверено на 2 и на 3+."""
        from api.v1.suppliers.services import create_supplier_by_owner

        user = User.objects.create(
            email="query-count-owner@example.com", full_name="Owner", role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
        )
        create_supplier_by_owner(user=user, display_name="Компания 1")
        create_supplier_by_owner(user=user, display_name="Компания 2")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        with self.assertNumQueries(8):
            response = self.client.get("/api/v1/suppliers/mine")
        self.assertEqual(response.status_code, 200)

        create_supplier_by_owner(user=user, display_name="Компания 3")
        with self.assertNumQueries(8):
            response = self.client.get("/api/v1/suppliers/mine")
        self.assertEqual(response.status_code, 200)


@override_settings(
    SERVICE_API_TOKEN="test-token",
    CATALOG_QDRANT_AUTO_SYNC_ENABLED=True,
    QDRANT_URL="https://qdrant.example",
    QDRANT_API_KEY="secret",
)
class BotSupplierPriceUploadTextAPITests(APITestCase):
    url = "/api/v1/bot-user-bindings/uploads/text"

    def setUp(self):
        self.supplier = Supplier.objects.create(
            legal_name="Bot Supplier LLC",
            display_name="Bot Supplier",
        )
        self.binding = BotUserBinding.objects.create(
            supplier=self.supplier,
            channel=SourceChannel.TELEGRAM,
            chat_id="chat-1",
            external_user_id="user-1",
            status=EntityStatus.ACTIVE,
        )
        Unit.objects.get_or_create(code="\u0448\u0442", defaults={"name": "piece", "kind": UnitKind.PIECE})

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def payload(self, **overrides):
        payload = {
            "platform": SourceChannel.TELEGRAM,
            "chat_id": "chat-1",
            "telegram_user_id": "user-1",
            "message_id": "message-1",
            "text": "\u0410\u0440\u0442\u0438\u0448\u043e\u043a \u0438\u0437 \u0431\u043e\u0442\u0430 777 \u0440\u0443\u0431 \u0448\u0442",
            "use_ai": False,
        }
        payload.update(overrides)
        return payload

    def test_requires_service_token(self):
        response = self.client.post(self.url, self.payload(), format="json")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(SupplierPriceUpload.objects.exists())

    def test_returns_404_when_bot_user_is_not_linked(self):
        self.authenticate()

        response = self.client.post(
            self.url,
            self.payload(chat_id="missing", telegram_user_id="missing"),
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "BOT_USER_BINDING_NOT_FOUND")
        self.assertFalse(SupplierPriceUpload.objects.exists())

    @patch("api.v1.suppliers.tasks.normalize_supplier_price_upload_task.delay")
    def test_queues_normalization_for_linked_bot_user(self, normalize_delay):
        normalize_delay.return_value.id = "normalize-task-id"
        self.authenticate()

        response = self.client.post(self.url, self.payload(), format="json")

        self.assertEqual(response.status_code, 202)
        upload = SupplierPriceUpload.objects.get()
        self.assertEqual(upload.supplier, self.supplier)
        self.assertEqual(upload.source_channel, SourceChannel.TELEGRAM)
        self.assertEqual(upload.raw_payload["metadata"]["telegram_message_id"], "message-1")
        normalize_delay.assert_called_once_with(
            upload_id=str(upload.id),
            use_ai=False,
            ai_mode=None,
            confirm_full=False,
            run_ai_cleanup=False,
            ai_cleanup_scope="all",
        )
        self.assertEqual(response.json()["status_url"], f"/api/v1/suppliers/uploads/{upload.id}")
        self.assertEqual(response.json()["normalization"]["celery_task_id"], "normalize-task-id")
        self.assertEqual(BotUserState.objects.get().state, "price_processing")
        self.assertEqual(WorkflowEvent.objects.get().event_type, "bot_price_upload_received")

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    @patch("api.v1.suppliers.tasks.sync_supplier_catalog_to_qdrant_task.delay")
    def test_sync_flow_reaches_catalog_and_qdrant_queue(self, qdrant_delay, _vector_scores):
        qdrant_delay.return_value.id = "qdrant-task-id"
        self.authenticate()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.url,
                self.payload(run_async=False),
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["binding"]["supplier_id"], str(self.supplier.id))
        self.assertIn(response.json()["normalization"]["status"], {UploadStatus.APPLIED, UploadStatus.NEEDS_REVIEW})
        self.assertEqual(response.json()["normalization"]["products_created"], 1)

        product = SupplierProduct.objects.get(supplier=self.supplier)
        catalog_response = self.client.get(
            "/api/v1/catalog/products",
            {"q": "\u0430\u0440\u0442\u0438\u0448\u043e\u043a \u0438\u0437 \u0431\u043e\u0442\u0430"},
        )
        self.assertEqual(catalog_response.status_code, 200)
        self.assertEqual(catalog_response.json()["count"], 1)
        self.assertEqual(catalog_response.json()["results"][0]["id"], str(product.id))
        qdrant_delay.assert_called_once_with(supplier_id=str(self.supplier.id), upload_id=str(product.last_upload_id))

        bot_state = BotUserState.objects.get(platform=SourceChannel.TELEGRAM, chat_id="chat-1")
        self.assertEqual(bot_state.state, "price_processed")
        self.assertEqual(bot_state.payload["status"], response.json()["normalization"]["status"])


class SupplierBulkNestedCollectionsTests(APITestCase):
    """2026-07-28: по запросу пользователя ("ручки излишне не делятся") —
    Figma-форма создания/редактирования карточки компании — один экран, одна
    кнопка "Сохранить", включающая адреса/контакты/банковские реквизиты/
    график. Фронт должен уметь сохранить всё одним запросом, не дёргая 4
    отдельных суб-ресурсных эндпоинта после создания компании."""

    def setUp(self):
        self.user = User.objects.create(
            email="bulk-nested@example.com", full_name="Owner", role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
        )

    def test_create_with_nested_collections_in_one_call(self):
        from api.v1.suppliers.services import create_supplier_by_owner

        supplier = create_supplier_by_owner(
            user=self.user,
            display_name="Моя компания",
            addresses=[{"address_type": "legal", "locality": "Москва"}],
            contacts=[{"contact_type": "email", "value": "info@example.com"}],
            bank_accounts=[
                {"bank_name": "Банк 1", "account_number": "1", "bik": "044525225", "is_primary": True},
                {"bank_name": "Банк 2", "account_number": "2", "bik": "044525974", "is_primary": True},
            ],
            schedule_rules=[
                {"schedule_type": "delivery", "weekday": 0, "accepts_from": "09:00", "accepts_until": "18:00"}
            ],
        )
        self.assertEqual(supplier.addresses.count(), 1)
        self.assertEqual(supplier.contacts.count(), 1)
        self.assertEqual(supplier.schedule_rules.count(), 1)
        accounts = list(supplier.bank_accounts.all())
        self.assertEqual(len(accounts), 2)
        # is_primary эксклюзивен даже внутри одной пачки — только первый остаётся True.
        self.assertEqual(sum(1 for a in accounts if a.is_primary), 1)

    def test_http_create_accepts_nested_collections(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.post(
            "/api/v1/suppliers/mine",
            {
                "display_name": "ООО Одним запросом",
                "addresses": [{"address_type": "legal", "locality": "Казань"}],
                "contacts": [{"contact_type": "phone", "value": "+79001234567"}],
                "bank_accounts": [{"bank_name": "Банк", "account_number": "1", "bik": "044525225"}],
                "schedule_rules": [
                    {"schedule_type": "pickup", "weekday": 5, "accepts_from": "10:00", "accepts_until": "16:00"}
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["addresses"]), 1)
        self.assertEqual(len(response.data["contacts"]), 1)
        self.assertEqual(len(response.data["bank_accounts"]), 1)
        self.assertEqual(len(response.data["schedule_rules"]), 1)

    def test_omitting_nested_field_on_create_leaves_it_empty(self):
        from api.v1.suppliers.services import create_supplier_by_owner

        supplier = create_supplier_by_owner(user=self.user, display_name="Без вложенных данных")
        self.assertEqual(supplier.addresses.count(), 0)
        self.assertEqual(supplier.contacts.count(), 0)
        self.assertEqual(supplier.bank_accounts.count(), 0)
        self.assertEqual(supplier.schedule_rules.count(), 0)

    def test_cabinet_update_replaces_whole_collection_when_provided(self):
        from api.v1.suppliers.services import update_supplier_cabinet

        supplier = _make_supplier()
        SupplierAddress.objects.create(supplier=supplier, address_type="legal", locality="Старый адрес")
        update_supplier_cabinet(
            supplier=supplier,
            data={"addresses": [{"address_type": "actual", "locality": "Новый адрес"}]},
        )
        addresses = list(supplier.addresses.all())
        self.assertEqual(len(addresses), 1)
        self.assertEqual(addresses[0].locality, "Новый адрес")
        self.assertEqual(addresses[0].address_type, "actual")

    def test_cabinet_update_leaves_collection_untouched_when_field_omitted(self):
        from api.v1.suppliers.services import update_supplier_cabinet

        supplier = _make_supplier()
        SupplierAddress.objects.create(supplier=supplier, address_type="legal", locality="Не трогать")
        update_supplier_cabinet(supplier=supplier, data={"description": "Просто обновляем описание"})
        self.assertEqual(supplier.addresses.count(), 1)
        self.assertEqual(supplier.addresses.first().locality, "Не трогать")

    def test_cabinet_update_empty_list_clears_collection(self):
        from api.v1.suppliers.services import update_supplier_cabinet

        supplier = _make_supplier()
        SupplierAddress.objects.create(supplier=supplier, address_type="legal", locality="Удалить меня")
        update_supplier_cabinet(supplier=supplier, data={"addresses": []})
        self.assertEqual(supplier.addresses.count(), 0)

    def test_http_cabinet_patch_replaces_nested_collections(self):
        supplier = _make_supplier()
        SupplierContact.objects.create(supplier=supplier, contact_type="email", value="old@example.com")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        response = self.client.patch(
            "/api/v1/suppliers/cabinet/me",
            {"contacts": [{"contact_type": "email", "value": "new@example.com"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["supplier"]["contacts"]), 1)
        self.assertEqual(response.data["supplier"]["contacts"][0]["value"], "new@example.com")


class UpdateSupplierCabinetNestedCollectionsConcurrencyTests(TransactionTestCase):
    """2026-07-28, при финальной перепроверке всей bulk-логики проверялось,
    может ли _sync_supplier_nested_collections (.all().delete() +
    .bulk_create() без явного лока) дать смешанный результат при двух
    одновременных PATCH cabinet/me. Попытка форсировать пересечение через
    threading.Barrier прямо перед вызовом _sync_supplier_nested_collections
    привела к дедлоку, а не к гонке — потому что supplier.save() (вызывается
    раньше в той же функции) уже берёт row-lock в Postgres на обновляемую
    строку, и второй вызов блокируется на нём же, до места, где стоял бы
    барьер, не доходя. Т.е. в текущем коде реальной гонки эмпирически не
    обнаружено — save() уже неявно сериализует конкурентные вызовы. Явный
    select_for_update() в update_supplier_cabinet оставлен как документация
    инварианта и защита на случай будущего рефакторинга (например, если
    порядок вызовов поменяют местами), не как фикс подтверждённого бага.
    Этот тест — обычные потоки без искусственной синхронизации, просто
    убеждается, что параллельные PATCH не роняют ошибку и не портят данные."""

    def _run_pair(self, supplier_id, payload_a, payload_b):
        from api.v1.suppliers.services import update_supplier_cabinet

        errors = []

        def worker(payload):
            connections.close_all()
            try:
                supplier = Supplier.objects.get(pk=supplier_id)
                update_supplier_cabinet(supplier=supplier, data=payload)
            except Exception as exc:  # noqa: BLE001 — capturing for assertion below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(payload,)) for payload in (payload_a, payload_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    def test_concurrent_patches_do_not_error_or_merge_nested_collections(self):
        supplier = Supplier.objects.create(legal_name="Race LLC", display_name="Race LLC")
        payload_a = {"addresses": [{"address_type": "legal", "locality": "A"}]}
        payload_b = {
            "addresses": [
                {"address_type": "actual", "locality": "B1"},
                {"address_type": "actual", "locality": "B2"},
            ]
        }
        errors = self._run_pair(supplier.id, payload_a, payload_b)
        self.assertEqual(errors, [])
        final_count = SupplierAddress.objects.filter(supplier=supplier).count()
        # Итог — ровно намерение одного из двух запросов (1 или 2), не их склейка (3).
        self.assertIn(final_count, (1, 2))


# ---------------------------------------------------------------------------
# supplier_product_deleted flag on referencing rows
# ---------------------------------------------------------------------------


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductDeletedFlagTests(APITestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(legal_name="ООО Ромашка", display_name="Ромашка")
        self.venue = Venue.objects.create(display_name="Кафе Флаг")
        self.sp = SupplierProduct.objects.create(
            supplier=self.supplier, normalized_name="Лавровый лист", status=EntityStatus.ACTIVE
        )

    def test_flag_not_set_on_delete(self):
        """2026-09-23: delete_supplier_product больше не hard-delete (см.
        SupplierProductDeleteHTTPMethodAPITests) — товар остаётся строкой в
        БД (ARCHIVED), поэтому FK у ссылающихся таблиц не обнуляется, и этот
        флаг (нужен был только чтобы пережить исчезновение строки) больше не
        проставляется, ровно как и при archive."""
        from api.v1.suppliers.services import delete_supplier_product

        matrix_item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Лавровый лист", supplier_product=self.sp, status=EntityStatus.ACTIVE
        )
        cart_user = User.objects.create(email="flag-cart-owner@example.com", full_name="Flag", role=UserRole.VENUE_USER)
        cart = VenueCart.objects.create(venue=self.venue, user=cart_user)
        cart_item = VenueCartItem.objects.create(
            cart=cart, supplier_product=self.sp, requested_name="Лавровый лист", requested_qty=1
        )
        order = Order.objects.create(venue=self.venue, order_number="TEST-1")
        order_item = OrderItem.objects.create(
            order=order, supplier_product=self.sp, requested_name="Лавровый лист", requested_qty=1
        )
        template = OrderTemplate.objects.create(venue=self.venue, name="Шаблон флага")
        template_item = OrderTemplateItem.objects.create(
            template=template, supplier_product=self.sp, requested_name="Лавровый лист", requested_qty=1
        )
        receiving = Receiving.objects.create(venue=self.venue)
        receiving_doc = ReceivingDocument.objects.create(
            receiving=receiving, supplier_product=self.sp, received_name="Лавровый лист", received_qty=1
        )

        delete_supplier_product(supplier_product_id=self.sp.id, actor=self.supplier)

        matrix_item.refresh_from_db()
        cart_item.refresh_from_db()
        order_item.refresh_from_db()
        template_item.refresh_from_db()
        receiving_doc.refresh_from_db()
        self.assertFalse(matrix_item.supplier_product_deleted)
        self.assertIsNotNone(matrix_item.supplier_product_id)
        self.assertFalse(cart_item.supplier_product_deleted)
        self.assertFalse(order_item.supplier_product_deleted)
        self.assertFalse(template_item.supplier_product_deleted)
        self.assertFalse(receiving_doc.supplier_product_deleted)

    def test_flag_not_set_on_archive(self):
        from api.v1.suppliers.services import archive_supplier_product

        matrix_item = VenueProductMatrixItem.objects.create(
            venue=self.venue, requested_name="Кориандр", supplier_product=self.sp, status=EntityStatus.ACTIVE
        )
        archive_supplier_product(supplier_product_id=self.sp.id, actor=self.supplier)
        matrix_item.refresh_from_db()
        self.assertFalse(matrix_item.supplier_product_deleted)
        self.assertIsNotNone(matrix_item.supplier_product_id)


# ---------------------------------------------------------------------------
# Task 5.1: SupplierProductImage gallery model (§4 ТЗ)
# ---------------------------------------------------------------------------


class SupplierProductImageModelTests(DjangoTestCase):
    def test_ordering_by_position(self):
        supplier = Supplier.objects.create(display_name="Поставщик А")
        product = SupplierProduct.objects.create(supplier=supplier, supplier_name="Сливки", normalized_name="Сливки")
        SupplierProductImage.objects.create(supplier_product=product, image_url="https://example.com/2.jpg", position=1)
        SupplierProductImage.objects.create(supplier_product=product, image_url="https://example.com/1.jpg", position=0)

        self.assertEqual(
            list(product.images.order_by("position").values_list("image_url", flat=True)),
            ["https://example.com/1.jpg", "https://example.com/2.jpg"],
        )

    def test_deleting_product_cascades_images(self):
        supplier = Supplier.objects.create(display_name="Поставщик Б")
        product = SupplierProduct.objects.create(supplier=supplier, supplier_name="Мука", normalized_name="Мука")
        image = SupplierProductImage.objects.create(
            supplier_product=product, image_url="https://example.com/1.jpg", position=0
        )
        product.delete()
        self.assertFalse(SupplierProductImage.objects.filter(pk=image.pk).exists())


class SupplierProductImageBackfillMigrationTests(DjangoTestCase):
    """suppliers.0043 adds a RunPython data migration
    (backfill_gallery_positions) that (a) assigns sequential `position` to
    every already-existing SupplierProductImage row per product (the gallery
    model from 0040_supplierimage_supplierproductimage predates `position`,
    ordered only by created_at) and (b) copies every SupplierProduct with a
    non-empty image_url but zero gallery rows into a position=0
    SupplierProductImage row. Since manage.py test always runs the full
    migration chain (0043 included) before any test starts, the
    pre-migration state can't be observed through the ORM directly —
    following the pattern in
    api/v1/catalog/tests.py::SupplierOrderRequestBackfillMigrationTests,
    this imports the migration's own function and re-runs it directly
    against rows built by hand."""

    def _run_backfill(self):
        import importlib

        from django.apps import apps as django_apps

        module = importlib.import_module("suppliers.migrations.0043_supplierproductimage_position")
        module.backfill_gallery_positions(django_apps, None)

    def test_backfills_product_with_image_url(self):
        supplier = Supplier.objects.create(display_name="Поставщик В")
        product = SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Сыр",
            normalized_name="Сыр",
            image_url="https://example.com/cheese.jpg",
        )
        # migration already ran once during test-db setup (image_url was
        # empty at that point) — clear whatever it created so this test
        # observes only the re-run against the row built above.
        SupplierProductImage.objects.filter(supplier_product=product).delete()

        self._run_backfill()

        images = list(SupplierProductImage.objects.filter(supplier_product=product))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].position, 0)
        self.assertEqual(images[0].image_url, "https://example.com/cheese.jpg")

    def test_skips_product_without_image_url(self):
        supplier = Supplier.objects.create(display_name="Поставщик Г")
        product = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Соль", normalized_name="Соль", image_url=None
        )

        self._run_backfill()

        self.assertEqual(SupplierProductImage.objects.filter(supplier_product=product).count(), 0)

    def test_rerunning_backfill_does_not_duplicate_existing_gallery_rows(self):
        """A product that already has a gallery row (whether from a prior
        backfill run or created directly, e.g. by Task 5.2's upload endpoint)
        must be left alone by a re-run — normal operation only ever runs this
        migration once (Django tracks it in django_migrations), but the
        function is called directly by this test class and could plausibly be
        re-invoked by a manual replay, so it must be idempotent regardless."""
        supplier = Supplier.objects.create(display_name="Поставщик Д")
        product = SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Масло",
            normalized_name="Масло",
            image_url="https://example.com/butter.jpg",
        )
        SupplierProductImage.objects.filter(supplier_product=product).delete()

        self._run_backfill()
        self._run_backfill()

        images = list(SupplierProductImage.objects.filter(supplier_product=product))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].image_url, "https://example.com/butter.jpg")

    def test_backfill_skips_product_that_already_has_a_gallery_row(self):
        supplier = Supplier.objects.create(display_name="Поставщик Е")
        product = SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Йогурт",
            normalized_name="Йогурт",
            image_url="https://example.com/current.jpg",
        )
        SupplierProductImage.objects.filter(supplier_product=product).delete()
        SupplierProductImage.objects.create(
            supplier_product=product, image_url="https://example.com/already-there.jpg", position=0
        )

        self._run_backfill()

        images = list(SupplierProductImage.objects.filter(supplier_product=product))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].image_url, "https://example.com/already-there.jpg")


# ---------------------------------------------------------------------------
# Task 5.2: CRUD-эндпоинты галереи (загрузка, reorder, удаление) (§4 ТЗ)
# ---------------------------------------------------------------------------


class SupplierProductGalleryServiceTests(TemporaryMediaRootMixin, DjangoTestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(display_name="Поставщик А")
        self.product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сливки", normalized_name="Сливки"
        )

    def _fake_upload(self, name="photo.jpg", content=b"fake-bytes", content_type="image/jpeg"):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def test_first_added_image_becomes_main(self):
        from api.v1.suppliers.services import upload_supplier_product_image

        image = upload_supplier_product_image(supplier_product_id=self.product.id, image_file=self._fake_upload())
        self.assertEqual(image.position, 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.image_url, image.image_url)

    def test_second_added_image_is_not_main(self):
        from api.v1.suppliers.services import upload_supplier_product_image

        upload_supplier_product_image(supplier_product_id=self.product.id, image_file=self._fake_upload("a.jpg"))
        second = upload_supplier_product_image(
            supplier_product_id=self.product.id, image_file=self._fake_upload("b.jpg")
        )
        self.assertEqual(second.position, 1)

    def test_reorder_changes_main_image(self):
        from api.v1.suppliers.services import (
            reorder_supplier_product_image,
            upload_supplier_product_image,
        )

        first = upload_supplier_product_image(
            supplier_product_id=self.product.id, image_file=self._fake_upload("a.jpg")
        )
        second = upload_supplier_product_image(
            supplier_product_id=self.product.id, image_file=self._fake_upload("b.jpg")
        )

        reorder_supplier_product_image(supplier_product_id=self.product.id, ordered_image_ids=[second.id, first.id])

        second.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(second.position, 0)
        self.assertEqual(first.position, 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.image_url, second.image_url)

    def test_delete_image_compacts_positions_and_updates_main(self):
        from api.v1.suppliers.services import delete_supplier_product_image, upload_supplier_product_image

        first = upload_supplier_product_image(
            supplier_product_id=self.product.id, image_file=self._fake_upload("a.jpg")
        )
        second = upload_supplier_product_image(
            supplier_product_id=self.product.id, image_file=self._fake_upload("b.jpg")
        )

        delete_supplier_product_image(supplier_product_id=self.product.id, image_id=first.id)

        second.refresh_from_db()
        self.assertEqual(second.position, 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.image_url, second.image_url)

    def test_delete_last_image_clears_main_image_url(self):
        from api.v1.suppliers.services import delete_supplier_product_image, upload_supplier_product_image

        only = upload_supplier_product_image(supplier_product_id=self.product.id, image_file=self._fake_upload("a.jpg"))

        delete_supplier_product_image(supplier_product_id=self.product.id, image_id=only.id)

        self.product.refresh_from_db()
        self.assertIsNone(self.product.image_url)
        self.assertEqual(self.product.images.count(), 0)

    def test_reorder_rejects_unknown_image_id(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from api.v1.suppliers.services import (
            reorder_supplier_product_image,
            upload_supplier_product_image,
        )

        upload_supplier_product_image(supplier_product_id=self.product.id, image_file=self._fake_upload("a.jpg"))
        with self.assertRaises(DRFValidationError):
            reorder_supplier_product_image(supplier_product_id=self.product.id, ordered_image_ids=[uuid4()])

    def test_reorder_rejects_incomplete_list(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from api.v1.suppliers.services import (
            reorder_supplier_product_image,
            upload_supplier_product_image,
        )

        first = upload_supplier_product_image(
            supplier_product_id=self.product.id, image_file=self._fake_upload("a.jpg")
        )
        upload_supplier_product_image(supplier_product_id=self.product.id, image_file=self._fake_upload("b.jpg"))
        with self.assertRaises(DRFValidationError):
            reorder_supplier_product_image(supplier_product_id=self.product.id, ordered_image_ids=[first.id])


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductGalleryAPITests(TemporaryMediaRootMixin, APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def create_supplier_product(self, supplier=None):
        supplier = supplier or Supplier.objects.create(display_name="Gallery Supplier")
        return SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Товар с галереей", normalized_name="товар с галереей"
        )

    def _fake_upload(self, name="photo.jpg", content=b"fake-bytes", content_type="image/jpeg"):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def images_url(self, supplier_product):
        return f"/api/v1/suppliers/products/{supplier_product.id}/image"

    def reorder_url(self, supplier_product):
        return f"/api/v1/suppliers/products/{supplier_product.id}/image/reorder"

    def image_detail_url(self, supplier_product, image_id):
        return f"/api/v1/suppliers/products/{supplier_product.id}/image/{image_id}"

    def test_post_adds_image_and_sets_product_image_url_on_first_upload(self):
        product = self.create_supplier_product()
        self.authenticate()

        response = self.client.post(self.images_url(product), {"image": self._fake_upload()}, format="multipart")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["position"], 0)
        self.assertTrue(response.data["image_url"])
        product.refresh_from_db()
        self.assertEqual(product.image_url, response.data["image_url"])
        self.assertTrue(default_storage.exists(response.data["image_url"].removeprefix(settings.MEDIA_URL)))

    def test_post_second_image_does_not_change_main_image(self):
        product = self.create_supplier_product()
        self.authenticate()
        self.client.post(self.images_url(product), {"image": self._fake_upload("a.jpg")}, format="multipart")
        product.refresh_from_db()
        first_image_url = product.image_url

        response = self.client.post(self.images_url(product), {"image": self._fake_upload("b.jpg")}, format="multipart")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["position"], 1)
        product.refresh_from_db()
        self.assertEqual(product.image_url, first_image_url)

    def test_post_rejects_without_credentials(self):
        product = self.create_supplier_product()
        response = self.client.post(self.images_url(product), {"image": self._fake_upload()}, format="multipart")
        self.assertEqual(response.status_code, 401)

    def test_post_supplier_cabinet_jwt_cannot_upload_to_other_suppliers_product(self):
        own = self.create_supplier_product()
        other = self.create_supplier_product()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own.supplier))

        response = self.client.post(self.images_url(other), {"image": self._fake_upload()}, format="multipart")

        self.assertEqual(response.status_code, 404)

    def test_post_rejects_disallowed_content_type(self):
        product = self.create_supplier_product()
        self.authenticate()
        bad_file = SimpleUploadedFile("product.exe", b"binary", content_type="application/octet-stream")

        response = self.client.post(self.images_url(product), {"image": bad_file}, format="multipart")

        self.assertEqual(response.status_code, 400)

    def test_patch_reorder_changes_main_image_and_returns_product(self):
        product = self.create_supplier_product()
        self.authenticate()
        first_resp = self.client.post(
            self.images_url(product), {"image": self._fake_upload("a.jpg")}, format="multipart"
        )
        second_resp = self.client.post(
            self.images_url(product), {"image": self._fake_upload("b.jpg")}, format="multipart"
        )
        first_id, second_id = first_resp.data["id"], second_resp.data["id"]

        response = self.client.patch(
            self.reorder_url(product), {"ordered_image_ids": [second_id, first_id]}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], str(product.id))
        images_by_id = {img["id"]: img for img in response.data["images"]}
        self.assertEqual(images_by_id[second_id]["position"], 0)
        self.assertEqual(images_by_id[first_id]["position"], 1)
        self.assertEqual(response.data["image_url"], images_by_id[second_id]["image_url"])

    def test_patch_reorder_rejects_incomplete_id_list(self):
        product = self.create_supplier_product()
        self.authenticate()
        first_resp = self.client.post(
            self.images_url(product), {"image": self._fake_upload("a.jpg")}, format="multipart"
        )
        self.client.post(self.images_url(product), {"image": self._fake_upload("b.jpg")}, format="multipart")

        response = self.client.patch(
            self.reorder_url(product), {"ordered_image_ids": [first_resp.data["id"]]}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_patch_reorder_rejects_foreign_image_id(self):
        product = self.create_supplier_product()
        other_product = self.create_supplier_product()
        self.authenticate()
        own_resp = self.client.post(self.images_url(product), {"image": self._fake_upload("a.jpg")}, format="multipart")
        foreign_resp = self.client.post(
            self.images_url(other_product), {"image": self._fake_upload("c.jpg")}, format="multipart"
        )

        response = self.client.patch(
            self.reorder_url(product),
            {"ordered_image_ids": [own_resp.data["id"], foreign_resp.data["id"]]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_patch_reorder_on_other_suppliers_product_is_404_for_cabinet_jwt(self):
        own = self.create_supplier_product()
        other = self.create_supplier_product()
        self.authenticate()
        image_resp = self.client.post(self.images_url(other), {"image": self._fake_upload()}, format="multipart")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own.supplier))

        response = self.client.patch(
            self.reorder_url(other), {"ordered_image_ids": [image_resp.data["id"]]}, format="json"
        )

        self.assertEqual(response.status_code, 404)

    def test_delete_removes_image_and_compacts_remaining_positions(self):
        product = self.create_supplier_product()
        self.authenticate()
        first_resp = self.client.post(
            self.images_url(product), {"image": self._fake_upload("a.jpg")}, format="multipart"
        )
        second_resp = self.client.post(
            self.images_url(product), {"image": self._fake_upload("b.jpg")}, format="multipart"
        )

        response = self.client.delete(self.image_detail_url(product, first_resp.data["id"]))

        self.assertEqual(response.status_code, 204)
        self.assertEqual(SupplierProductImage.objects.filter(supplier_product=product).count(), 1)
        remaining = SupplierProductImage.objects.get(pk=second_resp.data["id"])
        self.assertEqual(remaining.position, 0)
        product.refresh_from_db()
        self.assertEqual(product.image_url, remaining.image_url)

    def test_delete_last_image_sets_product_image_url_to_null(self):
        product = self.create_supplier_product()
        self.authenticate()
        only_resp = self.client.post(self.images_url(product), {"image": self._fake_upload()}, format="multipart")

        response = self.client.delete(self.image_detail_url(product, only_resp.data["id"]))

        self.assertEqual(response.status_code, 204)
        product.refresh_from_db()
        self.assertIsNone(product.image_url)

    def test_delete_on_other_suppliers_product_is_404_for_cabinet_jwt(self):
        own = self.create_supplier_product()
        other = self.create_supplier_product()
        self.authenticate()
        image_resp = self.client.post(self.images_url(other), {"image": self._fake_upload()}, format="multipart")
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(own.supplier))

        response = self.client.delete(self.image_detail_url(other, image_resp.data["id"]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(SupplierProductImage.objects.filter(pk=image_resp.data["id"]).exists())

    def test_delete_unknown_image_id_is_404(self):
        product = self.create_supplier_product()
        self.authenticate()

        response = self.client.delete(self.image_detail_url(product, uuid4()))

        self.assertEqual(response.status_code, 404)

    def test_product_list_includes_images_field(self):
        product = self.create_supplier_product()
        self.authenticate()
        self.client.post(self.images_url(product), {"image": self._fake_upload()}, format="multipart")

        response = self.client.get("/api/v1/suppliers/products")

        self.assertEqual(response.status_code, 200)
        row = next(row for row in response.data["results"] if row["id"] == str(product.id))
        self.assertEqual(len(row["images"]), 1)
        self.assertEqual(row["images"][0]["position"], 0)
        self.assertEqual(set(row["images"][0]), {"id", "image_url", "position", "created_at"})

    def test_product_list_query_count_does_not_grow_with_gallery_images(self):
        """Same N+1 bug class as Task 1 (docs/wiki + brief's own warning):
        SupplierProductListItemSerializer.get_images reads product.images on
        every row — without .prefetch_related("images") on
        supplier_products_queryset that's one extra query per row. Confirm
        the query count for GET /products stays fixed as gallery images are
        added, the same way SupplierListNestedFieldsQueryCountTests does for
        the nested addresses/contacts/etc. lists."""
        product_a = self.create_supplier_product()
        product_b = self.create_supplier_product()
        self.authenticate()
        self.client.post(self.images_url(product_a), {"image": self._fake_upload("a.jpg")}, format="multipart")
        self.client.post(self.images_url(product_b), {"image": self._fake_upload("b.jpg")}, format="multipart")

        with self.assertNumQueries(3):
            response = self.client.get("/api/v1/suppliers/products")
        self.assertEqual(response.status_code, 200)

        self.client.post(self.images_url(product_a), {"image": self._fake_upload("a2.jpg")}, format="multipart")
        self.client.post(self.images_url(product_b), {"image": self._fake_upload("b2.jpg")}, format="multipart")

        with self.assertNumQueries(3):
            response = self.client.get("/api/v1/suppliers/products")
        self.assertEqual(response.status_code, 200)

    def test_gallery_add_reorder_delete_url_ordering_reorder_not_swallowed_by_image_id(self):
        """Sanity check for the brief's own warning: images/reorder must not
        be shadowed by images/<uuid:image_id> — hit reorder with an empty
        gallery (so there's no valid image id to send) and confirm it's
        routed to the reorder view (400 empty-list validation, not 404)."""
        product = self.create_supplier_product()
        self.authenticate()

        response = self.client.patch(self.reorder_url(product), {"ordered_image_ids": []}, format="json")

        self.assertEqual(response.status_code, 400)


class SupplierProductImportModelsTests(TestCase):
    def test_session_and_item_round_trip(self):
        supplier = _make_supplier()
        session = SupplierProductImportSession.objects.create(supplier=supplier, source_file_name="price.xlsx")
        item = SupplierProductImportItem.objects.create(
            session=session, row_number=1, raw_name="Молоко 3.2%", name="Молоко 3.2%"
        )
        self.assertEqual(item.action, SupplierProductImportAction.REVIEW)
        self.assertEqual(session.status, SupplierProductImportSessionStatus.OPEN)
        self.assertEqual(list(session.items.all()), [item])


class ParseSupplierProductImportFileTests(TestCase):
    def test_parses_rows_by_column_header(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload(
            [
                [
                    "Название",
                    "Страна",
                    "Ед. изм.",
                    "Цена с НДС",
                    "Кратность",
                    "Вес нетто",
                    "Ед. нетто",
                    "Мин. кол-во",
                    "Наличие",
                    "Артикул",
                ],
                ["Молоко 3.2% 1л", "Россия", "шт", "85.50", "12", "1", "л", "1", "В наличии", "SKU-1"],
            ]
        )
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["row_number"], 2)
        self.assertEqual(rows[0]["name"], "Молоко 3.2% 1л")
        self.assertEqual(rows[0]["country_of_origin"], "Россия")
        self.assertEqual(rows[0]["unit_label"], "шт")
        self.assertEqual(rows[0]["price"], "85.50")
        self.assertEqual(rows[0]["supplier_sku"], "SKU-1")

    def test_xlsx_content_with_xls_extension_is_parsed(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload([["Название", "Цена"], ["Молоко", "85"]], name="Прайс Февраль.xls")
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual([r["name"] for r in rows], ["Молоко"])

    def test_binary_xls_gets_readable_error(self):
        from api.v1.suppliers.product_import_parsing import (
            SupplierProductImportParseError,
            parse_supplier_product_import_file,
        )

        ole2_header = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 512
        upload = SimpleUploadedFile("old.xls", ole2_header)
        with self.assertRaisesMessage(SupplierProductImportParseError, "сохраните файл как .xlsx"):
            parse_supplier_product_import_file(upload)

    def test_row_without_name_is_dropped(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload([["Название"], [""], ["Сахар"]])
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual([r["name"] for r in rows], ["Сахар"])

    def test_missing_name_column_raises(self):
        from api.v1.suppliers.product_import_parsing import (
            SupplierProductImportParseError,
            parse_supplier_product_import_file,
        )

        upload = _xlsx_upload([["Цена"], ["10"]])
        with self.assertRaises(SupplierProductImportParseError):
            parse_supplier_product_import_file(upload)

    def test_parses_txt_via_shared_file_import_layer(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        content = "Название;Ед;Цена\nКунжут 1кг;шт;850\n".encode("utf-8-sig")
        upload = SimpleUploadedFile("price.txt", content, content_type="text/plain")

        rows = parse_supplier_product_import_file(upload)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Кунжут 1кг")
        self.assertEqual(rows[0]["price"], "850")

    def test_nomenklatura_header_below_title_block_and_section_rows(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload(
            [
                ["ПРАЙС-ЛИСТ"],
                ["ООО Компания"],
                ["Номенклатура"],
                ["Номенклатура", "", "Цена", "Штрихкод", "Кратность отгрузки"],
                ["Бульоны, супы"],
                ["Бульон Роллтон 90г", "", "36.17", "4605496020035", "3"],
                ["Пюре Роллтон 10 кг", "", "4806.34", "4605496012740", ""],
            ]
        )
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual([r["name"] for r in rows], ["Бульоны, супы", "Бульон Роллтон 90г", "Пюре Роллтон 10 кг"])
        self.assertEqual(rows[1]["price"], "36.17")
        self.assertEqual(rows[1]["delivery_multiple"], "3")

    def test_priced_file_keeps_product_with_blank_price_but_other_cells(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload(
            [
                ["Номенклатура", "Цена", "Штрихкод"],
                ["Хлопья", "", ""],
                ["Мюсли", "", "4600819001517"],
                ["Овсянка", "120", "4600819001518"],
            ]
        )
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual([r["name"] for r in rows], ["Хлопья", "Мюсли", "Овсянка"])
        self.assertNotIn("_name_only", rows[0])

    def test_multirow_header_price_and_unit_columns(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload(
            [
                ["", "Изменение цен номенклатуры"],
                ["№", "", "Товар", "", "а"],
                ["", "", "", "", "Цена", "", "Ед.", "", "Валюта"],
                [1, "", "авокадо ВЕСОВОЙ", "", 330, "", "кг", "", "руб."],
                [2, "", "авокадо штука", "", 60, "", "шт", "", "руб."],
            ]
        )
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual([r["name"] for r in rows], ["авокадо ВЕСОВОЙ", "авокадо штука"])
        self.assertEqual([r["price"] for r in rows], ["330", "60"])
        self.assertEqual([r["unit_label"] for r in rows], ["кг", "шт"])

    def test_1c_header_split_over_rows_and_trailing_unit_in_name(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload(
            [
                ["", "", "", "", "", "", "", "", "", "Розничные"],
                ["Номенклатура, Упаковка", "", "", "", "", "", "", "", "", "руб."],
                ["", "", "", "", "", "", "", "", "", "Включает НДС"],
                ["", "", "", "", "", "", "", "", "", "Цена"],
                ["ПРОДОВОЛЬСТВИЕ"],
                ["Горчица Баварская 1/870 гр., шт", "", "", "", "", "", "", "", "", 224.53],
                ["Сыр Российский 4,5 кг, кг", "", "", "", "", "", "", "", "", 1052.17],
            ]
        )
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual(
            [r["name"] for r in rows],
            ["ПРОДОВОЛЬСТВИЕ", "Горчица Баварская 1/870 гр., шт", "Сыр Российский 4,5 кг, кг"],
        )
        self.assertEqual([r["price"] for r in rows], [None, "224.53", "1052.17"])
        self.assertEqual([r["unit_label"] for r in rows], [None, "шт", "кг"])

    def test_section_row_right_under_header_is_not_swallowed_into_header(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload(
            [
                ["Номенклатура", "Цена"],
                ["Бульоны, супы"],
                ["Бульон куриный 2кг", "1752"],
            ]
        )
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual([r["name"] for r in rows], ["Бульоны, супы", "Бульон куриный 2кг"])

    def test_header_with_alias_as_first_words(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload(
            [
                ["Наименование товара в УС", "Ед. изм. в УС", "Код товара УС"],
                ["Виски Джемесон / л", "л", "186"],
            ]
        )
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual(rows[0]["name"], "Виски Джемесон / л")
        self.assertEqual(rows[0]["unit_label"], "л")

    def test_header_with_comma_after_alias_and_multirow_title_block(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        upload = _xlsx_upload(
            [
                ["ООО Сервис Продукт"],
                ["Прайс-лист на 27 февраля 2019 г."],
                ["Ценовая группа", "", "Базовая цена"],
                ["Артикул", "Номенклатура, Упаковка", "руб."],
                ["", "", "Цена"],
                ["", "Адыгейский Владимирский, кг", "238.95"],
                ["2001", "Гауда Биберталлер, кг", "388.5"],
            ]
        )
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual([r["name"] for r in rows], ["Адыгейский Владимирский, кг", "Гауда Биберталлер, кг"])
        self.assertEqual(rows[1]["supplier_sku"], "2001")

    def test_parses_docx_via_shared_file_import_layer(self):
        from io import BytesIO

        from docx import Document

        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        document = Document()
        table = document.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text = "Название"
        table.rows[0].cells[1].text = "Цена"
        table.rows[1].cells[0].text = "Кунжут 1кг"
        table.rows[1].cells[1].text = "850"
        buffer = BytesIO()
        document.save(buffer)
        upload = SimpleUploadedFile(
            "price.docx",
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        rows = parse_supplier_product_import_file(upload)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Кунжут 1кг")

    def test_net_is_taken_from_the_name_when_file_has_no_net_columns(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        rows = parse_supplier_product_import_file(
            _xlsx_upload([["Название", "Цена"], ["Брынза 250г", "100"], ["Лайм", "50"], ["Сыр 200г + соус 50г", "70"]])
        )
        by_name = {row["name"]: row for row in rows}
        self.assertEqual((by_name["Брынза 250г"]["net_value"], by_name["Брынза 250г"]["net_unit_label"]), ("250", "г"))
        self.assertIsNone(by_name["Лайм"]["net_value"])
        self.assertIsNone(by_name["Сыр 200г + соус 50г"]["net_value"])

    def test_net_column_from_file_wins_over_the_name(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        rows = parse_supplier_product_import_file(_xlsx_upload([["Название", "Вес"], ["Брынза 250г", "300"]]))
        # 250 г из названия не перезаписывает значение колонки файла
        self.assertEqual((rows[0]["net_value"], rows[0]["net_unit_label"]), ("300", None))

    def test_unsupported_extension_raises(self):
        from api.v1.suppliers.product_import_parsing import (
            SupplierProductImportParseError,
            parse_supplier_product_import_file,
        )

        upload = SimpleUploadedFile("price.zip", b"not a real zip", content_type="application/zip")
        with self.assertRaises(SupplierProductImportParseError):
            parse_supplier_product_import_file(upload)


class ImportRowLimitTests(TestCase):
    def _csv(self, rows):
        body = "Название,Цена\n" + "\n".join(f"Товар {i},{i}" for i in range(1, rows + 1))
        return SimpleUploadedFile("big.csv", body.encode("utf-8"), content_type="text/csv")

    def test_file_with_ten_thousand_rows_is_accepted(self):
        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file

        rows = parse_supplier_product_import_file(self._csv(10000))
        self.assertEqual(len(rows), 10000)

    @override_settings(SUPPLIER_PRODUCT_IMPORT_MAX_ROWS=50)
    def test_limit_comes_from_settings(self):
        from api.v1.suppliers.product_import_parsing import (
            SupplierProductImportParseError,
            parse_supplier_product_import_file,
        )

        with self.assertRaisesMessage(SupplierProductImportParseError, "Слишком много строк. Максимум — 50."):
            parse_supplier_product_import_file(self._csv(51))

    @override_settings(SUPPLIER_PRODUCT_IMPORT_MAX_ROWS=12000)
    def test_file_over_the_limit_is_rejected_not_silently_truncated(self):
        from api.v1.suppliers.product_import_parsing import (
            SupplierProductImportParseError,
            parse_supplier_product_import_file,
        )

        with self.assertRaisesMessage(SupplierProductImportParseError, "Слишком много строк. Максимум — 12000."):
            parse_supplier_product_import_file(self._csv(12500))

    def test_import_task_has_its_own_time_limit_above_global(self):
        from django.conf import settings

        from api.v1.suppliers.tasks import process_supplier_product_import_session_task

        self.assertGreater(process_supplier_product_import_session_task.time_limit, settings.CELERY_TASK_TIME_LIMIT)
        self.assertLess(
            process_supplier_product_import_session_task.soft_time_limit,
            process_supplier_product_import_session_task.time_limit,
        )


class SupplierProductImportFileStorageTests(TemporaryMediaRootMixin, APITestCase):
    URL = IMPORT_ANALYZE_URL

    def setUp(self):
        patcher = patch("api.v1.suppliers.tasks.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)
        delay = patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay")
        self.mock_delay = delay.start()
        self.addCleanup(delay.stop)
        self.supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))

    def _post(self, rows=None, content=None):
        # openpyxl кладёт в xlsx время создания -- байты двух вызовов _xlsx_upload различаются; чтобы получить
        # «тот же файл», нужно повторно отправить одни и те же байты.
        if content is None:
            content = _xlsx_upload(rows or [["Название"], ["Молоко"], ["Сыр"]]).read()
        upload = SimpleUploadedFile("products.xlsx", content)
        return self.client.post(self.URL, {"file": upload}, format="multipart")

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True)
    def test_file_is_stored_and_linked_to_the_session(self):
        response = self._post()
        self.assertEqual(response.status_code, 202, response.content)
        session = SupplierProductImportSession.objects.get(pk=response.json()["id"])
        self.assertIsNotNone(session.import_file)
        self.assertEqual(session.import_file.scope_id, self.supplier.id)
        self.assertEqual(session.import_file.scope_type, "supplier")
        self.assertEqual(session.import_file.sha256, session.content_sha256)
        self.assertTrue(default_storage.exists(session.import_file.storage_key))

    def test_flag_off_keeps_old_behaviour(self):
        response = self._post()
        self.assertEqual(response.status_code, 202, response.content)
        session = SupplierProductImportSession.objects.get(pk=response.json()["id"])
        self.assertIsNone(session.import_file)

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True)
    def test_storage_failure_gives_503_and_creates_no_session(self):
        with patch("file_imports.services.default_storage.save", side_effect=OSError("s3 down")):
            response = self._post()
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(
            response.json(), {"detail": "Не удалось сохранить исходный файл. Загрузите файл ещё раз чуть позже."}
        )
        self.assertEqual(SupplierProductImportSession.objects.count(), 0)

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True)
    def test_unparseable_file_is_not_stored(self):
        response = self._post(rows=[["Цена"], ["10"]])  # нет колонки названия
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ImportFile.objects.count(), 0)

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True)
    def test_second_session_for_same_file_reuses_the_stored_file(self):
        content = _xlsx_upload([["Название"], ["Молоко"], ["Сыр"]]).read()
        first = self._post(content=content)
        SupplierProductImportSession.objects.filter(pk=first.json()["id"]).update(status="committed")
        second = self._post(content=content)
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(ImportFile.objects.count(), 1)


def _docx_upload(rows, name="price.docx"):
    from io import BytesIO

    from docx import Document

    document = Document()
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            table.rows[row_index].cells[column_index].text = value
    buffer = BytesIO()
    document.save(buffer)
    return SimpleUploadedFile(
        name,
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


class ExtractSupplierProductImportSessionTests(TemporaryMediaRootMixin, TestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        # дозаполнение LLM в этих тестах не нужно
        patcher = patch(
            "api.v1.suppliers.product_import_selectors.enrich_supplier_product_import_rows", return_value={}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self, upload, *, expired=False):
        from datetime import timedelta

        from django.utils import timezone

        from file_imports.models import ImportFile, ImportFileScope
        from file_imports.services import store_import_file

        import_file = store_import_file(
            uploaded_file=upload, scope_type=ImportFileScope.SUPPLIER, scope_id=self.supplier.id
        )
        if expired:
            ImportFile.objects.filter(pk=import_file.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
            import_file.refresh_from_db()
        return SupplierProductImportSession.objects.create(
            supplier=self.supplier,
            source_file_name=upload.name,
            processing_status="pending",
            import_file=import_file,
        )

    def test_extracts_rows_from_stored_file_and_marks_session_ready(self):
        from api.v1.suppliers.product_import_selectors import extract_supplier_product_import_session

        session = self._session(_docx_upload([["Название", "Цена"], ["Кунжут 1кг", "850"], ["Лён 500г", "120"]]))
        result = extract_supplier_product_import_session(session_id=session.id)

        self.assertEqual(result.processing_status, "ready")
        session.refresh_from_db()
        self.assertEqual((session.rows_total, session.rows_done), (2, 2))
        items = list(session.items.order_by("row_number"))
        self.assertEqual([item.name for item in items], ["Кунжут 1кг", "Лён 500г"])
        self.assertEqual(items[0].price, Decimal("850"))

    def test_second_run_does_not_duplicate_rows(self):
        from api.v1.suppliers.product_import_selectors import extract_supplier_product_import_session

        session = self._session(_docx_upload([["Название"], ["Кунжут 1кг"]]))
        extract_supplier_product_import_session(session_id=session.id)
        SupplierProductImportSession.objects.filter(pk=session.pk).update(processing_status="processing")
        extract_supplier_product_import_session(session_id=session.id)
        self.assertEqual(session.items.count(), 1)

    def test_file_without_name_column_fails_with_readable_message(self):
        from api.v1.suppliers.product_import_selectors import extract_supplier_product_import_session

        session = self._session(_docx_upload([["Цена"], ["850"]]))
        result = extract_supplier_product_import_session(session_id=session.id)
        self.assertEqual(result.processing_status, "failed")
        self.assertEqual(result.error_message, "Не найдена колонка с названием товара.")
        self.assertEqual(session.items.count(), 0)

    def test_expired_file_fails_with_readable_message(self):
        from api.v1.suppliers.product_import_selectors import extract_supplier_product_import_session

        session = self._session(_docx_upload([["Название"], ["Кунжут"]]), expired=True)
        result = extract_supplier_product_import_session(session_id=session.id)
        self.assertEqual(result.processing_status, "failed")
        self.assertEqual(result.error_message, "Исходный файл недоступен: срок хранения истёк. Загрузите файл ещё раз.")

    def test_session_without_stored_file_fails(self):
        from api.v1.suppliers.product_import_selectors import extract_supplier_product_import_session

        session = SupplierProductImportSession.objects.create(supplier=self.supplier, processing_status="pending")
        result = extract_supplier_product_import_session(session_id=session.id)
        self.assertEqual(result.processing_status, "failed")

    def test_finished_session_is_left_alone(self):
        from api.v1.suppliers.product_import_selectors import extract_supplier_product_import_session

        session = self._session(_docx_upload([["Название"], ["Кунжут"]]))
        SupplierProductImportSession.objects.filter(pk=session.pk).update(processing_status="ready")
        result = extract_supplier_product_import_session(session_id=session.id)
        self.assertEqual(result.processing_status, "ready")
        self.assertEqual(session.items.count(), 0)


class ExtractSupplierProductImportSessionTaskTests(TemporaryMediaRootMixin, TestCase):
    def setUp(self):
        patcher = patch("api.v1.suppliers.tasks.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_task_is_routed_to_queue_consumed_by_the_worker(self):
        from django.conf import settings

        from api.v1.suppliers.tasks import extract_supplier_product_import_session_task

        route = settings.CELERY_TASK_ROUTES[extract_supplier_product_import_session_task.name]
        self.assertEqual(route["queue"], settings.AI_CLEANUP_QUEUE)

    def test_unexpected_extractor_crash_marks_session_failed_and_reraises(self):
        from api.v1.suppliers.tasks import extract_supplier_product_import_session_task

        session = SupplierProductImportSession.objects.create(supplier=_make_supplier(), processing_status="pending")
        with patch(
            "api.v1.suppliers.product_import_selectors.extract_supplier_product_import_session",
            side_effect=RuntimeError("docling crashed"),
        ):
            with self.assertRaises(RuntimeError):
                extract_supplier_product_import_session_task.apply(
                    kwargs={"session_id": str(session.id)}, throw=True
                ).get()
        session.refresh_from_db()
        self.assertEqual(session.processing_status, "failed")
        self.assertEqual(session.error_message, "Не удалось прочитать файл.")


@override_settings(
    IMPORT_FILE_STORAGE_ENABLED=True, SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED=True, SERVICE_API_TOKEN="test-token"
)
class SupplierProductImportAsyncAnalyzeTests(TemporaryMediaRootMixin, APITestCase):
    URL = IMPORT_ANALYZE_URL

    def setUp(self):
        patcher = patch("api.v1.suppliers.tasks.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))

    @patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay")
    @patch("api.v1.suppliers.product_import_selectors.parse_supplier_product_import_file")
    def test_docx_is_not_parsed_in_the_request(self, mock_parse, mock_delay):
        mock_parse.side_effect = AssertionError("файл разобран внутри HTTP-запроса")
        response = self.client.post(
            self.URL, {"file": _docx_upload([["Название"], ["Кунжут 1кг"]])}, format="multipart"
        )

        self.assertEqual(response.status_code, 202, response.content)
        accepted = response.json()
        self.assertEqual(accepted["status"], "pending")
        mock_delay.assert_called_once_with(session_id=accepted["id"])
        session = SupplierProductImportSession.objects.get(pk=accepted["id"])
        self.assertEqual((session.rows_total, session.items.count()), (0, 0))
        self.assertIsNotNone(session.import_file)

    @override_settings(CANONICAL_SUPPLIER_PARSER_ENABLED=False, CANONICAL_IMPORT_LLM_ENABLED=False)
    @patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay")
    @patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay")
    @patch("api.v1.suppliers.product_import_selectors.parse_supplier_product_import_file")
    def test_xlsx_without_llm_is_not_parsed_in_the_request(self, mock_parse, mock_extract_delay, mock_process_delay):
        mock_parse.side_effect = AssertionError("файл разобран внутри HTTP-запроса")
        response = self.client.post(
            self.URL, {"file": _xlsx_upload([["Название"], ["Молоко"], ["Сыр"]])}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        accepted = response.json()
        self.assertEqual(accepted["status"], "pending")
        mock_extract_delay.assert_called_once_with(session_id=accepted["id"])
        mock_process_delay.assert_not_called()
        session = SupplierProductImportSession.objects.get(pk=accepted["id"])
        self.assertEqual((session.rows_total, session.items.count()), (0, 0))
        self.assertEqual(session.processing_status, "pending")
        self.assertIsNotNone(session.import_file)

    @override_settings(CANONICAL_SUPPLIER_PARSER_ENABLED=False, CANONICAL_IMPORT_LLM_ENABLED=False)
    @patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay")
    @patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay")
    def test_csv_without_llm_is_extracted_by_the_task(self, mock_extract_delay, mock_process_delay):
        upload = SimpleUploadedFile("price.csv", "Название\nМолоко\nСыр\n".encode("utf-8"), content_type="text/csv")
        response = self.client.post(self.URL, {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 202, response.content)
        mock_extract_delay.assert_called_once_with(session_id=response.json()["id"])
        mock_process_delay.assert_not_called()

    @override_settings(
        CANONICAL_SUPPLIER_PARSER_ENABLED=False,
        CANONICAL_IMPORT_LLM_ENABLED=False,
        CELERY_TASK_ALWAYS_EAGER=True,
        CELERY_TASK_EAGER_PROPAGATES=True,
    )
    @patch("api.v1.suppliers.product_import_selectors.enrich_supplier_product_import_rows", return_value={})
    def test_end_to_end_eager_xlsx_without_llm_publishes_rows_total_after_extraction(self, _enrich):
        response = self.client.post(
            self.URL, {"file": _xlsx_upload([["Название"], ["Молоко"], ["Сыр"]])}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        polled = self.client.get(response.json()["status_url"]).json()
        self.assertEqual(polled["processing_status"], "ready")
        session = SupplierProductImportSession.objects.get(pk=response.json()["id"])
        self.assertEqual((session.rows_total, session.rows_done), (2, 2))
        self.assertEqual(sorted(item["name"] for item in polled["items"]), ["Молоко", "Сыр"])

    @override_settings(SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED=False)
    @patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay")
    @patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay")
    def test_flag_off_xlsx_is_parsed_in_the_request(self, mock_extract_delay, mock_process_delay):
        response = self.client.post(
            self.URL, {"file": _xlsx_upload([["Название"], ["Молоко"], ["Сыр"]])}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        session = SupplierProductImportSession.objects.get(pk=response.json()["id"])
        self.assertEqual((session.rows_total, session.items.count()), (2, 2))
        mock_process_delay.assert_called_once()
        mock_extract_delay.assert_not_called()

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=False)
    @patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay")
    @patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay")
    def test_without_file_storage_xlsx_is_parsed_in_the_request(self, mock_extract_delay, mock_process_delay):
        response = self.client.post(
            self.URL, {"file": _xlsx_upload([["Название"], ["Молоко"], ["Сыр"]])}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(SupplierProductImportSession.objects.get(pk=response.json()["id"]).items.count(), 2)
        mock_extract_delay.assert_not_called()

    @override_settings(CANONICAL_SUPPLIER_PARSER_ENABLED=True, CANONICAL_IMPORT_LLM_ENABLED=True)
    @patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay")
    @patch("api.v1.suppliers.product_import_selectors.parse_supplier_product_import_file")
    def test_xlsx_with_canonical_llm_is_not_parsed_in_the_request(self, mock_parse, mock_delay):
        mock_parse.side_effect = AssertionError("файл разобран внутри HTTP-запроса")
        response = self.client.post(
            self.URL, {"file": _xlsx_upload([["Название"], ["Молоко"], ["Сыр"]])}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        accepted = response.json()
        self.assertEqual(accepted["status"], "pending")
        mock_delay.assert_called_once_with(session_id=accepted["id"])
        session = SupplierProductImportSession.objects.get(pk=accepted["id"])
        self.assertEqual((session.rows_total, session.items.count()), (0, 0))
        self.assertEqual(session.parser_version, "canonical-products-v2")

    @override_settings(CANONICAL_SUPPLIER_PARSER_ENABLED=True, CANONICAL_IMPORT_LLM_ENABLED=False)
    @patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay")
    @patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay")
    def test_xlsx_with_canonical_without_llm_is_extracted_by_the_task(self, mock_extract_delay, mock_process_delay):
        response = self.client.post(
            self.URL, {"file": _xlsx_upload([["Название"], ["Молоко"], ["Сыр"]])}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        mock_extract_delay.assert_called_once_with(session_id=response.json()["id"])
        mock_process_delay.assert_not_called()

    @override_settings(SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED=False)
    @patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay")
    def test_flag_off_docx_is_parsed_in_the_request(self, _delay):
        response = self.client.post(
            self.URL, {"file": _docx_upload([["Название"], ["Кунжут 1кг"]])}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        session = SupplierProductImportSession.objects.get(pk=response.json()["id"])
        self.assertEqual(session.items.count(), 1)

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=False)
    @patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay")
    def test_without_file_storage_async_extraction_is_not_used(self, _delay):
        response = self.client.post(
            self.URL, {"file": _docx_upload([["Название"], ["Кунжут 1кг"]])}, format="multipart"
        )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(SupplierProductImportSession.objects.get(pk=response.json()["id"]).items.count(), 1)

    @patch(
        "api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay", side_effect=OSError("redis down")
    )
    def test_broker_down_gives_503_and_leaves_no_session(self, _delay):
        response = self.client.post(
            self.URL, {"file": _docx_upload([["Название"], ["Кунжут 1кг"]])}, format="multipart"
        )
        self.assertEqual(response.status_code, 503, response.content)
        self.assertFalse(SupplierProductImportSession.objects.exists())

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    @patch("api.v1.suppliers.product_import_selectors.enrich_supplier_product_import_rows", return_value={})
    def test_end_to_end_eager_docx_becomes_ready_with_rows_in_get(self, _enrich):
        response = self.client.post(
            self.URL,
            {"file": _docx_upload([["Название", "Цена"], ["Кунжут 1кг", "850"]])},
            format="multipart",
        )
        self.assertEqual(response.status_code, 202, response.content)
        polled = self.client.get(response.json()["status_url"]).json()
        self.assertEqual(polled["processing_status"], "ready")
        self.assertEqual([item["name"] for item in polled["items"]], ["Кунжут 1кг"])


class ImportBatchingDefaultsTests(SimpleTestCase):
    def test_defaults_are_chunk_10_workers_16_batch_160(self):
        from api.v1.suppliers import product_import_llm, product_import_selectors

        self.assertEqual(product_import_llm._chunk_size(), 10)
        self.assertEqual(product_import_llm._max_workers(), 16)
        self.assertEqual(product_import_selectors._batch_size(), 160)

    @override_settings(
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_SIZE=30, SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_MAX_WORKERS=8
    )
    def test_batch_follows_chunk_and_workers_when_not_set_explicitly(self):
        from api.v1.suppliers import product_import_selectors

        self.assertEqual(product_import_selectors._batch_size(), 240)

    @override_settings(SUPPLIER_PRODUCT_IMPORT_BATCH_SIZE=50)
    def test_explicit_batch_size_wins(self):
        from api.v1.suppliers import product_import_selectors

        self.assertEqual(product_import_selectors._batch_size(), 50)


class NetFromNameTests(SimpleTestCase):
    def test_single_token_is_returned_with_unit_code(self):
        from api.v1.suppliers.product_import_net import net_from_name

        self.assertEqual(net_from_name("Брынза 250г"), (Decimal("250"), "г"))
        self.assertEqual(net_from_name("Молоко 0,5 л"), (Decimal("0.5"), "л"))
        self.assertEqual(net_from_name("Сыр 1.5кг Россия"), (Decimal("1.5"), "кг"))
        self.assertEqual(net_from_name("Сироп 250 мл"), (Decimal("250"), "мл"))

    def test_word_forms_of_units_are_recognised(self):
        from api.v1.suppliers.product_import_net import net_from_name

        self.assertEqual(net_from_name("Мука 2 килограмма"), (Decimal("2"), "кг"))
        self.assertEqual(net_from_name("Сыр 250 гр"), (Decimal("250"), "г"))

    def test_no_weight_or_ambiguous_name_gives_none(self):
        from api.v1.suppliers.product_import_net import net_from_name

        self.assertIsNone(net_from_name("Лайм"))
        self.assertIsNone(net_from_name("Сыр 200г + соус 50г"))
        # «г» внутри слова и «л» в составе слова не считаются единицей
        self.assertIsNone(net_from_name("Пюре 5 гастрономическое"))

    def test_zero_is_ignored(self):
        from api.v1.suppliers.product_import_net import net_from_name

        self.assertIsNone(net_from_name("Товар 0 кг"))


class FindSupplierProductImportCandidatesTests(TestCase):
    def test_lower_name_index_exists_for_exact_match_branch(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'supplier_products' "
                "AND indexname = 'sp_supplier_lower_name_idx'"
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertIn("lower(supplier_name", row[0])

    def test_exact_match_ranks_first_and_fuzzy_match_is_found(self):
        from api.v1.suppliers.product_import_matching import find_supplier_product_import_candidates

        supplier = _make_supplier()
        exact = SupplierProduct.objects.create(supplier=supplier, supplier_name="Молоко 3.2% 1л", normalized_name="x")
        fuzzy = SupplierProduct.objects.create(supplier=supplier, supplier_name="Молоко 3,2% 1 л", normalized_name="y")
        other_supplier = _make_supplier()
        SupplierProduct.objects.create(supplier=other_supplier, supplier_name="Молоко 3.2% 1л", normalized_name="z")

        result = find_supplier_product_import_candidates(
            supplier_id=supplier.id, names=["молоко 3.2% 1л", "совершенно другой несвязанный текст без общего"]
        )

        self.assertEqual(result[0][0]["id"], exact.id)
        self.assertEqual(result[0][0]["score"], 1.0)
        self.assertIn(fuzzy.id, [c["id"] for c in result[0]])
        self.assertNotIn(1, result)

    def test_archived_products_are_excluded(self):
        from api.v1.suppliers.product_import_matching import find_supplier_product_import_candidates

        supplier = _make_supplier()
        SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Сахар", normalized_name="x", status=EntityStatus.ARCHIVED
        )
        result = find_supplier_product_import_candidates(supplier_id=supplier.id, names=["Сахар"])
        self.assertEqual(result, {})

    def test_empty_names_returns_empty_dict(self):
        from api.v1.suppliers.product_import_matching import find_supplier_product_import_candidates

        supplier = _make_supplier()
        self.assertEqual(find_supplier_product_import_candidates(supplier_id=supplier.id, names=[]), {})


class CountryHintTests(SimpleTestCase):
    def test_explicit_country_words_are_hints(self):
        from api.v1.suppliers.product_import_hints import has_country_hint

        for name in (
            "Манго Тайланд, шт",
            "Сыр Италия 200г",
            "Паста итальянская",
            "Мёд, Россия",
            "Чай Китай 100г",
            "Оливки испанские",
            "Сыр французский",
            "Чай индийский",
        ):
            self.assertTrue(has_country_hint(name), name)

    def test_names_without_country_are_not_hints(self):
        from api.v1.suppliers.product_import_hints import has_country_hint

        for name in (
            "Молоко 3,2% 1л",
            "Угорь Калининградский в желе",
            "Желток (г.Пенза) 10кг",
            "Масло PRESIDENT® 82%",
            "Корейка свиная",
            "Мука пшеничная",
        ):
            self.assertFalse(has_country_hint(name), name)


class EnrichSupplierProductImportRowsTests(TestCase):
    def _enrich(self, mock_connector_cls, name, *, needs_unit=True, needs_country=False, **model_fields):
        from api.v1.suppliers.product_import_contracts import SupplierProductEnrichment, SupplierProductEnrichmentBatch
        from api.v1.suppliers.product_import_llm import enrich_supplier_product_import_rows

        mock_connector_cls.return_value.structured.return_value = SupplierProductEnrichmentBatch(
            items=[SupplierProductEnrichment(row_number=1, **model_fields)]
        )
        return enrich_supplier_product_import_rows(
            rows=[{"row_number": 1, "name": name, "needs_unit": needs_unit, "needs_country": needs_country}]
        )

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True)
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_unit_comes_back_as_code_and_is_mapped_to_id(self, mock_connector_cls):
        piece = Unit.objects.get(code="шт")
        self.assertEqual(self._enrich(mock_connector_cls, "Лайм", unit="шт"), {1: {"unit_id": str(piece.id)}})

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True)
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_unit_code_outside_sale_list_is_dropped(self, mock_connector_cls):
        self.assertEqual(self._enrich(mock_connector_cls, "Лайм", unit="рул"), {})
        self.assertEqual(self._enrich(mock_connector_cls, "Лайм", unit="такой-нет"), {})

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True)
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_payload_has_units_by_code_and_no_uuid_and_per_row_flags(self, mock_connector_cls):
        self._enrich(mock_connector_cls, "Манго Тайланд", needs_unit=False, needs_country=True)
        payload = mock_connector_cls.return_value.structured.call_args.kwargs["payload"]
        self.assertTrue({"шт", "кг", "уп", "бан", "бут"} <= {u["code"] for u in payload["units"]})
        self.assertFalse({u["code"] for u in payload["units"]} & {"рул", "лот", "вед", "пал", "м2"})
        self.assertTrue(all(set(u) == {"code", "name"} for u in payload["units"]))
        self.assertEqual(
            payload["rows"], [{"row_number": 1, "name": "Манго Тайланд", "needs_unit": False, "needs_country": True}]
        )

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True)
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_country_without_hint_in_name_is_dropped(self, mock_connector_cls):
        result = self._enrich(
            mock_connector_cls,
            "Угорь Калининградский",
            needs_unit=False,
            needs_country=True,
            country_of_origin="Россия",
        )
        self.assertEqual(result, {})

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True)
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_country_with_hint_in_name_is_kept_only_when_requested(self, mock_connector_cls):
        kept = self._enrich(
            mock_connector_cls, "Манго Тайланд", needs_unit=False, needs_country=True, country_of_origin="Тайланд"
        )
        self.assertEqual(kept, {1: {"country_of_origin": "Тайланд"}})
        not_requested = self._enrich(
            mock_connector_cls, "Манго Тайланд", needs_unit=True, needs_country=False, country_of_origin="Тайланд"
        )
        self.assertEqual(not_requested, {})

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True)
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_unit_answer_for_row_that_did_not_need_unit_is_ignored(self, mock_connector_cls):
        result = self._enrich(mock_connector_cls, "Лайм", needs_unit=False, needs_country=True, unit="шт")
        self.assertEqual(result, {})

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True)
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_row_without_flags_is_treated_as_requesting_both_fields(self, mock_connector_cls):
        from api.v1.suppliers.product_import_contracts import SupplierProductEnrichment, SupplierProductEnrichmentBatch
        from api.v1.suppliers.product_import_llm import enrich_supplier_product_import_rows

        piece = Unit.objects.get(code="шт")
        mock_connector_cls.return_value.structured.return_value = SupplierProductEnrichmentBatch(
            items=[SupplierProductEnrichment(row_number=1, unit="шт", country_of_origin="Китай")]
        )
        result = enrich_supplier_product_import_rows(rows=[{"row_number": 1, "name": "Чай Китай"}])
        self.assertEqual(result, {1: {"unit_id": str(piece.id), "country_of_origin": "Китай"}})

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True)
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_all_chunks_failing_raises(self, mock_connector_cls):
        from api.v1.suppliers.product_import_llm import (
            SupplierProductEnrichmentError,
            enrich_supplier_product_import_rows,
        )

        mock_connector_cls.return_value.structured.side_effect = LLMConnectorError("down")
        with self.assertRaises(SupplierProductEnrichmentError):
            enrich_supplier_product_import_rows(rows=[{"row_number": 1, "name": "x"}])

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True)
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_text_null_country_is_treated_as_empty(self, mock_connector_cls):
        from api.v1.suppliers.product_import_contracts import SupplierProductEnrichment, SupplierProductEnrichmentBatch
        from api.v1.suppliers.product_import_llm import enrich_supplier_product_import_rows

        mock_connector_cls.return_value.structured.return_value = SupplierProductEnrichmentBatch(
            items=[
                SupplierProductEnrichment(row_number=1, country_of_origin="null"),
                SupplierProductEnrichment(row_number=2, country_of_origin=" Китай "),
            ]
        )
        result = enrich_supplier_product_import_rows(
            rows=[{"row_number": 1, "name": "чай Китай"}, {"row_number": 2, "name": "кофе Китай"}]
        )
        self.assertEqual(result, {2: {"country_of_origin": "Китай"}})

    @override_settings(
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True,
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_SIZE=1,
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_MAX_WORKERS=4,
    )
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_chunks_are_requested_in_parallel(self, mock_connector_cls):
        import threading

        from api.v1.suppliers.product_import_contracts import SupplierProductEnrichment, SupplierProductEnrichmentBatch
        from api.v1.suppliers.product_import_llm import enrich_supplier_product_import_rows

        # Барьер на 3 участника проходит, только если 3 запроса в полёте одновременно.
        barrier = threading.Barrier(3, timeout=5)

        def fake_structured(**kwargs):
            barrier.wait()
            row_number = kwargs["payload"]["rows"][0]["row_number"]
            return SupplierProductEnrichmentBatch(
                items=[SupplierProductEnrichment(row_number=row_number, country_of_origin="Китай")]
            )

        mock_connector_cls.return_value.structured.side_effect = fake_structured
        rows = [{"row_number": n, "name": f"товар {n} Китай"} for n in (1, 2, 3)]
        result = enrich_supplier_product_import_rows(rows=rows)
        self.assertEqual(result, {n: {"country_of_origin": "Китай"} for n in (1, 2, 3)})

    @override_settings(
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True,
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_SIZE=1,
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_MAX_WORKERS=1,
    )
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_one_failed_chunk_does_not_lose_the_others_and_single_worker_works(self, mock_connector_cls):
        from api.v1.suppliers.product_import_contracts import SupplierProductEnrichment, SupplierProductEnrichmentBatch
        from api.v1.suppliers.product_import_llm import enrich_supplier_product_import_rows

        def fake_structured(**kwargs):
            row_number = kwargs["payload"]["rows"][0]["row_number"]
            if row_number == 2:
                raise LLMConnectorError("timeout")
            return SupplierProductEnrichmentBatch(
                items=[SupplierProductEnrichment(row_number=row_number, country_of_origin="Китай")]
            )

        mock_connector_cls.return_value.structured.side_effect = fake_structured
        rows = [{"row_number": n, "name": f"товар {n} Китай"} for n in (1, 2, 3)]
        result = enrich_supplier_product_import_rows(rows=rows)
        self.assertEqual(sorted(result), [1, 3])

    @override_settings(
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=True,
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_SIZE=1,
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_MAX_WORKERS=1,
        SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_RETRIES=1,
    )
    @patch("api.v1.suppliers.product_import_llm.LLMConnector")
    def test_failed_chunk_is_retried_once(self, mock_connector_cls):
        from api.v1.suppliers.product_import_contracts import SupplierProductEnrichment, SupplierProductEnrichmentBatch
        from api.v1.suppliers.product_import_llm import enrich_supplier_product_import_rows

        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMConnectorError("APITimeoutError")
            return SupplierProductEnrichmentBatch(
                items=[SupplierProductEnrichment(row_number=1, country_of_origin="Китай")]
            )

        mock_connector_cls.return_value.structured.side_effect = flaky
        result = enrich_supplier_product_import_rows(rows=[{"row_number": 1, "name": "товар Китай"}])
        self.assertEqual(result, {1: {"country_of_origin": "Китай"}})
        self.assertEqual(calls["n"], 2)

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED=False)
    def test_disabled_by_default_returns_empty(self):
        from api.v1.suppliers.product_import_llm import enrich_supplier_product_import_rows

        self.assertEqual(enrich_supplier_product_import_rows(rows=[{"row_number": 1, "name": "x"}]), {})


class PatchSupplierProductImportItemTests(TestCase):
    def test_editing_name_resets_match_and_reruns_search(self):
        from api.v1.suppliers.product_import_services import patch_supplier_product_import_item

        supplier = _make_supplier()
        target = SupplierProduct.objects.create(supplier=supplier, supplier_name="Сахар белый", normalized_name="x")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            action=SupplierProductImportAction.NEW,
            target_supplier_product=None,
        )

        updated = patch_supplier_product_import_item(item=item, data={"name": "Сахар белый"})

        self.assertEqual(updated.action, SupplierProductImportAction.REVIEW)
        self.assertIsNone(updated.target_supplier_product_id)
        self.assertEqual([c["id"] for c in updated.match_candidates], [str(target.id)])

    def test_manual_edit_clears_llm_source(self):
        from api.v1.suppliers.product_import_services import patch_supplier_product_import_item

        supplier = _make_supplier()
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            country_of_origin="Россия",
            field_sources={"country_of_origin": "llm"},
        )

        updated = patch_supplier_product_import_item(item=item, data={"country_of_origin": "Беларусь"})

        self.assertEqual(updated.field_sources.get("country_of_origin"), "user")

    def test_manual_unit_edit_overwrites_the_llm_key_used_by_enrichment(self):
        """Обогащение пишет ключи "unit"/"net_unit", значит ручная правка должна затирать именно их,
        иначе маркер «Предложено» не исчезает (ТЗ §12)."""
        from api.v1.suppliers.product_import_services import patch_supplier_product_import_item

        supplier = _make_supplier()
        unit = Unit.objects.create(code="kg-src", name="kg-src", kind=UnitKind.WEIGHT)
        net_unit = Unit.objects.create(code="l-src", name="l-src", kind=UnitKind.VOLUME)
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            field_sources={"unit": "llm", "net_value": "llm", "net_unit": "llm"},
        )

        updated = patch_supplier_product_import_item(
            item=item, data={"unit_id": unit.id, "net_value": 2, "net_unit_id": net_unit.id}
        )

        self.assertEqual(updated.field_sources, {"unit": "user", "net_value": "user", "net_unit": "user"})


class SetSupplierProductImportItemActionTests(TestCase):
    def test_replace_accepts_active_target_outside_candidates(self):
        from api.v1.suppliers.product_import_services import set_supplier_product_import_item_action

        supplier = _make_supplier()
        target = SupplierProduct.objects.create(supplier=supplier, supplier_name="Дальний target", normalized_name="x")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(session=session, row_number=1, raw_name="Импорт", name="Импорт")

        updated = set_supplier_product_import_item_action(
            item=item, action=SupplierProductImportAction.REPLACE, target_supplier_product_id=target.id
        )

        self.assertEqual(updated.target_supplier_product_id, target.id)
        self.assertEqual(updated.target_expected_updated_at, target.updated_at)

    def test_replace_rejects_target_from_another_supplier(self):
        from api.v1.suppliers.product_import_services import set_supplier_product_import_item_action

        supplier = _make_supplier()
        stranger = _make_supplier()
        target = SupplierProduct.objects.create(supplier=stranger, supplier_name="Чужой", normalized_name="x")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(session=session, row_number=1, raw_name="Импорт", name="Импорт")

        with self.assertRaises(DjangoValidationError):
            set_supplier_product_import_item_action(
                item=item, action=SupplierProductImportAction.REPLACE, target_supplier_product_id=target.id
            )

    def test_replace_requires_target_from_candidates(self):
        from api.v1.suppliers.product_import_services import set_supplier_product_import_item_action

        supplier = _make_supplier()
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            match_candidates=[
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "name": "x",
                    "unit_id": None,
                    "unit_name": None,
                    "score": 0.9,
                }
            ],
        )
        with self.assertRaises(DjangoValidationError):
            set_supplier_product_import_item_action(
                item=item, action=SupplierProductImportAction.REPLACE, target_supplier_product_id=None
            )

    def test_skip_is_reversible_to_new(self):
        from api.v1.suppliers.product_import_services import set_supplier_product_import_item_action

        supplier = _make_supplier()
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(
            session=session, row_number=1, raw_name="Сахар", name="Сахар", action=SupplierProductImportAction.SKIP
        )
        updated = set_supplier_product_import_item_action(item=item, action=SupplierProductImportAction.NEW)
        self.assertEqual(updated.action, SupplierProductImportAction.NEW)


class BulkSetSupplierProductImportItemsAvailabilityTests(TestCase):
    def test_skip_rows_are_excluded_from_bulk_edit(self):
        from api.v1.suppliers.product_import_services import bulk_set_supplier_product_import_items_availability

        supplier = _make_supplier()
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        editable = SupplierProductImportItem.objects.create(session=session, row_number=1, raw_name="A", name="A")
        skipped = SupplierProductImportItem.objects.create(
            session=session, row_number=2, raw_name="B", name="B", action=SupplierProductImportAction.SKIP
        )

        updated_count = bulk_set_supplier_product_import_items_availability(
            session=session, item_ids=[editable.id, skipped.id], availability=ProductAvailability.OUT_OF_STOCK
        )

        self.assertEqual(updated_count, 1)
        editable.refresh_from_db()
        skipped.refresh_from_db()
        self.assertEqual(editable.availability, ProductAvailability.OUT_OF_STOCK)
        self.assertIsNone(skipped.availability)


class CommitSupplierProductImportSessionTests(TestCase):
    def test_stale_replace_target_is_left_for_review(self):
        from api.v1.suppliers.product_import_services import commit_supplier_product_import_session

        supplier = _make_supplier()
        unit = _make_unit(code="stale-kg")
        target = SupplierProduct.objects.create(supplier=supplier, supplier_name="До", normalized_name="x")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="После",
            name="После",
            unit=unit,
            country_of_origin="Россия",
            price=Decimal("10"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("1"),
            net_unit=unit,
            min_order_qty=Decimal("1"),
            availability=ProductAvailability.IN_STOCK,
            action=SupplierProductImportAction.REPLACE,
            target_supplier_product=target,
            target_expected_updated_at=target.updated_at,
        )
        target.supplier_name = "Изменено после review"
        target.save(update_fields=["supplier_name", "updated_at"])

        result = commit_supplier_product_import_session(session=session)

        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["not_ready_row_numbers"], [1])
        item.refresh_from_db()
        self.assertEqual(item.action, SupplierProductImportAction.REVIEW)
        self.assertEqual(session.status, SupplierProductImportSessionStatus.OPEN)

    def test_second_commit_can_process_rows_left_open(self):
        from api.v1.suppliers.product_import_services import commit_supplier_product_import_session

        supplier = _make_supplier()
        unit = _make_unit(code="repeat-kg")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            unit=unit,
            country_of_origin="Россия",
            price=Decimal("10"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("1"),
            net_unit=unit,
            min_order_qty=Decimal("1"),
            availability=ProductAvailability.IN_STOCK,
            action=SupplierProductImportAction.REVIEW,
        )

        first = commit_supplier_product_import_session(session=session)
        self.assertEqual(first["imported_count"], 0)
        item.action = SupplierProductImportAction.NEW
        item.save(update_fields=["action", "updated_at"])
        second = commit_supplier_product_import_session(session=session)

        self.assertEqual(second["imported_count"], 1)
        self.assertTrue(second["session_deleted"])
        self.assertEqual(second["session_status"], SupplierProductImportSessionStatus.COMMITTED)
        self.assertFalse(SupplierProductImportSession.objects.filter(pk=session.pk).exists())

    def test_commit_removes_imported_and_skipped_rows_but_keeps_review_rows(self):
        from api.v1.suppliers.product_import_services import commit_supplier_product_import_session

        supplier = _make_supplier()
        unit = _make_unit(code="keep-review-kg")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        ready = dict(
            unit=unit,
            country_of_origin="Россия",
            price=Decimal("10"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("1"),
            net_unit=unit,
            min_order_qty=Decimal("1"),
            availability=ProductAvailability.IN_STOCK,
        )
        SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            action=SupplierProductImportAction.NEW,
            **ready,
        )
        SupplierProductImportItem.objects.create(
            session=session, row_number=2, raw_name="Мука", name="Мука", action=SupplierProductImportAction.SKIP
        )
        review = SupplierProductImportItem.objects.create(
            session=session, row_number=3, raw_name="Соль", name="Соль", action=SupplierProductImportAction.REVIEW
        )

        result = commit_supplier_product_import_session(session=session)

        self.assertEqual(result["imported_count"], 1)
        self.assertFalse(result["session_deleted"])
        self.assertEqual(result["session_status"], SupplierProductImportSessionStatus.OPEN)
        self.assertEqual(list(session.items.values_list("pk", flat=True)), [review.pk])
        self.assertTrue(SupplierProduct.objects.filter(supplier=supplier, supplier_name="Сахар").exists())

    def test_commit_of_only_skipped_rows_deletes_session(self):
        from api.v1.suppliers.product_import_services import commit_supplier_product_import_session

        session = SupplierProductImportSession.objects.create(supplier=_make_supplier())
        SupplierProductImportItem.objects.create(
            session=session, row_number=1, raw_name="Мука", name="Мука", action=SupplierProductImportAction.SKIP
        )

        result = commit_supplier_product_import_session(session=session)

        self.assertEqual(result["imported_count"], 0)
        self.assertTrue(result["session_deleted"])
        self.assertFalse(SupplierProductImportSession.objects.filter(pk=session.pk).exists())
        self.assertFalse(SupplierProductImportItem.objects.filter(raw_name="Мука").exists())

    def test_new_row_creates_product_and_replace_row_preserves_id(self):
        from api.v1.suppliers.product_import_services import commit_supplier_product_import_session

        supplier = _make_supplier()
        unit = _make_unit(code="commit-kg")
        existing = SupplierProduct.objects.create(supplier=supplier, supplier_name="Масло 5л", normalized_name="x")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            unit=unit,
            country_of_origin="Россия",
            price=Decimal("100.00"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("1"),
            net_unit=unit,
            min_order_qty=Decimal("1"),
            availability=ProductAvailability.IN_STOCK,
            action=SupplierProductImportAction.NEW,
        )
        SupplierProductImportItem.objects.create(
            session=session,
            row_number=2,
            raw_name="Масло 5л Extra",
            name="Масло 5л Extra Virgin",
            unit=unit,
            country_of_origin="Россия",
            price=Decimal("2000.00"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("5"),
            net_unit=unit,
            min_order_qty=Decimal("1"),
            availability=ProductAvailability.IN_STOCK,
            action=SupplierProductImportAction.REPLACE,
            target_supplier_product=existing,
            target_expected_updated_at=existing.updated_at,
        )
        SupplierProductImportItem.objects.create(
            session=session,
            row_number=3,
            raw_name="Недоделан",
            name="Недоделан",
            action=SupplierProductImportAction.REVIEW,
        )

        result = commit_supplier_product_import_session(session=session, actor=supplier)

        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(result["skipped_review_count"], 1)
        self.assertTrue(SupplierProduct.objects.filter(supplier=supplier, supplier_name="Сахар").exists())
        existing.refresh_from_db()
        self.assertEqual(existing.supplier_name, "Масло 5л Extra Virgin")
        replace_log = AuditLog.objects.get(
            entity_type=AuditLogEntityType.SUPPLIER_PRODUCT,
            entity_id=existing.id,
            action=AuditLogAction.REPLACED,
        )
        self.assertEqual(replace_log.actor_type, AuditActorType.SUPPLIER)
        self.assertEqual(json.loads(replace_log.summary)["event"], "supplier_product_import_replace")
        session.refresh_from_db()
        self.assertEqual(session.status, SupplierProductImportSessionStatus.OPEN)

    def test_new_row_missing_required_field_is_not_ready(self):
        from api.v1.suppliers.product_import_services import commit_supplier_product_import_session

        supplier = _make_supplier()
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        SupplierProductImportItem.objects.create(
            session=session, row_number=1, raw_name="Сахар", name="Сахар", action=SupplierProductImportAction.NEW
        )

        result = commit_supplier_product_import_session(session=session)

        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["not_ready_row_numbers"], [1])
        self.assertFalse(SupplierProduct.objects.filter(supplier=supplier).exists())

    def test_new_row_creates_draft_product_pending_publish(self):
        from api.v1.suppliers.product_import_services import commit_supplier_product_import_session

        supplier = _make_supplier()
        unit = _make_unit(code="draft-kg")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            unit=unit,
            country_of_origin="Россия",
            price=Decimal("10"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("1"),
            net_unit=unit,
            min_order_qty=Decimal("1"),
            availability=ProductAvailability.IN_STOCK,
            action=SupplierProductImportAction.NEW,
        )

        result = commit_supplier_product_import_session(session=session)

        self.assertEqual(result["imported_count"], 1)
        product = SupplierProduct.objects.get(supplier=supplier, supplier_name="Сахар")
        self.assertEqual(product.status, EntityStatus.DRAFT)
        self.assertEqual(result["draft_supplier_product_ids"], [product.id])

    def test_replace_row_does_not_change_target_status(self):
        from api.v1.suppliers.product_import_services import commit_supplier_product_import_session

        supplier = _make_supplier()
        unit = _make_unit(code="replace-status-kg")
        existing = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Масло", normalized_name="x", status=EntityStatus.ACTIVE
        )
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Масло Extra",
            name="Масло Extra Virgin",
            unit=unit,
            country_of_origin="Россия",
            price=Decimal("100"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("1"),
            net_unit=unit,
            min_order_qty=Decimal("1"),
            availability=ProductAvailability.IN_STOCK,
            action=SupplierProductImportAction.REPLACE,
            target_supplier_product=existing,
            target_expected_updated_at=existing.updated_at,
        )

        result = commit_supplier_product_import_session(session=session)

        existing.refresh_from_db()
        self.assertEqual(existing.status, EntityStatus.ACTIVE)
        self.assertEqual(result["draft_supplier_product_ids"], [])


@override_settings(SERVICE_API_TOKEN="test-token")
@override_settings(SUPPLIER_PRODUCT_IMPORT_BATCH_SIZE=2)
class SupplierProductImportBackgroundProcessingTests(APITestCase):
    """analyze всегда отвечает 202 (pending), обработка идёт в Celery: pending -> processing -> ready | failed,
    готовые строки появляются в GET сессии по ходу (TASK-132 + запрос фронта 2026-10-01)."""

    def test_task_is_routed_to_queue_consumed_by_the_worker(self):
        """Без маршрута задача уходит в очередь celery, которую не читает ни один воркер (стенд: -Q ai_normalization),
        и сессия навсегда остаётся в processing."""
        from django.conf import settings

        from api.v1.suppliers.tasks import process_supplier_product_import_session_task

        route = settings.CELERY_TASK_ROUTES[process_supplier_product_import_session_task.name]
        self.assertEqual(route["queue"], settings.AI_CLEANUP_QUEUE)

    def test_every_project_task_has_a_route(self):
        """Любая задача проекта без маршрута попадает в очередь celery, которую никто не читает."""
        from django.conf import settings

        from config.celery import app

        app.loader.import_default_modules()
        names = [
            name for name in app.tasks if name.startswith(("suppliers.", "catalog.", "notifications.", "file_imports."))
        ]
        self.assertTrue(names)
        missing = sorted(name for name in names if name not in settings.CELERY_TASK_ROUTES)
        self.assertEqual(missing, [])

    def test_only_rows_missing_unit_or_with_country_hint_go_to_the_model(self):
        from api.v1.suppliers import product_import_selectors as selectors

        supplier = _make_supplier()
        unit = Unit.objects.get(code="шт")
        session = SupplierProductImportSession.objects.create(supplier=supplier, rows_total=4)
        for number, (name, unit_id) in enumerate(
            [("Лайм", None), ("Молоко 1л", unit.id), ("Манго Тайланд", unit.id), ("Сыр Италия", None)], start=1
        ):
            SupplierProductImportItem.objects.create(
                session=session, row_number=number, raw_name=name, name=name, unit_id=unit_id
            )
        with patch.object(selectors, "enrich_supplier_product_import_rows", return_value={}) as mock_enrich:
            selectors._enrich_and_match_items(supplier=supplier, items=list(session.items.order_by("row_number")))
        sent = {row["row_number"]: row for row in mock_enrich.call_args.kwargs["rows"]}
        self.assertEqual(set(sent), {1, 3, 4})  # «Молоко 1л»: единица есть, подсказки страны нет -- в модель не идёт
        self.assertEqual((sent[1]["needs_unit"], sent[1]["needs_country"]), (True, False))
        self.assertEqual((sent[3]["needs_unit"], sent[3]["needs_country"]), (False, True))
        self.assertEqual((sent[4]["needs_unit"], sent[4]["needs_country"]), (True, True))

    URL = IMPORT_ANALYZE_URL

    def setUp(self):
        # close_old_connections() внутри задачи закрывает тестовое соединение -- как в test_tasks.py.
        patcher = patch("api.v1.suppliers.tasks.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _upload_big(self):
        return _xlsx_upload([["Название"], ["Молоко 3.2% 1л"], ["Сыр"], ["Мука"], ["Соль"], ["Сахар"]])

    def _authorize(self, supplier):
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

    def _make_session(self, supplier, rows=5, *, processing_status="pending", rows_done=0):
        session = SupplierProductImportSession.objects.create(
            supplier=supplier, rows_total=rows, rows_done=rows_done, processing_status=processing_status
        )
        for number in range(1, rows + 1):
            SupplierProductImportItem.objects.create(
                session=session, row_number=number * 10, raw_name=f"Товар {number}", name=f"Товар {number}"
            )
        return session

    @patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay")
    def test_analyze_always_returns_202_pending_even_for_a_small_file(self, mock_delay):
        supplier = _make_supplier()
        self._authorize(supplier)
        response = self.client.post(
            self.URL, {"file": _xlsx_upload([["Название"], ["Молоко"], ["Сыр"]])}, format="multipart"
        )

        self.assertEqual(response.status_code, 202, response.content)
        accepted = response.json()
        self.assertEqual(set(accepted), {"id", "session_id", "status", "status_url"})
        self.assertEqual(accepted["status"], "pending")
        self.assertEqual(accepted["id"], accepted["session_id"])
        self.assertEqual(accepted["status_url"], f"/api/v1/suppliers/products/import/{accepted['id']}")
        mock_delay.assert_called_once_with(session_id=accepted["session_id"])

        polled = self.client.get(accepted["status_url"]).json()
        self.assertEqual(polled["processing_status"], "pending")
        self.assertEqual((polled["rows_total"], polled["rows_done"], polled["percent"]), (2, 0, 0))
        self.assertEqual(polled["items"], [])
        self.assertEqual(polled["counts"]["all"], 0)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    def test_task_enriches_matches_and_marks_ready(self):
        supplier = _make_supplier()
        self._authorize(supplier)
        SupplierProduct.objects.create(supplier=supplier, supplier_name="Молоко 3.2% 1л", normalized_name="x")

        response = self.client.post(self.URL, {"file": self._upload_big()}, format="multipart")
        self.assertEqual(response.status_code, 202, response.content)

        polled = self.client.get(response.json()["status_url"]).json()
        self.assertEqual(polled["processing_status"], "ready")
        self.assertEqual((polled["rows_done"], polled["percent"]), (5, 100))
        self.assertEqual(polled["counts"]["all"], 5)
        actions = {item["name"]: item["action"] for item in polled["items"]}
        self.assertEqual(actions["Молоко 3.2% 1л"], "replace")
        self.assertEqual(actions["Сыр"], "new")

    def test_status_goes_pending_processing_ready(self):
        from api.v1.suppliers import product_import_selectors as selectors

        supplier = _make_supplier()
        session = self._make_session(supplier, rows=3)
        seen = []
        real = selectors._enrich_and_match_items

        def spy(**kwargs):
            seen.append(SupplierProductImportSession.objects.get(pk=session.pk).processing_status)
            return real(**kwargs)

        with patch.object(selectors, "_enrich_and_match_items", side_effect=spy):
            selectors.process_supplier_product_import_session(session_id=session.id)

        self.assertEqual(seen, ["processing", "processing"])
        session.refresh_from_db()
        self.assertEqual((session.processing_status, session.rows_done), ("ready", 3))

    def test_ready_rows_appear_in_get_while_processing_batch_by_batch(self):
        from api.v1.suppliers import product_import_selectors as selectors

        supplier = _make_supplier()
        self._authorize(supplier)
        session = self._make_session(supplier, rows=5)
        url = f"/api/v1/suppliers/products/import/{session.id}"
        snapshots = []
        real = selectors._enrich_and_match_items

        def spy(**kwargs):
            body = self.client.get(url).json()
            snapshots.append((body["processing_status"], body["rows_done"], [i["row_number"] for i in body["items"]]))
            return real(**kwargs)

        with patch.object(selectors, "_enrich_and_match_items", side_effect=spy):
            selectors.process_supplier_product_import_session(session_id=session.id)

        # пачка = 2 строки: перед 1-й пачкой готовых строк нет, перед 2-й -- две, перед 3-й -- четыре
        self.assertEqual(
            snapshots,
            [("processing", 0, []), ("processing", 2, [10, 20]), ("processing", 4, [10, 20, 30, 40])],
        )
        final = self.client.get(url).json()
        self.assertEqual(final["processing_status"], "ready")
        self.assertEqual([i["row_number"] for i in final["items"]], [10, 20, 30, 40, 50])

    def test_counts_cover_all_ready_rows_not_only_the_returned_page(self):
        supplier = _make_supplier()
        self._authorize(supplier)
        session = self._make_session(supplier, rows=5, processing_status="processing", rows_done=4)
        url = f"/api/v1/suppliers/products/import/{session.id}"

        full = self.client.get(url).json()
        self.assertEqual([i["row_number"] for i in full["items"]], [10, 20, 30, 40])
        self.assertEqual(full["counts"]["all"], 4)

        delta = self.client.get(url, {"after_row_number": 20}).json()
        self.assertEqual([i["row_number"] for i in delta["items"]], [30, 40])
        self.assertEqual(delta["counts"]["all"], 4)

        light = self.client.get(url, {"include_items": "false"}).json()
        self.assertEqual(light["items"], [])
        self.assertEqual((light["counts"]["all"], light["rows_done"], light["processing_status"]), (4, 4, "processing"))

    def test_get_query_parameters_are_validated(self):
        supplier = _make_supplier()
        self._authorize(supplier)
        session = self._make_session(supplier, rows=1, processing_status="ready", rows_done=1)
        url = f"/api/v1/suppliers/products/import/{session.id}"

        self.assertEqual(self.client.get(url, {"after_row_number": "-1"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"after_row_number": "abc"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"include_items": "maybe"}).status_code, 400)

    def test_failed_session_returns_no_items_and_error_message(self):
        supplier = _make_supplier()
        self._authorize(supplier)
        session = self._make_session(supplier, rows=2, processing_status="failed", rows_done=2)
        SupplierProductImportSession.objects.filter(pk=session.pk).update(error_message="boom")

        body = self.client.get(f"/api/v1/suppliers/products/import/{session.id}").json()
        self.assertEqual((body["processing_status"], body["error_message"], body["items"]), ("failed", "boom", []))

    def test_edits_and_commit_are_rejected_while_pending_processing_or_failed(self):
        supplier = _make_supplier()
        self._authorize(supplier)
        for processing_status in ("pending", "processing"):
            session = self._make_session(supplier, rows=1, processing_status=processing_status)
            item = session.items.first()
            base = f"/api/v1/suppliers/products/import/{session.id}"
            calls = [
                self.client.patch(f"{base}/items/{item.id}", {"price": "10"}, format="json"),
                self.client.put(f"{base}/items/{item.id}/action", {"action": "skip"}, format="json"),
                self.client.patch(
                    f"{base}/items/bulk", {"item_ids": [str(item.id)], "availability": "in_stock"}, format="json"
                ),
                self.client.post(f"{base}/commit"),
            ]
            for call in calls:
                self.assertEqual(call.status_code, 409, (processing_status, call.content))
                self.assertEqual(call.json(), {"detail": "Импорт ещё обрабатывается."})

        SupplierProductImportSession.objects.filter(pk=session.pk).update(processing_status="failed", error_message="x")
        failed = self.client.post(f"/api/v1/suppliers/products/import/{session.id}/commit")
        self.assertEqual(failed.status_code, 409)
        self.assertEqual(failed.json(), {"detail": "Обработка импорта завершилась ошибкой."})

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
    @patch("api.v1.suppliers.product_import_selectors._enrich_and_match_items", side_effect=RuntimeError("boom"))
    def test_task_failure_marks_session_failed_with_message(self, _mock_enrich):
        supplier = _make_supplier()
        self._authorize(supplier)
        response = self.client.post(self.URL, {"file": self._upload_big()}, format="multipart")
        self.assertEqual(response.status_code, 202, response.content)

        polled = self.client.get(response.json()["status_url"]).json()
        self.assertEqual(polled["processing_status"], "failed")
        self.assertEqual(polled["error_message"], "boom")
        self.assertEqual(polled["items"], [])

    @patch(
        "api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay", side_effect=OSError("broker down")
    )
    @patch(
        "api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay", side_effect=OSError("broker down")
    )
    def test_unreachable_broker_returns_503_and_leaves_no_session(self, _mock_delay, _mock_extract_delay):
        supplier = _make_supplier()
        self._authorize(supplier)
        response = self.client.post(self.URL, {"file": self._upload_big()}, format="multipart")

        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(
            response.json(),
            {"detail": "Не удалось поставить обработку импорта в очередь. Загрузите файл ещё раз чуть позже."},
        )
        # Обработка не началась: сессию с разобранными строками в списке «Ошибка обработки» не оставляем.
        self.assertFalse(SupplierProductImportSession.objects.filter(supplier=supplier).exists())


class SupplierProductImportTextPriceTests(APITestCase):
    """TASK-127 (минимальный вариант): текст вместо цены -> статус наличия, цена пустая."""

    def _analyze(self, rows):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        _accepted, response = _analyze_ready(self.client, _xlsx_upload(rows))
        self.assertEqual(response.status_code, 200, response.content)
        return {row["name"]: row for row in response.json()["items"]}

    def test_text_in_price_column_becomes_availability(self):
        rows = self._analyze(
            [
                ["Название", "Цена"],
                ["Товар под заказ", "под заказ"],
                ["Товара нет", " Нет "],
                ["Товар по запросу", "по запросу"],
                ["Товар с ценой", "125,50"],
            ]
        )
        self.assertEqual(rows["Товар под заказ"]["availability"], "on_order")
        self.assertIsNone(rows["Товар под заказ"]["price"])
        self.assertEqual(rows["Товара нет"]["availability"], "out_of_stock")
        self.assertIsNone(rows["Товара нет"]["price"])
        self.assertIsNone(rows["Товар по запросу"]["availability"])
        self.assertIsNone(rows["Товар по запросу"]["price"])
        self.assertIsNone(rows["Товар с ценой"]["availability"])
        self.assertEqual(rows["Товар с ценой"]["price"], "125.50")

    def test_explicit_availability_column_wins_over_text_price(self):
        rows = self._analyze([["Название", "Цена", "Наличие"], ["Товар", "нет", "в наличии"]])
        self.assertEqual(rows["Товар"]["availability"], "in_stock")
        self.assertIsNone(rows["Товар"]["price"])

    def test_short_no_in_availability_column_is_recognised(self):
        rows = self._analyze([["Название", "Цена", "Наличие"], ["Товар", "10", "Нет"]])
        self.assertEqual(rows["Товар"]["availability"], "out_of_stock")

    def test_other_price_text_is_not_mistaken_for_availability(self):
        rows = self._analyze([["Название", "Цена"], ["Товар", "нет данных"], ["Другой", "заказ по телефону"]])
        self.assertIsNone(rows["Товар"]["availability"])
        self.assertIsNone(rows["Другой"]["availability"])


class SupplierProductImportSkuMatchAndIdempotencyTests(APITestCase):
    """TASK-133 пп. 2-3: match по supplier_sku до fuzzy по названию; повторная загрузка того же файла."""

    def _ready(self, upload):
        """analyze -> обработка -> GET сессии (готовые строки)."""
        _accepted, response = _analyze_ready(self.client, upload)
        self.assertEqual(response.status_code, 200, response.content)
        return response

    @staticmethod
    def _same_bytes_upload(rows, name="products.xlsx"):
        # openpyxl кладёт в файл время создания -- два _xlsx_upload одних строк дают разные sha256.
        original = _xlsx_upload(rows, name=name)
        content = original.read()
        return lambda: SimpleUploadedFile(name, content, content_type=original.content_type)

    def test_known_sku_gives_replace_with_target_even_if_name_differs(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        product = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Сыр Хохланд 380г", normalized_name="x", supplier_sku="A-100"
        )

        response = self._ready(_xlsx_upload([["Название", "Артикул"], ["Сыр Hochland 380 гр плавленый", "A-100"]]))

        item = response.json()["items"][0]
        self.assertEqual(item["action"], "replace")
        self.assertEqual(item["target_supplier_product_id"], str(product.id))
        self.assertIsNotNone(item["target_expected_updated_at"])
        self.assertEqual([c["id"] for c in item["match_candidates"]], [str(product.id)])
        self.assertEqual(item["match_candidates"][0]["score"], 1.0)
        self.assertEqual(response.json()["counts"]["replace"], 1)

    def test_unknown_or_missing_sku_keeps_name_matching(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        existing = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Молоко 3.2% 1л", normalized_name="x", supplier_sku="A-100"
        )

        response = self._ready(
            _xlsx_upload(
                [
                    ["Название", "Артикул"],
                    ["Молоко 3.2% 1л", "ZZZ-999"],
                    ["Совсем другой несвязанный текст", None],
                ]
            )
        )

        rows = {row["name"]: row for row in response.json()["items"]}
        # The unknown SKU matches nothing, so the row falls back to the name; an exact name is a replace (b0e64339).
        self.assertEqual(rows["Молоко 3.2% 1л"]["action"], "replace")
        self.assertEqual(rows["Молоко 3.2% 1л"]["target_supplier_product_id"], str(existing.id))
        self.assertEqual(rows["Совсем другой несвязанный текст"]["action"], "new")

    def test_sku_of_other_supplier_or_archived_product_does_not_match(self):
        supplier = _make_supplier()
        other = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        SupplierProduct.objects.create(
            supplier=other, supplier_name="Чужой товар", normalized_name="x", supplier_sku="A-100"
        )
        SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Архивный товар",
            normalized_name="x",
            supplier_sku="B-200",
            status=EntityStatus.ARCHIVED,
        )

        response = self._ready(
            _xlsx_upload([["Название", "Артикул"], ["Первая позиция", "A-100"], ["Вторая", "B-200"]])
        )

        foreign, archived = sorted(response.json()["items"], key=lambda row: row["row_number"])
        self.assertEqual(foreign["action"], "new")
        # Audit F6: the archived card still holds B-200, so a new card would fail on apply -- say so up front.
        self.assertEqual(archived["action"], "review")
        self.assertIsNone(archived["target_supplier_product_id"])
        self.assertEqual(
            archived["validation_errors"],
            [
                "Артикул B-200 занят товаром «Архивный товар» в архиве. Восстановите товар из архива или исправьте артикул."
            ],
        )

    def test_duplicate_sku_in_file_matches_only_first_row(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        product = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Сахар песок", normalized_name="x", supplier_sku="A-100"
        )

        response = self._ready(_xlsx_upload([["Название", "Артикул"], ["Сахар 1кг", "A-100"], ["Сахар 5кг", "A-100"]]))

        items = response.json()["items"]
        self.assertEqual([i["action"] for i in items].count("replace"), 1)
        replaced = next(i for i in items if i["action"] == "replace")
        self.assertEqual(replaced["row_number"], items[0]["row_number"])
        self.assertEqual(replaced["target_supplier_product_id"], str(product.id))

    def test_same_file_returns_existing_open_session(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        make_upload = self._same_bytes_upload([["Название"], ["Новый товар один"], ["Новый товар два"]])

        first = _analyze_queued(self.client, make_upload())
        second = _analyze_queued(self.client, make_upload())

        self.assertEqual((first.status_code, second.status_code), (202, 202), (first.content, second.content))
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(second.json()["status"], "pending")
        self.assertEqual(SupplierProductImportSession.objects.filter(supplier=supplier).count(), 1)
        self.assertIsNotNone(SupplierProductImportSession.objects.get().content_sha256)

    def test_same_file_keeps_user_edits_of_existing_session(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        make_upload = self._same_bytes_upload([["Название"], ["Новый товар"]])
        first_accepted, first_response = _analyze_ready(self.client, make_upload())
        first = first_response.json()
        self.client.patch(
            f"/api/v1/suppliers/products/import/{first['id']}/items/{first['items'][0]['id']}",
            {"country_of_origin": "Россия"},
            format="json",
        )

        second_accepted = _analyze_queued(self.client, make_upload())

        self.assertEqual(second_accepted.status_code, 202, second_accepted.content)
        self.assertEqual(second_accepted.json()["id"], first_accepted.json()["id"])
        self.assertEqual(second_accepted.json()["status"], "ready")
        second = self.client.get(second_accepted.json()["status_url"]).json()
        self.assertEqual(second["items"][0]["country_of_origin"], "Россия")

    def test_changed_file_creates_new_session(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))

        first = _analyze_queued(self.client, _xlsx_upload([["Название"], ["Товар один"]]))
        second = _analyze_queued(self.client, _xlsx_upload([["Название"], ["Товар один"], ["Товар два"]]))

        self.assertEqual(second.status_code, 202, second.content)
        self.assertNotEqual(first.json()["id"], second.json()["id"])

    def test_same_file_of_other_supplier_creates_own_session(self):
        first_supplier = _make_supplier()
        second_supplier = _make_supplier()
        make_upload = self._same_bytes_upload([["Название"], ["Товар один"]])

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(first_supplier))
        first = _analyze_queued(self.client, make_upload())
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(second_supplier))
        second = _analyze_queued(self.client, make_upload())

        self.assertEqual(second.status_code, 202, second.content)
        self.assertNotEqual(first.json()["id"], second.json()["id"])

    def test_same_file_after_commit_or_failure_creates_new_session(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        make_upload = self._same_bytes_upload([["Название"], ["Товар один"]])

        first = _analyze_queued(self.client, make_upload())
        SupplierProductImportSession.objects.filter(pk=first.json()["id"]).update(
            status=SupplierProductImportSessionStatus.COMMITTED
        )
        second = _analyze_queued(self.client, make_upload())
        SupplierProductImportSession.objects.filter(pk=second.json()["id"]).update(
            processing_status=SupplierProductImportProcessingStatus.FAILED
        )
        third = _analyze_queued(self.client, make_upload())

        self.assertEqual({first.status_code, second.status_code, third.status_code}, {202})
        self.assertEqual(len({first.json()["id"], second.json()["id"], third.json()["id"]}), 3)

    def test_same_file_while_processing_returns_same_session_with_current_status(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        make_upload = self._same_bytes_upload([["Название"], ["Товар один"], ["Товар два"]])

        first = _analyze_queued(self.client, make_upload())
        SupplierProductImportSession.objects.filter(pk=first.json()["id"]).update(
            processing_status=SupplierProductImportProcessingStatus.PROCESSING
        )
        second = _analyze_queued(self.client, make_upload())

        self.assertEqual(second.status_code, 202, second.content)
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(second.json()["status"], "processing")
        self.assertEqual(SupplierProductImportSession.objects.filter(supplier=supplier).count(), 1)


class SupplierProductImportAPITests(APITestCase):
    """End-to-end coverage of the 6 supplier product import endpoints together --
    they share one session lifecycle, so one connected flow per concern reads
    clearer than isolating every endpoint into its own TestCase."""

    def test_analyze_creates_session_and_flags_duplicate(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        existing = SupplierProduct.objects.create(
            supplier=supplier, supplier_name="Молоко 3.2% 1л", normalized_name="x"
        )

        upload = _xlsx_upload([["Название"], ["Молоко 3.2% 1л"], ["Совсем другой несвязанный текст"]])
        _accepted, response = _analyze_ready(self.client, upload)

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["counts"]["all"], 2)
        # An exact name match with exactly one product of the supplier is a replace (b0e64339), not a review.
        self.assertEqual(payload["counts"]["replace"], 1)
        self.assertEqual(payload["counts"]["review"], 0)
        self.assertEqual(payload["counts"]["new"], 1)
        rows_by_name = {row["name"]: row for row in payload["items"]}
        self.assertEqual(rows_by_name["Молоко 3.2% 1л"]["action"], "replace")
        self.assertEqual(rows_by_name["Молоко 3.2% 1л"]["target_supplier_product_id"], str(existing.id))
        self.assertEqual(rows_by_name["Совсем другой несвязанный текст"]["action"], "new")

    def test_get_denied_for_other_suppliers_session(self):
        owner = _make_supplier()
        stranger = _make_supplier()
        session = SupplierProductImportSession.objects.create(supplier=owner)

        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(stranger))
        response = self.client.get(f"/api/v1/suppliers/products/import/{session.id}")
        self.assertEqual(response.status_code, 404)

    def test_full_review_and_commit_flow(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        unit = _make_unit(code="flow-kg")

        upload = _xlsx_upload([["Название"], ["Новый товар без дублей"]])
        accepted, analyze_response = _analyze_ready(self.client, upload)
        self.assertEqual(analyze_response.status_code, 200, analyze_response.content)
        session_id = accepted.json()["id"]
        item_id = analyze_response.json()["items"][0]["id"]

        patch_response = self.client.patch(
            f"/api/v1/suppliers/products/import/{session_id}/items/{item_id}",
            {
                "country_of_origin": "Россия",
                "unit_id": str(unit.id),
                "price": "10.00",
                "delivery_multiple": "1",
                "net_value": "1",
                "net_unit_id": str(unit.id),
                "min_order_qty": "1",
                "availability": "in_stock",
            },
            format="json",
        )
        self.assertEqual(patch_response.status_code, 200, patch_response.content)

        bulk_response = self.client.patch(
            f"/api/v1/suppliers/products/import/{session_id}/items/bulk",
            {"item_ids": [item_id], "availability": "on_order"},
            format="json",
        )
        self.assertEqual(bulk_response.status_code, 200, bulk_response.content)
        self.assertEqual(bulk_response.json()["updated_count"], 1)

        with patch("api.v1.suppliers.views.product_import.notify_linked_supplier_chats") as notify:
            commit_response = self.client.post(f"/api/v1/suppliers/products/import/{session_id}/commit")
        self.assertEqual(commit_response.status_code, 200, commit_response.content)
        self.assertEqual(commit_response.json()["imported_count"], 1)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["supplier"], supplier)
        self.assertIn("Импорт товаров с сайта завершён", notify.call_args.kwargs["text"])
        created_product = SupplierProduct.objects.get(supplier=supplier, supplier_name="Новый товар без дублей")
        self.assertEqual(created_product.status, EntityStatus.DRAFT)
        self.assertEqual(commit_response.json()["draft_supplier_product_ids"], [str(created_product.id)])

        self.assertTrue(commit_response.json()["session_deleted"])
        self.assertFalse(SupplierProductImportSession.objects.filter(pk=session_id).exists())

        for method, url in (
            (self.client.get, f"/api/v1/suppliers/products/import/{session_id}"),
            (self.client.post, f"/api/v1/suppliers/products/import/{session_id}/commit"),
            (self.client.patch, f"/api/v1/suppliers/products/import/{session_id}/items/{item_id}"),
        ):
            self.assertEqual(method(url).status_code, 404, url)

    def test_action_replace_without_target_is_rejected(self):
        supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(supplier))
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = SupplierProductImportItem.objects.create(session=session, row_number=1, raw_name="Сахар", name="Сахар")

        response = self.client.put(
            f"/api/v1/suppliers/products/import/{session.id}/items/{item.id}/action",
            {"action": "replace"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
