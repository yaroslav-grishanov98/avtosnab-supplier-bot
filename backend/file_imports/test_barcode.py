from decimal import Decimal

from django.test import SimpleTestCase

from core.countries import COUNTRY_NAMES
from file_imports.barcode import Barcode, gs1_country, normalize_barcode
from file_imports.gs1_prefixes import GS1_PREFIX_RANGES


def check_digit(body: str) -> str:
    """GS1 check digit for a code body (everything except the last digit)."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def make_ean13(prefix_and_rest: str) -> str:
    """12 digits + check digit."""
    assert len(prefix_and_rest) == 12
    return prefix_and_rest + check_digit(prefix_and_rest)


def make_with_prefix(prefix: str) -> str:
    return make_ean13((prefix + "123456789")[:12])


class NormalizeBarcodeTests(SimpleTestCase):
    def assertSingle(self, raw, value, kind, valid=True):
        result = normalize_barcode(raw)
        self.assertEqual(result, [Barcode(value, kind, valid)], raw)

    def test_known_real_code_is_valid(self):
        self.assertEqual(check_digit("460549602963"), "2")
        self.assertSingle(4605496029632, "4605496029632", "ean13")

    def test_excel_number_forms(self):
        self.assertSingle(4605496029632, "4605496029632", "ean13")
        self.assertSingle(4605496029632.0, "4605496029632", "ean13")
        self.assertSingle(4.605496029632e12, "4605496029632", "ean13")
        self.assertSingle(Decimal("4605496029632"), "4605496029632", "ean13")
        self.assertSingle("4605496029632", "4605496029632", "ean13")
        self.assertSingle("4605496029632.0", "4605496029632", "ean13")
        self.assertSingle("4.605496029632E12", "4605496029632", "ean13")
        self.assertSingle("4.605496029632e+12", "4605496029632", "ean13")
        self.assertSingle("4,605496029632E12", "4605496029632", "ean13")

    def test_lossy_scientific_notation_is_not_guessed(self):
        # "4.6055E12" lost most digits: padding with zeros would invent a code.
        self.assertEqual(normalize_barcode("4.6055E12"), [])

    def test_non_integer_float_and_garbage_floats(self):
        self.assertEqual(normalize_barcode(4605496029632.5), [])
        self.assertEqual(normalize_barcode(float("nan")), [])
        self.assertEqual(normalize_barcode(float("inf")), [])

    def test_spaces_and_dashes_inside_one_code(self):
        self.assertSingle("4 605496 029632", "4605496029632", "ean13")
        self.assertSingle("4605496-029632", "4605496029632", "ean13")
        self.assertSingle("4-605496-029632", "4605496029632", "ean13")
        self.assertSingle("  4605496029632  ", "4605496029632", "ean13")
        self.assertSingle("EAN: 4605496029632", "4605496029632", "ean13")

    def test_upca_gets_a_leading_zero(self):
        upc = "03600029145"
        upc = upc + check_digit(upc)
        self.assertEqual(len(upc), 12)
        self.assertSingle(upc, "0" + upc, "upca")

    def test_ean13_with_lost_leading_zero_is_the_same_as_upca(self):
        ean = make_ean13("012345678901")
        self.assertEqual(ean[0], "0")
        self.assertSingle(int(ean), ean, "upca")  # 12 digits left, padded back to the same 13

    def test_eleven_digit_number_is_invalid_not_padded(self):
        # A UPC whose leading zero Excel dropped is indistinguishable from a short code.
        result = normalize_barcode(36000291452)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].valid)
        self.assertIsNone(gs1_country(result[0]))

    def test_eleven_digits_inside_text_are_ignored(self):
        self.assertEqual(normalize_barcode("тел. 79051234567"), [])

    def test_ean8(self):
        ean8 = "4601234" + check_digit("4601234")
        self.assertSingle(ean8, ean8, "ean8")
        self.assertSingle(int(ean8), ean8, "ean8")
        self.assertEqual(gs1_country(normalize_barcode(ean8)[0]), "Россия")

    def test_restricted_circulation_ean8_gives_no_country(self):
        # EAN-8 codes starting with 0 or 2 are restricted circulation numbers (RCN-8), not GS1 prefixes.
        for body in ("0123456", "0601234", "2001234"):
            with self.subTest(body=body):
                result = normalize_barcode(body + check_digit(body))
                self.assertEqual(result[0].kind, "ean8")
                self.assertTrue(result[0].valid)
                self.assertIsNone(gs1_country(result[0]))
        self.assertEqual(gs1_country(normalize_barcode("4601234" + check_digit("4601234"))[0]), "Россия")

    def test_ean13_and_upca_with_a_leading_zero_keep_their_country(self):
        self.assertEqual(gs1_country(Barcode("0614141234561", "upca", True)), "США")
        self.assertEqual(gs1_country(Barcode("0614141234561", "ean13", True)), "США")

    def test_ean8_cannot_come_from_joined_pieces(self):
        # "2024-01-05" is a date, not an EAN-8.
        self.assertEqual(normalize_barcode("2024-01-05"), [])

    def test_gtin14_drops_indicator_digit_and_checks_full_code(self):
        body = "1" + "460549602963"
        gtin = body + check_digit(body)
        self.assertEqual(len(gtin), 14)
        result = normalize_barcode(gtin)
        self.assertEqual(result, [Barcode(gtin[1:], "gtin14", True)])
        self.assertEqual(gs1_country(result[0]), "Россия")  # digits 2-4

    def test_gtin14_wrong_check_digit(self):
        body = "1" + "460549602963"
        wrong = body + str((int(check_digit(body)) + 1) % 10)
        self.assertEqual(normalize_barcode(wrong), [Barcode(wrong[1:], "gtin14", False)])

    def test_wrong_check_digit_is_invalid(self):
        self.assertSingle("4605496029633", "4605496029633", "ean13", valid=False)
        self.assertSingle("460549602963", "0460549602963", "upca", valid=False)

    def test_several_codes_in_one_cell(self):
        a = make_with_prefix("460")
        b = make_with_prefix("481")
        for sep in (", ", ";", " / ", "/", " ", "\n", "  ,  "):
            with self.subTest(sep=sep):
                self.assertEqual(
                    normalize_barcode(f"{a}{sep}{b}"),
                    [Barcode(a, "ean13", True), Barcode(b, "ean13", True)],
                )

    def test_two_codes_example_from_the_brief(self):
        result = normalize_barcode("4601234567890, 4811234567890")
        self.assertEqual([b.value for b in result], ["4601234567890", "4811234567890"])
        self.assertEqual([b.kind for b in result], ["ean13", "ean13"])

    def test_several_codes_with_different_prefixes_map_separately(self):
        codes = [make_with_prefix("460"), make_with_prefix("400"), make_with_prefix("690")]
        result = normalize_barcode("; ".join(codes))
        self.assertEqual([gs1_country(b) for b in result], ["Россия", "Германия", "Китай"])

    def test_spaced_code_next_to_another_code(self):
        a = make_with_prefix("460")
        b = make_with_prefix("800")
        spaced = f"{a[:1]} {a[1:7]} {a[7:]}"
        result = normalize_barcode(f"{spaced} / {b}")
        self.assertEqual([x.value for x in result], [a, b])

    def test_duplicates_are_collapsed(self):
        a = make_with_prefix("460")
        self.assertEqual(normalize_barcode(f"{a}, {a}"), [Barcode(a, "ean13", True)])

    def test_empty_and_garbage_never_crash(self):
        for raw in (None, "", "   ", "abc", "-", "--", "///", ",;", "н/д", "нет", True, False, 0, [], {}, b"123"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_barcode(raw), [])

    def test_huge_numbers_never_crash(self):
        for raw in (
            10**5000,
            -(10**5000),
            10**14,
            Decimal("1E+5000"),
            Decimal("-1E+5000"),
            Decimal("1E-5000"),
            Decimal("NaN"),
            Decimal("sNaN"),
            Decimal("Infinity"),
            float("inf"),
            float("-inf"),
            float("nan"),
            1e300,
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_barcode(raw), [])

    def test_largest_14_digit_number_is_still_parsed(self):
        result = normalize_barcode(10**14 - 1)
        self.assertEqual([b.kind for b in result], ["gtin14"])

    def test_huge_strings_never_crash_or_hang(self):
        for raw in (
            "1" * 10_000,
            "1 " * 5_000,
            "4605496029632, " * 1_000,
            "1" * 10_000 + "E5",
            "1." + "0" * 10_000 + "E5",
            "1E" + "9" * 5_000,
            "7" * 1_000_000,
        ):
            with self.subTest(length=len(raw)):
                self.assertEqual(normalize_barcode(raw), [])

    def test_wrong_length_digit_runs_are_ignored(self):
        for raw in ("1", "12345", "123456789", "1234567890", "123456789012345", "12345678901234567"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_barcode(raw), [])


class Gs1CountryTests(SimpleTestCase):
    def country(self, prefix, rest="123456789"):
        code = make_ean13((prefix + rest)[:12])
        result = normalize_barcode(code)
        self.assertEqual(len(result), 1, code)
        self.assertTrue(result[0].valid, code)
        return gs1_country(result[0])

    def test_prefix_expectations(self):
        expected = {
            "460": "Россия",
            "469": "Россия",
            "481": "Беларусь",
            "477": "Литва",
            "475": "Латвия",
            "800": "Италия",
            "839": "Италия",
            "871": "Нидерланды",
            "900": "Австрия",
            "590": "Польша",
            "690": "Китай",
            "680": "Китай",
            "681": "Китай",
            "699": "Китай",
            "778": "Аргентина",
            "779": "Аргентина",
            "400": "Германия",
            "440": "Германия",
            "450": "Япония",
            "490": "Япония",
            "489": "Гонконг",
            "958": "Макао",
            "471": "Тайвань",
            "381": "Косово",
            "387": "Босния и Герцеговина",
            "629": "ОАЭ",
            "746": "Доминиканская Республика",
            "868": "Турция",
            "789": "Бразилия",
            "487": "Казахстан",
            "859": "Чехия",
            "500": "Великобритания",
            "520": "Греция",
            "540": "Бельгия",
            "001": "США",
            "030": "США",
            "060": "США",
            "139": "США",
            "300": "Франция",
        }
        for prefix, country in expected.items():
            with self.subTest(prefix=prefix):
                self.assertEqual(self.country(prefix), country)

    def test_ignored_prefixes_never_give_a_country(self):
        for prefix in (
            "020",
            "025",
            "029",
            "040",
            "049",
            "050",
            "059",
            "200",
            "250",
            "299",
            "960",
            "969",
            "977",
            "978",
            "979",
            "980",
            "981",
            "990",
            "999",
        ):
            with self.subTest(prefix=prefix):
                self.assertIsNone(self.country(prefix))

    def test_prefixes_absent_from_the_table_give_none(self):
        for prefix in ("140", "199", "390", "445", "515", "633", "660", "751", "756", "920", "953"):
            with self.subTest(prefix=prefix):
                self.assertFalse(any(lo <= int(prefix) <= hi for lo, hi, _ in GS1_PREFIX_RANGES))
                self.assertIsNone(self.country(prefix))

    def test_invalid_code_gives_none(self):
        self.assertIsNone(gs1_country(Barcode("4605496029633", "ean13", False)))
        self.assertIsNone(gs1_country(Barcode("4605496029632", "ean13", False)))

    def test_valid_barcode_object_directly(self):
        self.assertEqual(gs1_country(Barcode("4605496029632", "ean13", True)), "Россия")

    def test_upca_uses_implied_leading_zero(self):
        # 614141234561 is the GS1 example: prefix 061 (US), not 614.
        self.assertEqual(check_digit("61414123456"), "1")
        result = normalize_barcode("614141234561")
        self.assertEqual(result, [Barcode("0614141234561", "upca", True)])
        self.assertEqual(gs1_country(result[0]), "США")

    def test_malformed_barcode_object_does_not_crash(self):
        self.assertIsNone(gs1_country(Barcode("", "ean13", True)))
        self.assertIsNone(gs1_country(Barcode("ab", "ean13", True)))


class Gs1PrefixTableTests(SimpleTestCase):
    def test_shape_and_values(self):
        previous_hi = -1
        for lo, hi, country in GS1_PREFIX_RANGES:
            self.assertLessEqual(lo, hi)
            self.assertGreater(lo, previous_hi, f"overlap at {lo}")
            self.assertTrue(0 <= lo and hi <= 999)
            self.assertIn(country, COUNTRY_NAMES)
            previous_hi = hi

    def test_table_excludes_non_member_ranges(self):
        covered = {p for lo, hi, _ in GS1_PREFIX_RANGES for p in range(lo, hi + 1)}
        for lo, hi in ((20, 29), (40, 49), (50, 59), (200, 299), (950, 952), (960, 969), (977, 983)):
            for prefix in range(lo, hi + 1):
                self.assertNotIn(prefix, covered, prefix)
        for prefix in (610, 614, 623, 758, 894):  # managed by GS1 Global Office for future MO
            self.assertNotIn(prefix, covered, prefix)
