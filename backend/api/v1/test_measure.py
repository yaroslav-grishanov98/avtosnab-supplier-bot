from decimal import Decimal

from django.test import SimpleTestCase

from api.v1.measure import NetValueField, format_net_value, round_net_value


class NetValueFormatTests(SimpleTestCase):
    def test_format_net_value(self):
        cases = [
            (Decimal("100.0000"), "100"),
            (Decimal("100.000"), "100"),
            (Decimal("0.5000"), "0.5"),
            (Decimal("1.2345"), "1.235"),
            (Decimal("1.2344"), "1.234"),
            (Decimal("0.0004"), "0"),
            (Decimal("1000"), "1000"),
            (Decimal("2.5"), "2.5"),
            (None, None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(format_net_value(value), expected)

    def test_round_net_value_keeps_three_places(self):
        self.assertEqual(round_net_value(Decimal("1.23456")), Decimal("1.235"))
        self.assertIsNone(round_net_value(None))

    def test_net_value_field_round_trip(self):
        field = NetValueField(allow_null=True)
        self.assertEqual(field.to_representation(Decimal("100.0000")), "100")
        self.assertIsNone(field.to_representation(None))
        self.assertEqual(field.run_validation("12.5"), Decimal("12.500"))
        self.assertEqual(field.run_validation("1.2345"), Decimal("1.235"))
