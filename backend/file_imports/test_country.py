from django.test import SimpleTestCase

from core.countries import COUNTRY_NAMES, normalize_country
from file_imports.country import (
    ROW_COUNTRY_PATTERN,
    CountryHit,
    country_from_heading,
    find_country_in_text,
)


class FindCountryInTextTests(SimpleTestCase):
    def test_row_finds(self):
        cases = {
            "Дорадо целая с/м (300-400г) Турция (короб. 5 кг)": "Турция",
            "Лук жареный Kotanyi (Австрия) 1кг": "Австрия",
            "Гребешок IQF 80/100 500гр Китай": "Китай",
            "Бекон нарезка 220гр, РФ": "Россия",
            "Сыр, пр-во Италия": "Италия",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                hit = find_country_in_text(text)
                self.assertIsInstance(hit, CountryHit)
                self.assertEqual(hit.value, expected)

    def test_row_evidence_and_span_point_at_the_country_token(self):
        text = "Гребешок IQF 80/100 500гр Китай"
        hit = find_country_in_text(text)
        self.assertEqual(hit.evidence, "Китай")
        self.assertEqual(text[hit.span[0] : hit.span[1]], "Китай")

    def test_row_abbreviations_and_markers(self):
        cases = {
            "Масло сливочное 82% РБ": "Беларусь",
            "Чай чёрный КНР 100 пак": "Китай",
            "Сок яблочный ОАЭ": "ОАЭ",
            "Орехи кешью США 1 кг": "США",
            "Томаты Производство Марокко": "Марокко",
            "Сыр made in Italy": "Италия",
            "Мёд Страна-производитель: Китай": "Китай",
            "Соус пр-во Чили": "Чили",
            "Сыр, пр-во Россия.": "Россия",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                hit = find_country_in_text(text)
                self.assertIsNotNone(hit)
                self.assertEqual(hit.value, expected)

    def test_row_ignores(self):
        for text in (
            "Сыр Брест-Литовск российский 50%",
            "Булочка Французская 30гр",
            "Багет для французского хот-дога",
            "Соус Чили 200мл",
            "Корейка свиная",
            "Китайская капуста",
            "Мука Россия-пшеница 1 кг",
            "",
        ):
            with self.subTest(text=text):
                self.assertIsNone(find_country_in_text(text))

    def test_heading(self):
        self.assertEqual(country_from_heading("Италия"), "Италия")
        self.assertEqual(country_from_heading("Продукция Беларуси"), "Беларусь")
        self.assertEqual(country_from_heading("Производство Россия"), "Россия")
        self.assertEqual(country_from_heading("Продукция Чили"), "Чили")
        self.assertEqual(country_from_heading("Страна-производитель: Китай"), "Китай")
        self.assertEqual(country_from_heading("Россия."), "Россия")

    def test_heading_ignores(self):
        for text in ("Чили", "Соус Чили", "Индийский чай", "Бульоны, супы", "Роллтон", "", None):
            with self.subTest(text=text):
                self.assertIsNone(country_from_heading(text))


class NameCandidatesCountryTests(SimpleTestCase):
    def test_country_candidate_in_row(self):
        from file_imports.canonical import name_candidates

        candidate = name_candidates("Гребешок IQF 80/100 500гр Китай")["country_of_origin"][0]
        self.assertEqual(
            (candidate.value, candidate.method, candidate.source, candidate.confidence, candidate.evidence),
            ("Китай", "country-in-row", "name", 0.95, "Китай"),
        )
        self.assertEqual(candidate.status, "parsed")


class LongWhitespaceTests(SimpleTestCase):
    """A long run of spaces after a marker must not make the marker regex backtrack quadratically."""

    def test_long_whitespace_runs_return_quickly(self):
        import time

        spaces = " " * 50_000
        cases = {
            f"Сыр пр-во{spaces}x": None,
            f"Сыр пр-во{spaces}Италия": "Италия",
            f"Сыр страна{spaces}x": None,
            f"Сыр made in{spaces}x": None,
            f"Сыр 500г{spaces}Китай": "Китай",
        }
        for text, expected in cases.items():
            with self.subTest(text=text[:12], expected=expected):
                started = time.monotonic()
                hit = find_country_in_text(text)
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(hit.value if hit else None, expected)
        started = time.monotonic()
        self.assertIsNone(country_from_heading(f"Производство{spaces}x"))
        self.assertEqual(country_from_heading(f"Производство{spaces}Италия"), "Италия")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_span_still_points_into_the_original_text(self):
        text = "Сыр,   пр-во    Италия"
        hit = find_country_in_text(text)
        self.assertEqual(text[hit.span[0] : hit.span[1]], "Италия")


class FixRound1Tests(SimpleTestCase):
    def test_row_values_are_canonical_names_only(self):
        self.assertEqual(find_country_in_text("Манго, Тайланд").value, "Таиланд")
        self.assertIsNone(find_country_in_text("Сыр, Корея"))
        self.assertIsNone(find_country_in_text("Сыр, пр-во Корея"))
        for token in ROW_COUNTRY_PATTERN.split("|"):
            with self.subTest(token=token):
                self.assertIn(normalize_country(token), COUNTRY_NAMES)

    def test_marker_needs_a_written_country_name(self):
        for text in (
            "Масло производство того же завода",
            "Сыр пр-во мали",
            "Сыр пр-во того",
            "Колбаса производство гана",
            "МАСЛО ПРОИЗВОДСТВО ТОГО ЖЕ ЗАВОДА",
        ):
            with self.subTest(text=text):
                self.assertIsNone(find_country_in_text(text))

    def test_marker_keeps_working_with_normal_spelling(self):
        cases = {
            "Сыр made in Spain": "Испания",
            "Сыр Made In Spain": "Испания",
            "Колбаса Product of Germany": "Германия",
            "Сыр ПР-ВО Италия": "Италия",
            "Сыр производство Того": "Того",
            "Сыр made in UK": "Великобритания",
            "Сыр made in Hong Kong": "Гонконг",
            "Орехи пр-во США": "США",
            "Чай пр-во КНР": "Китай",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                hit = find_country_in_text(text)
                self.assertIsNotNone(hit)
                self.assertEqual(hit.value, expected)
