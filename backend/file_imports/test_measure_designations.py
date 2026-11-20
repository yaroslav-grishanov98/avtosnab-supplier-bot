"""A product designation («№2», «сорт 2») glued to a weight must not become part of the weight."""

from django.test import TestCase, override_settings

from file_imports.golden import parse_projection


def _parse(name):
    content = f"Наименование;Цена;Ед.\n{name};100,50;шт\n".encode()
    return parse_projection("designation.csv", content)["supplier"][0]


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class MeasureDesignationTests(TestCase):
    def assertNet(self, row, value, unit):
        self.assertEqual((row["net_value"], row["net_unit_label"]), (value, unit))

    def test_number_sign_designation_is_not_part_of_the_weight(self):
        row = _parse("Томаты №2 800г")
        self.assertNet(row, "800", "г")
        self.assertEqual(row["name"], "Томаты №2")

    def test_multi_digit_designation_is_kept_in_the_name(self):
        row = _parse("Томаты №12 500г")
        self.assertNet(row, "500", "г")
        self.assertEqual(row["name"], "Томаты №12")

    def test_grade_designation_is_not_part_of_the_weight(self):
        row = _parse("Огурцы сорт 2 500 г")
        self.assertNet(row, "500", "г")
        self.assertEqual(row["name"], "Огурцы сорт 2")

    def test_thousands_grouping_without_designation_still_parsed(self):
        row = _parse("Томаты 2 800 г")
        self.assertNet(row, "2800", "г")
        self.assertEqual(row["name"], "Томаты")

    def test_plain_weight_unchanged(self):
        row = _parse("Мука 1 кг")
        self.assertNet(row, "1", "кг")
        self.assertEqual(row["name"], "Мука")

    def test_glued_case_ratio_unchanged(self):
        row = _parse("Молоко 1000 мл1/12")
        self.assertNet(row, "1000", "мл")
