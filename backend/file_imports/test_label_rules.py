"""Parsing rules confirmed by the hand-labelled reference rows (owner decisions D1, D2, D9, D10 and unit words)."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file
from file_imports.canonical import parse_product_row
from file_imports.types import RawImportRow


def _parse(name, unit=None):
    header = "Наименование;Цена" + (";Ед." if unit is not None else "")
    line = f"{name};100,50" + (f";{unit}" if unit is not None else "")
    content = f"{header}\n{line}\n".encode()
    row = parse_supplier_product_import_file(SimpleUploadedFile("labels.csv", content), canonical_enabled=True)[0]
    fields = row["_parsed"]["fields"]

    def value(key):
        field = fields.get(key, {})
        return None if field.get("status") in ("ambiguous", "invalid") else field.get("value")

    return {
        "unit": row["unit_label"],
        "net": (row["net_value"], row["net_unit_label"]),
        "pack": (value("pack_items_count"), value("pack_item_value"), value("pack_item_unit_label")),
        "fields": fields,
    }


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class SoldByWeightTests(TestCase):
    """D1/D10: a kg sale unit means net 1 kg; the weight of a piece in the name is only a reference."""

    def test_approximate_piece_weight_does_not_become_the_net(self):
        row = _parse('Маасдам 45% брус ТМ "Сыробогатов" ~3кг, кг')
        self.assertEqual((row["unit"], row["net"]), ("кг", ("1", "кг")))
        reference = row["fields"]["estimated_piece_weight"]
        self.assertEqual(Decimal(str(reference["value"])), Decimal("3"))

    def test_weight_word_means_sold_by_kg(self):
        row = _parse("Угорь замороженный потрошёный 500гр+ вес")
        self.assertEqual((row["unit"], row["net"]), ("кг", ("1", "кг")))

    def test_kg_column_with_packaging_ratio(self):
        row = _parse("картофель вареный фасовка 1/3 кг весовой", unit="кг")
        self.assertEqual((row["unit"], row["net"]), ("кг", ("1", "кг")))
        self.assertIsNone(row["pack"][0])

    def test_piece_unit_keeps_the_weight_from_the_name(self):
        row = _parse("Сыр Гауда 45% 180г, шт")
        self.assertEqual((row["unit"], row["net"]), ("шт", ("180", "г")))


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class SingleContainerTests(TestCase):
    """D2: «1/5кг» is one container of 5 kg, not a 1 × 5 kg composition."""

    def test_one_over_weight_gives_net_and_no_composition(self):
        row = _parse("капуста квашеная с клюквой в квадратной банке 1/0,5кг", unit="упак")
        self.assertEqual(row["net"], ("0.5", "кг"))
        self.assertEqual(row["pack"], (None, None, None))

    def test_container_word_keeps_the_single_item_composition(self):
        row = _parse("капуста квашеная с клюквой в ведре 1/5кг", unit="упак")
        self.assertEqual(row["net"], ("5", "кг"))
        self.assertEqual(row["pack"], (1, "5", "кг"))
        self.assertEqual(row["fields"]["pack_type_label"]["value"], "ведро")

    def test_real_case_ratio_composition_is_kept(self):
        row = _parse("Сок 0,2 л 6x0,2л", unit="упак")
        self.assertEqual(row["pack"][0], 6)


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class MassOverCountTests(TestCase):
    """D9: «180г/6шт» and «200гр х 15 пл/бут» are N items of X each."""

    def test_mass_slash_pieces(self):
        row = _parse('Бри СНЭК тм "Alti" 60% 180г/6шт, шт')
        self.assertEqual(row["pack"], (6, "180", "г"))
        self.assertEqual(row["net"], ("180", "г"))

    def test_mass_times_count_with_container_word(self):
        row = _parse("Соус чили MIVIMEX барбекю 200гр х 15 пл/бут., шт")
        self.assertEqual(row["pack"], (15, "200", "г"))

    def test_case_ratio_without_pieces_is_not_a_composition(self):
        row = _parse("Масло подсолнечное Идеал 2л/6, шт")
        self.assertEqual(row["pack"], (None, None, None))
        self.assertEqual(row["net"], ("2", "л"))


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class CountryInRowTests(TestCase):
    """D5: a country named in the row itself is evidence; a country in a section header is not."""

    def test_country_after_a_comma(self):
        row = _parse("Лапша рисовая 500 гр, Вьетнам 1/20, шт")
        self.assertEqual(row["fields"]["country_of_origin"]["value"], "Вьетнам")

    def test_country_in_brackets(self):
        row = _parse("Соль 1кг (Россия) Илецкая 1//50, шт")
        self.assertEqual(row["fields"]["country_of_origin"]["value"], "Россия")

    def test_country_after_a_closing_bracket(self):
        row = _parse("Дорадо целая с/м (300-400г) Турция (короб. 5 кг) МО")
        self.assertEqual(row["fields"]["country_of_origin"]["value"], "Турция")

    def test_product_words_that_look_like_countries_are_not_countries(self):
        for name in ("перец ЧИЛИ острый 100г", "Соус Чили сладкий 200г", "Шоколад Россия щедрая душа 100г"):
            with self.subTest(name=name):
                self.assertIsNone(_parse(name)["fields"].get("country_of_origin", {}).get("value"))

    def test_column_country_wins_over_the_name(self):
        row = parse_product_row(
            RawImportRow(20, {"name": "Лапша, Вьетнам 500 г", "country_of_origin": "Италия"}, {}), lambda key: key
        )
        self.assertEqual(row.fields["country_of_origin"].value, "Италия")


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class CountInBoxTests(TestCase):
    def test_pieces_in_a_box_with_the_item_volume_in_brackets(self):
        row = _parse("МОЛ. КОКТЕЙЛЬ 1,2% Белый Город ШОКОЛАД (0.2л) Edge 27 шт в кор, шт")
        self.assertEqual(row["pack"], (27, "0.2", "л"))

    def test_the_name_keeps_the_box_text(self):
        row = _parse("Лапша яичная 500 гр, 30 шт/кор, Китай 1/30, шт")
        self.assertIn("30 шт", row["fields"]["name"]["value"])


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class ConflictingContainerWeightTests(TestCase):
    def test_container_ratio_and_another_weight_in_brackets_need_review(self):
        row = _parse("Крупа фасоль красная 1/25 кг, (5 кг)")
        self.assertEqual(row["fields"]["net_value"]["status"], "ambiguous")

    def test_container_ratio_alone_is_a_clean_net(self):
        row = _parse("Крупа кукурузная 1/4кг, шт")
        self.assertNotEqual(row["fields"]["net_value"]["status"], "ambiguous")

    def test_total_weight_next_to_a_real_composition_is_not_a_conflict(self):
        row = _parse("Сыр 6x200г (1,2кг), шт")
        self.assertNotEqual(row["fields"]["net_value"]["status"], "ambiguous")


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class CaseRatioTests(TestCase):
    """D11: «1/N» with no unit after N means N pieces in a box; the composition is N x the one measure of the name."""

    def test_ratio_after_a_measure_gives_the_composition(self):
        self.assertEqual(_parse('Соус Мирин ТМ "Yakimal", 1л 1/12, шт')["pack"], (12, "1", "л"))
        self.assertEqual(_parse("Паста СПАГЕТТИ BARILLA  850гр 1/12, шт")["pack"], (12, "850", "г"))

    def test_double_slash_and_brackets(self):
        self.assertEqual(_parse("Соль 1кг (Россия) Илецкая 1//50, шт")["pack"], (50, "1", "кг"))
        self.assertEqual(_parse("Мороженое Сибирское Ваниль 450гр (1/5)")["pack"], (5, "450", "г"))

    def test_ratio_without_a_measure_gives_only_the_count(self):
        row = _parse("Горчица Медовая 170 1/12, шт")
        self.assertEqual(row["pack"], (12, None, None))

    def test_two_different_measures_do_not_make_a_composition(self):
        row = _parse("Шампиньоны резанные 2650 мл (2,55 кг) 1/6, шт")
        self.assertEqual(row["pack"][1:], (None, None))

    def test_single_container_and_measure_over_number_are_not_case_ratios(self):
        self.assertEqual(_parse("Крупа кукурузная 1/4кг, шт")["pack"], (None, None, None))
        self.assertEqual(_parse("Масло подсолнечное Идеал 2л/6, шт")["pack"], (None, None, None))

    def test_sold_by_kg_does_not_get_a_case_composition(self):
        self.assertEqual(_parse('Маасдам 45% брус ТМ "Сыробогатов" ~3кг 1/12, кг')["pack"], (None, None, None))

    def test_decimal_comma_is_not_a_ratio(self):
        self.assertEqual(_parse("Йогурт 1/0,5 литра")["pack"][0], None)

    def test_the_name_keeps_the_ratio_text(self):
        row = _parse('Соус Мирин ТМ "Yakimal", 1л 1/12, шт')
        self.assertIn("1/12", row["fields"]["name"]["value"])


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class UnitWordTests(TestCase):
    def test_litre_words_are_volume_units(self):
        self.assertEqual(_parse("Нектар яблочный 0,2 литра 1/27Смешарики, шт")["net"], ("0.2", "л"))
        self.assertEqual(_parse("Сок апельсиновый Tetra pak без крышки 1 литр 1/12 ГОСТ Диас, шт")["net"], ("1", "л"))

    def test_capital_l_size_next_to_a_weight_is_not_a_volume(self):
        self.assertEqual(_parse("Креветка северная с/м 2L Каллиста 1кг")["net"], ("1", "кг"))

    def test_number_glued_by_a_hyphen_is_not_a_thousands_group(self):
        row = _parse("Фас. Пармезан 40% Gran Riserva-24 180г/8шт, шт")
        self.assertEqual(row["net"], ("180", "г"))
