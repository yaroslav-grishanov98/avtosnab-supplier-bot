import unittest

from handlers.catalog import validate_product_lines


class ProductTextValidationTests(unittest.TestCase):
    def test_accepts_name_and_positive_price(self):
        lines, errors = validate_product_lines("Молоко 3,2% — 95 руб/л\nСыр Российский — 720,50 ₽")
        self.assertEqual(len(lines), 2)
        self.assertEqual(errors, [])

    def test_reports_line_without_price(self):
        _, errors = validate_product_lines("Молоко без цены")
        self.assertEqual(errors, [(1, "не найдена цена в конце строки")])

    def test_reports_missing_name_and_non_positive_price(self):
        _, errors = validate_product_lines("100 руб\nГайка — 0 руб")
        self.assertEqual(errors[0], (1, "не указано название товара"))
        self.assertEqual(errors[1], (2, "цена должна быть больше нуля"))

    def test_limits_batch_size(self):
        text = "\n".join(f"Товар {number} — 10 руб" for number in range(1001))
        _, errors = validate_product_lines(text)
        self.assertEqual(errors, [(1001, "за один раз можно отправить не более 1000 позиций")])


if __name__ == "__main__":
    unittest.main()
