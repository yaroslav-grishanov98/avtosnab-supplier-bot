"""Import validation, draft deduplication and per-row savepoint regressions."""

from decimal import Decimal
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from openpyxl import Workbook
from openpyxl.styles import Alignment, PatternFill

from api.v1.suppliers.product_import_matching import (
    find_supplier_product_import_candidates,
    find_supplier_products_by_sku,
)
from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file
from api.v1.suppliers.product_import_selectors import _create_import_items, _to_decimal
from api.v1.suppliers.product_import_services import commit_supplier_product_import_session
from api.v1.suppliers.serializers import SupplierProductImportItemPatchRequestSerializer
from api.v1.suppliers.tests import _make_supplier, _make_unit
from core.models import EntityStatus
from suppliers.models import SupplierProduct, SupplierProductImportItem, SupplierProductImportSession


class SupplierImportPipelineRegressionTests(TestCase):
    def setUp(self):
        self.supplier, self.unit = _make_supplier(), _make_unit()
        self.session = SupplierProductImportSession.objects.create(supplier=self.supplier)

    def item(self, number, name, **extra):
        return SupplierProductImportItem.objects.create(
            session=self.session,
            row_number=number,
            raw_name=name,
            name=name,
            country_of_origin="Россия",
            unit=self.unit,
            net_unit=self.unit,
            price=Decimal("0"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("1"),
            min_order_qty=Decimal("1"),
            availability="in_stock",
            action="new",
            **extra,
        )

    def test_zero_price_creates_draft_and_is_not_missing(self):
        self.item(1, "Мука")
        response = commit_supplier_product_import_session(session=self.session)
        self.assertEqual(response["imported_count"], 1)
        self.assertEqual(SupplierProduct.objects.get(supplier=self.supplier).status, EntityStatus.DRAFT)
        self.assertTrue(response["session_deleted"])

    def test_duplicate_sku_failure_does_not_rollback_good_row(self):
        SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Уже есть", supplier_sku="SKU")
        bad = self.item(1, "Дубликат", supplier_sku="SKU")
        self.item(2, "Хороший")
        response = commit_supplier_product_import_session(session=self.session)
        self.assertEqual(response["imported_count"], 1)
        self.assertEqual(response["errors"][0]["item_id"], str(bad.id))
        self.assertFalse(SupplierProduct.objects.filter(supplier=self.supplier, supplier_name="Дубликат").exists())
        self.assertTrue(SupplierProduct.objects.filter(supplier=self.supplier, supplier_name="Хороший").exists())
        self.assertEqual(self.session.items.count(), 1)

    def test_drafts_are_found_by_name_and_sku(self):
        product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Мука", supplier_sku="SKU", status=EntityStatus.DRAFT
        )
        self.assertEqual(
            find_supplier_products_by_sku(supplier_id=self.supplier.id, skus=["SKU"])["SKU"].id, product.id
        )
        self.assertEqual(
            find_supplier_product_import_candidates(supplier_id=self.supplier.id, names=["Мука"])[0][0]["id"],
            product.id,
        )

    def test_packaging_does_not_make_unrelated_foods_candidates(self):
        names = [
            'аджика "Балтийская" в ведре 1/5 кг, упак',
            "Бифштекс замороженный 1/0,295 кг",
        ]
        unrelated = [
            "Макароны спагетти Петровские Нивы 5 кг 1/5, шт",
            "Рис длиннозерный 1/5кг, шт",
            "Нектар яблочный 0,2 литра 1/27Смешарики, шт",
            "Биточки замороженные 1/0,295 кг",
        ]
        for name in unrelated:
            SupplierProduct.objects.create(supplier=self.supplier, supplier_name=name)
        self.assertEqual(find_supplier_product_import_candidates(supplier_id=self.supplier.id, names=names), {})
        adjika = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name='Аджика "Балтийская" в ведре 1/5 кг, шт'
        )
        steak = SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Бифштекс замороженный 1/0,300 кг")
        result = find_supplier_product_import_candidates(supplier_id=self.supplier.id, names=names)
        self.assertEqual([candidate["id"] for candidate in result[0]], [adjika.id])
        self.assertEqual([candidate["id"] for candidate in result[1]], [steak.id])

    def test_unrelated_higher_scores_do_not_hide_relevant_candidates(self):
        for index in range(6):
            SupplierProduct.objects.create(
                supplier=self.supplier, supplier_name=f"Каша рисовая длиннозерная 1/5 кг упаковка {index}"
            )
        rice = SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Рис длиннозерный 1/5 кг")
        result = find_supplier_product_import_candidates(
            supplier_id=self.supplier.id, names=["Рис рисовый длиннозерный 1/5 кг упаковка"]
        )
        self.assertEqual([candidate["id"] for candidate in result[0]], [rice.id])

    def test_exact_nonword_names_and_russian_inflections_remain_candidates(self):
        exact = SupplierProduct.objects.create(supplier=self.supplier, supplier_name="12345")
        adjika = SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Аджики Балтийские 1/5 кг")
        result = find_supplier_product_import_candidates(
            supplier_id=self.supplier.id, names=["12345", "аджика Балтийская 1/5 кг"]
        )
        self.assertEqual(result[0][0]["id"], exact.id)
        self.assertEqual(result[0][0]["score"], 1.0)
        self.assertEqual([candidate["id"] for candidate in result[1]], [adjika.id])

    def test_patch_rejects_negative_numbers_and_unknown_units(self):
        import uuid

        for payload in ({"price": "-1"}, {"net_value": "-1"}, {"min_order_qty": "0"}, {"unit_id": str(uuid.uuid4())}):
            serializer = SupplierProductImportItemPatchRequestSerializer(data=payload)
            self.assertFalse(serializer.is_valid(), payload)
        self.assertTrue(SupplierProductImportItemPatchRequestSerializer(data={"price": "0"}).is_valid())

    def test_russian_numbers_are_finite(self):
        self.assertEqual(_to_decimal("1\u00a0200,50 руб."), Decimal("1200.50"))
        self.assertIsNone(_to_decimal("NaN"))
        self.assertIsNone(_to_decimal("Infinity"))

    def test_duplicate_sku_is_reviewed_across_separate_batches(self):
        from api.v1.suppliers.product_import_selectors import _apply_sku_matches

        target = SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Мука", supplier_sku="SKU")
        first, second = self.item(1, "Мука первая", supplier_sku="SKU"), self.item(2, "Мука вторая", supplier_sku="SKU")
        _apply_sku_matches(supplier=self.supplier, items=[first])
        _apply_sku_matches(supplier=self.supplier, items=[second])
        self.assertEqual(first.target_supplier_product_id, target.id)
        self.assertEqual(second.action, "review")
        self.assertTrue(second.validation_errors)

    def match(self, *items):
        from api.v1.suppliers.product_import_selectors import _enrich_and_match_items

        _enrich_and_match_items(supplier=self.supplier, items=list(items))
        for item in items:
            item.refresh_from_db()

    def test_exact_name_of_an_existing_product_is_a_replace_not_a_review(self):
        product = SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Мука пшеничная в/с 25 кг")
        item = self.item(1, "мука пшеничная в/с 25 кг")  # case differs, the name is the same
        self.match(item)
        self.assertEqual(item.action, "replace")
        self.assertEqual(item.target_supplier_product_id, product.id)
        self.assertEqual(item.target_expected_updated_at, product.updated_at)
        self.assertEqual(item.match_candidates[0]["score"], 1.0)

    def test_a_similar_name_is_still_a_review(self):
        SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Мука пшеничная в/с 25 кг")
        item = self.item(1, "Мука пшеничная в/с 50 кг")
        self.match(item)
        self.assertEqual(item.action, "review")
        self.assertIsNone(item.target_supplier_product_id)
        self.assertEqual(len(item.match_candidates), 1)

    def test_an_exact_name_repeated_in_the_file_replaces_only_once(self):
        product = SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Мука пшеничная")
        first, second = self.item(1, "Мука пшеничная"), self.item(2, "Мука пшеничная")
        self.match(first, second)
        self.assertEqual([first.action, second.action], ["replace", "review"])
        self.assertEqual(first.target_supplier_product_id, product.id)
        self.assertIsNone(second.target_supplier_product_id)

    def test_exact_names_that_differ_only_by_case_in_the_file_do_not_share_a_target(self):
        SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Мука пшеничная")
        first, second = self.item(1, "Мука пшеничная"), self.item(2, "МУКА ПШЕНИЧНАЯ")
        self.match(first, second)
        self.assertEqual(
            sorted([first.action, second.action]),
            ["replace", "review"],
        )

    def test_a_product_taken_by_sku_is_not_also_taken_by_name(self):
        product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Мука пшеничная", supplier_sku="SKU-1"
        )
        by_sku = self.item(1, "Мука пшеничная высший сорт", supplier_sku="SKU-1")
        by_name = self.item(2, "Мука пшеничная")
        self.match(by_sku, by_name)
        self.assertEqual(by_sku.target_supplier_product_id, product.id)
        self.assertEqual(by_name.action, "review")
        self.assertIsNone(by_name.target_supplier_product_id)

    def test_sku_of_a_product_with_an_unrelated_name_is_a_review(self):
        """Аудит идентификаторов, F2: поставщик отдал старый артикул другому товару -- карточку не переписываем."""
        product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Мука пшеничная 25 кг", supplier_sku="SKU-1"
        )
        item = self.item(1, "Сахар песок 1 кг", supplier_sku="SKU-1")
        self.match(item)
        self.assertEqual(item.action, "review")
        self.assertIsNone(item.target_supplier_product_id)
        self.assertEqual(item.match_candidates[0]["id"], str(product.id))
        self.assertEqual(
            item.validation_errors,
            [
                "Артикул SKU-1 уже у товара «Мука пшеничная 25 кг» -- похоже, это другой товар. "
                "Если это тот же товар, выберите замену; если другой -- исправьте артикул."
            ],
        )

        commit_supplier_product_import_session(session=self.session)

        product.refresh_from_db()
        self.assertEqual(product.supplier_name, "Мука пшеничная 25 кг")

    def test_sku_of_an_archived_product_is_a_review_with_a_hint(self):
        """Аудит идентификаторов, F6: матчинг архив не видит, а новую карточку с этим артикулом не сохранить."""
        SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Мука пшеничная 25 кг",
            supplier_sku="SKU-1",
            status=EntityStatus.ARCHIVED,
        )
        item = self.item(1, "Мука пшеничная 25 кг", supplier_sku="SKU-1")
        self.match(item)
        self.assertEqual(item.action, "review")
        self.assertIsNone(item.target_supplier_product_id)
        self.assertEqual(
            item.validation_errors,
            [
                "Артикул SKU-1 занят товаром «Мука пшеничная 25 кг» в архиве. "
                "Восстановите товар из архива или исправьте артикул."
            ],
        )

    def test_sku_of_an_inactive_product_is_a_review_with_a_hint(self):
        SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Мука пшеничная 25 кг",
            supplier_sku="SKU-1",
            status=EntityStatus.INACTIVE,
        )
        item = self.item(1, "Сахар песок 1 кг", supplier_sku="SKU-1")
        self.match(item)
        self.assertEqual(item.action, "review")
        self.assertEqual(
            item.validation_errors,
            [
                "Артикул SKU-1 занят неактивным товаром «Мука пшеничная 25 кг». "
                "Сделайте товар снова активным или исправьте артикул."
            ],
        )

    def test_choosing_replace_for_an_unrelated_name_clears_the_warning(self):
        from api.v1.suppliers.product_import_services import set_supplier_product_import_item_action

        product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Мука пшеничная 25 кг", supplier_sku="SKU-1"
        )
        item = self.item(1, "Сахар песок 1 кг", supplier_sku="SKU-1")
        self.match(item)

        item = set_supplier_product_import_item_action(
            item=item, action="replace", target_supplier_product_id=product.id
        )

        self.assertEqual(item.action, "replace")
        self.assertEqual(item.validation_errors, [])

    def test_readiness_rendering_uses_prefetched_units(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from api.v1.suppliers.product_import_selectors import supplier_product_import_session_payload
        from api.v1.suppliers.serializers import SupplierProductImportSessionResponseSerializer

        self.session.processing_status = "ready"
        self.session.save()
        for number in range(20):
            self.item(number + 1, f"Товар {number}")
        with CaptureQueriesContext(connection) as queries:
            payload = supplier_product_import_session_payload(self.session)
            data = SupplierProductImportSessionResponseSerializer(payload).data
        self.assertEqual(data["counts"]["ready"], 20)
        self.assertTrue(all(item["ready"] for item in data["items"]))
        self.assertLess(len(queries), 6)

    def test_bulk_availability_preserves_other_row_errors(self):
        from api.v1.suppliers.product_import_services import bulk_set_supplier_product_import_items_availability

        item = self.item(1, "Ошибка")
        item.validation_errors = ["Не заполнено поле availability.", "Конфликт уникальных полей товара."]
        item.save()
        count = bulk_set_supplier_product_import_items_availability(
            session=self.session, item_ids=[item.id], availability="in_stock"
        )
        self.assertEqual(count, 1)
        item.refresh_from_db()
        self.assertEqual(item.validation_errors, ["Конфликт уникальных полей товара."])


@override_settings(
    CANONICAL_SUPPLIER_PARSER_ENABLED=True,
    CANONICAL_IMPORT_LLM_ENABLED=False,
    CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED=True,
)
class CountryOfOriginSourceTests(TestCase):
    """field_sources[country_of_origin] says where an inferred country came from; a file column stays absent."""

    PRICE = (
        "Название;Страна;Штрихкод;Цена\n"
        "Сыр Гауда 1 кг;Голландия;;100\n"
        "Макароны 500г, Турция;;;50\n"
        "Бульон Роллтон 200г;;4605496029632;30\n"
        "Соль 1 кг;;;20\n"
    )

    def import_items(self, text):
        rows = parse_supplier_product_import_file(SimpleUploadedFile("price.csv", text.encode("utf-8")))
        session = SupplierProductImportSession.objects.create(supplier=_make_supplier())
        _create_import_items(session, rows)
        return {item.row_number: item for item in session.items.all()}

    def test_country_source_is_recorded_for_name_and_barcode_but_not_for_a_column(self):
        items = list(self.import_items(self.PRICE).values())
        items.sort(key=lambda item: item.row_number)
        column, in_name, by_barcode, nothing = items

        self.assertEqual(column.country_of_origin, "Нидерланды")
        self.assertNotIn("country_of_origin", column.field_sources)
        self.assertEqual(in_name.country_of_origin, "Турция")
        self.assertEqual(in_name.field_sources["country_of_origin"], "name")
        self.assertEqual(by_barcode.country_of_origin, "Россия")
        self.assertEqual(by_barcode.field_sources["country_of_origin"], "gs1_prefix")
        self.assertFalse(nothing.country_of_origin)
        self.assertNotIn("country_of_origin", nothing.field_sources)

    def test_country_from_a_section_heading_is_recorded_as_section(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Номенклатура", "Цена"])
        sheet.append(["Италия", None])
        sheet.append(["Макароны 500г", 50])
        sheet.append(["Соус 200г", 70])
        sheet.cell(row=2, column=1).fill = PatternFill(patternType="solid", fgColor="FFD9D9D9")
        sheet.cell(row=2, column=1).alignment = Alignment(indent=0)
        for row_number in (3, 4):
            sheet.cell(row=row_number, column=1).alignment = Alignment(indent=2)
        buffer = BytesIO()
        workbook.save(buffer)
        rows = parse_supplier_product_import_file(SimpleUploadedFile("price.xlsx", buffer.getvalue()))
        session = SupplierProductImportSession.objects.create(supplier=_make_supplier())
        _create_import_items(session, rows)

        items = list(session.items.order_by("row_number"))
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item.country_of_origin, "Италия")
            self.assertEqual(item.field_sources["country_of_origin"], "section")

    def test_country_from_the_llm_is_recorded_as_llm(self):
        def row(number, country, source):
            return {
                "row_number": number,
                "name": f"Товар {number}",
                "country_of_origin": country,
                "unit_label": None,
                "price": "10",
                "delivery_multiple": None,
                "net_value": None,
                "net_unit_label": None,
                "min_order_qty": None,
                "availability_label": None,
                "supplier_sku": None,
                "package_composition": None,
                "_parsed": {
                    "parser_version": "canonical-products-v3",
                    "fields": {"country_of_origin": {"value": country, "source": source, "status": "inferred"}},
                },
            }

        session = SupplierProductImportSession.objects.create(supplier=_make_supplier())
        _create_import_items(session, [row(1, "Италия", "llm"), row(2, None, "llm")])
        filled, empty = session.items.order_by("row_number")

        self.assertEqual(filled.field_sources["country_of_origin"], "llm")
        self.assertNotIn("country_of_origin", empty.field_sources)

    def test_ambiguous_barcode_countries_record_no_source(self):
        text = 'Название;Штрихкод;Цена\nБульон Роллтон 200г;"4605496029632, 4810001000017";30\n'
        (item,) = self.import_items(text).values()

        self.assertFalse(item.country_of_origin)
        self.assertNotIn("country_of_origin", item.field_sources)


@override_settings(CANONICAL_SUPPLIER_PARSER_ENABLED=True, CANONICAL_IMPORT_LLM_ENABLED=False)
class CountryHintsOffByDefaultTests(TestCase):
    """Without CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED a barcode gives no country; name and column countries stay."""

    def test_only_explicit_countries_reach_the_rows(self):
        rows = parse_supplier_product_import_file(
            SimpleUploadedFile("price.csv", CountryOfOriginSourceTests.PRICE.encode("utf-8"))
        )
        session = SupplierProductImportSession.objects.create(supplier=_make_supplier())
        _create_import_items(session, rows)
        column, in_name, by_barcode, nothing = session.items.order_by("row_number")

        self.assertEqual(column.country_of_origin, "Нидерланды")
        self.assertEqual(in_name.field_sources["country_of_origin"], "name")
        self.assertFalse(by_barcode.country_of_origin)
        self.assertNotIn("country_of_origin", by_barcode.field_sources)
        self.assertEqual(by_barcode.parsed_snapshot["fields"]["barcode"]["value"], "4605496029632")
        self.assertFalse(nothing.country_of_origin)


class ManufacturerArticleColumnTests(TestCase):
    """«Артикул производителя» -- код изготовителя (MPN), а не поставщика: в supplier_sku (ключ replace) не идёт."""

    def skus(self, text):
        rows = parse_supplier_product_import_file(SimpleUploadedFile("price.csv", text.encode("utf-8")))
        session = SupplierProductImportSession.objects.create(supplier=_make_supplier())
        _create_import_items(session, rows)
        return list(session.items.order_by("row_number").values_list("supplier_sku", flat=True))

    def check_both_parsers(self, text, expected):
        for canonical in (False, True):
            with (
                self.subTest(canonical=canonical),
                override_settings(CANONICAL_SUPPLIER_PARSER_ENABLED=canonical, CANONICAL_IMPORT_LLM_ENABLED=False),
            ):
                self.assertEqual(self.skus(text), expected)

    def test_manufacturer_article_alone_is_not_a_supplier_sku(self):
        self.check_both_parsers("Наименование;Артикул производителя;Цена\nМука пшеничная 1 кг;MP-001;50\n", [None])

    def test_supplier_article_wins_over_manufacturer_article(self):
        self.check_both_parsers(
            "Наименование;Артикул поставщика;Артикул производителя;Цена\nМука пшеничная 1 кг;S-1;MP-001;50\n", ["S-1"]
        )
        self.check_both_parsers(
            "Наименование;Артикул производителя;Артикул поставщика;Цена\nМука пшеничная 1 кг;MP-001;S-1;50\n", ["S-1"]
        )

    def test_plain_article_is_still_a_supplier_sku(self):
        self.check_both_parsers("Наименование;Артикул;Цена\nМука пшеничная 1 кг;A-7;50\n", ["A-7"])
