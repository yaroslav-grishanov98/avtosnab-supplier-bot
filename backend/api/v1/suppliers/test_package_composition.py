"""TASK-111: «Состав упаковки» товара поставщика — «8 шт. × 2 кг = 16 кг/кор.»
(макет карточки товара в каталоге, 2026-09-28)."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from api.v1.suppliers.normalization import UNIT_ALIASES_MAP, normalize_unit_code
from api.v1.suppliers.package_composition import format_quantity, parse_package_composition
from api.v1.suppliers.tests import TemporaryMediaRootMixin
from core.models import EntityStatus, Unit
from suppliers.models import (
    PriceStatus,
    SourceChannel,
    Supplier,
    SupplierPriceUpload,
    SupplierProduct,
    SupplierProductPrice,
)

_ALIASES = {**UNIT_ALIASES_MAP, "кор": "кор", "коробка": "кор", "л": "л"}


def _parse(text):
    return parse_package_composition(text, unit_aliases=_ALIASES, normalize_unit_code=normalize_unit_code)


def _unit(code):
    return Unit.objects.get(code=code)


class PackageCompositionParseTests(SimpleTestCase):
    def test_parses_mockup_string(self):
        self.assertEqual(
            _parse("8 шт. × 2 кг = 16 кг/кор."),
            {
                "pack_items_count": 8,
                "pack_item_value": Decimal("2"),
                "pack_item_unit_code": "кг",
                "pack_type_code": "кор",
            },
        )

    def test_parses_decimal_comma_and_ascii_x_without_pack_type(self):
        parsed = _parse("6x0,75 л")
        self.assertEqual(parsed["pack_items_count"], 6)
        self.assertEqual(parsed["pack_item_value"], Decimal("0.75"))
        self.assertEqual(parsed["pack_item_unit_code"], "л")
        self.assertIsNone(parsed["pack_type_code"])

    def test_unparseable_or_unknown_unit_returns_none(self):
        self.assertIsNone(_parse("коробка большая"))
        self.assertIsNone(_parse("8 × 2 попугая"))
        self.assertIsNone(_parse("0 × 2 кг"))

    def test_format_quantity(self):
        self.assertEqual(format_quantity(Decimal("0.7500")), "0,75")
        self.assertEqual(format_quantity(Decimal("16.0000")), "16")


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierProductPackageCompositionAPITests(APITestCase):
    url = "/api/v1/suppliers/products"

    def setUp(self):
        self.supplier = Supplier.objects.create(legal_name="GFC LLC", display_name="GFC")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _create(self, **extra):
        return self.client.post(
            self.url,
            {"supplier_id": str(self.supplier.id), "supplier_raw_name": "Курица грудка 2кг", **extra},
            format="json",
        )

    def test_create_returns_composition_with_display(self):
        response = self._create(
            pack_items_count=8,
            pack_item_value="2",
            pack_item_unit_id=str(_unit("кг").id),
            pack_type_id=str(_unit("кор").id),
        )
        self.assertEqual(response.status_code, 201, response.json())
        composition = response.json()["package_composition"]
        self.assertEqual(composition["display"], "8 шт. × 2 кг = 16 кг/кор.")
        self.assertEqual(composition["items_count"], 8)
        self.assertEqual(Decimal(composition["total_value"]), Decimal("16"))
        self.assertEqual(composition["item_unit"], "кг")
        self.assertEqual(composition["pack_type"], "кор")

    def test_composition_is_null_when_not_filled(self):
        response = self._create()
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()["package_composition"])

    def test_partial_composition_rejected(self):
        response = self._create(pack_items_count=8)
        self.assertEqual(response.status_code, 400)
        self.assertIn("package_composition", response.json())

    def test_pack_type_must_be_package_unit(self):
        response = self._create(
            pack_items_count=8,
            pack_item_value="2",
            pack_item_unit_id=str(_unit("кг").id),
            pack_type_id=str(_unit("кг").id),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("pack_type_id", response.json())

    def test_patch_updates_and_clears_composition(self):
        product_id = self._create(
            pack_items_count=8, pack_item_value="2", pack_item_unit_id=str(_unit("кг").id)
        ).json()["id"]

        response = self.client.patch(f"{self.url}/{product_id}", {"pack_items_count": 6}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["package_composition"]["display"], "6 шт. × 2 кг = 12 кг")

        response = self.client.patch(f"{self.url}/{product_id}", {"pack_items_count": None}, format="json")
        self.assertEqual(response.status_code, 400)

        response = self.client.patch(
            f"{self.url}/{product_id}",
            {"pack_items_count": None, "pack_item_value": None, "pack_item_unit_id": None},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["package_composition"])

    def test_list_includes_composition(self):
        self._create(pack_items_count=8, pack_item_value="2", pack_item_unit_id=str(_unit("кг").id))
        response = self.client.get(self.url, {"supplier_id": str(self.supplier.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["package_composition"]["display"], "8 шт. × 2 кг = 16 кг")


@override_settings(SERVICE_API_TOKEN="test-token")
class PriceUploadPackageCompositionTests(TemporaryMediaRootMixin, APITestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(legal_name="GFC LLC", display_name="GFC")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def _import(self, content):
        response = self.client.post(
            "/api/v1/suppliers/uploads",
            {
                "supplier_id": str(self.supplier.id),
                "source_channel": SourceChannel.TELEGRAM,
                "source_file": SimpleUploadedFile("price.csv", content.encode("utf-8"), content_type="text/csv"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        upload = SupplierPriceUpload.objects.get(id=response.json()["id"])
        response = self.client.post(
            f"/api/v1/suppliers/uploads/{upload.id}/normalize", {"use_ai": False, "run_async": False}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.json())

    def test_import_fills_composition_from_column(self):
        self._import("Наименование,Цена,Ед,Состав упаковки\nКурица грудка филе,560,кг,8 шт. × 2 кг = 16 кг/кор.\n")
        product = SupplierProduct.objects.get(supplier=self.supplier)
        self.assertEqual(product.pack_items_count, 8)
        self.assertEqual(product.pack_item_value, Decimal("2"))
        self.assertEqual(product.pack_item_unit.code, "кг")
        self.assertEqual(product.pack_type.code, "кор")

    def test_reimport_without_column_keeps_manual_composition(self):
        self._import("Наименование,Цена,Ед,Состав упаковки\nКурица грудка филе,560,кг,8 шт. × 2 кг = 16 кг/кор.\n")
        self._import("Наименование,Цена,Ед\nКурица грудка филе,570,кг\n")
        product = SupplierProduct.objects.get(supplier=self.supplier)
        self.assertEqual(product.current_price, Decimal("570.00"))
        self.assertEqual(product.pack_items_count, 8)
        self.assertEqual(product.pack_type.code, "кор")

    def test_reimport_with_changed_composition_updates_it(self):
        self._import("Наименование,Цена,Ед,Состав упаковки\nКурица грудка филе,560,кг,8 шт. × 2 кг = 16 кг/кор.\n")
        self._import("Наименование,Цена,Ед,Состав упаковки\nКурица грудка филе,560,кг,6 шт. × 2 кг\n")
        product = SupplierProduct.objects.get(supplier=self.supplier)
        self.assertEqual(product.pack_items_count, 6)
        self.assertIsNone(product.pack_type)


class CatalogProductCardPackageCompositionTests(APITestCase):
    def test_card_returns_mockup_fields(self):
        supplier = Supplier.objects.create(legal_name="GFC LLC", display_name="GFC")
        product = SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name="Курица грудка филе без кожи 2кг, 16кг/кор",
            normalized_name="Курица грудка филе без кожи",
            unit=_unit("кг"),
            short_description="Филе куриной грудки без кожи",
            net_value=Decimal("16"),
            net_unit=_unit("кг"),
            pack_items_count=8,
            pack_item_value=Decimal("2"),
            pack_item_unit=_unit("кг"),
            pack_type=_unit("кор"),
            status=EntityStatus.ACTIVE,
        )
        SupplierProductPrice.objects.create(
            supplier_product=product, price="560.00", currency="RUB", status=PriceStatus.ACTIVE
        )

        data = self.client.get(f"/api/v1/catalog/products/{product.id}").json()

        self.assertEqual(data["package_composition"]["display"], "8 шт. × 2 кг = 16 кг/кор.")
        self.assertEqual(data["short_description"], "Филе куриной грудки без кожи")
        self.assertEqual(Decimal(data["net_value"]), Decimal("16"))
        self.assertEqual(data["net_unit"], "кг")
