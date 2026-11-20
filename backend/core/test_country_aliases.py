from django.test import SimpleTestCase

from core.countries import COUNTRY_ALIASES, COUNTRY_NAMES, normalize_country


class CountryAliasTests(SimpleTestCase):
    def test_aliases(self):
        cases = {
            "РФ": "Россия",
            "Рос.": "Россия",
            "Российская Федерация": "Россия",
            "РБ": "Беларусь",
            "Респ. Беларусь": "Беларусь",
            "Белоруссия": "Беларусь",
            "Голландия": "Нидерланды",
            "КНР": "Китай",
            "Italy": "Италия",
            "China": "Китай",
            "пр-во Италия": "Италия",
            "Производство: Турция": "Турция",
            "Страна: Египет": "Египет",
            "made in Spain": "Испания",
            "Product of Germany": "Германия",
            " россия ": "Россия",
            "Чили": "Чили",
            "ОАЭ": "ОАЭ",
            "США": "США",
            "UK": "Великобритания",
            "Англия": "Великобритания",
            "Страна происхождения: Польша": "Польша",
            "Hong Kong": "Гонконг",
            "Chinese Taipei": "Тайвань",
            "Macau": "Макао",
            "Продукция Беларуси": "Беларусь",
            "Беларуси": "Беларусь",
            "Италии": "Италия",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(normalize_country(text), expected)

    def test_not_countries(self):
        for text in (
            "корейка",
            "российский",
            "Французская",
            "итальянский",
            "Россия, Краснодарский край",
            "РФ/Китай",
            "Производство:",
            "",
            None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(normalize_country(text))

    def test_one_trailing_dot_is_ignored(self):
        cases = {"Россия.": "Россия", "РФ.": "Россия", "пр-во Италия.": "Италия", "Рос.": "Россия", "Рос..": "Россия"}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(normalize_country(text), expected)
        self.assertIsNone(normalize_country("."))
        self.assertIsNone(normalize_country("Россия..."))

    def test_every_alias_target_is_a_known_country(self):
        for alias, country in COUNTRY_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(country, COUNTRY_NAMES)
                self.assertEqual(alias, alias.casefold())

    def test_gs1_territories_are_listed(self):
        for name in ("Косово", "Гонконг", "Макао", "Тайвань"):
            self.assertIn(name, COUNTRY_NAMES)
        self.assertEqual(COUNTRY_NAMES, sorted(COUNTRY_NAMES))
        self.assertEqual(len(COUNTRY_NAMES), len(set(COUNTRY_NAMES)))
