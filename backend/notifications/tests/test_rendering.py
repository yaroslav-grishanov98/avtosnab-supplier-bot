from types import SimpleNamespace

from django.test import SimpleTestCase

from notifications.tasks import render_message


class NotificationRenderingTests(SimpleTestCase):
    def test_daily_delivery_empty_state_is_explicit(self):
        delivery = SimpleNamespace(event="daily_delivery", payload={"date": "2026-09-25", "deliveries": []})

        text, keyboard = render_message(delivery)

        self.assertEqual(text, "На сегодня поставок нет.")
        self.assertIsNone(keyboard)

    def test_supplier_request_has_group_action_buttons(self):
        delivery = SimpleNamespace(
            event="supplier_new_request",
            destination=SimpleNamespace(channel="telegram", chat_id="-1001234567890"),
            payload={
                "request_id": "12345678-1234-1234-1234-123456789abc",
                "order_number": "ORD-1",
                "request_number": "REQ-1",
                "venue": "Кафе",
                "delivery_address": "Минск, Ленина 1",
                "total_amount": "1500.000000",
                "buyer_comment": "Позвонить при доставке",
                "supplier_id": "87654321-1234-1234-1234-123456789abc",
                "items": [
                    {
                        "name": "Картофель",
                        "quantity": "5.0000",
                        "unit": "кг",
                        "subdivision": "Кухня",
                    }
                ],
            },
        )

        text, keyboard = render_message(delivery)

        self.assertIn("Картофель — 5 кг", text)
        self.assertIn("Новая заявка №REQ-1", text)
        self.assertIn("Минск, Ленина 1", text)
        self.assertIn("Сумма: 1 500 ₽", text)
        self.assertIn("Комментарий заведения: Позвонить при доставке", text)
        self.assertEqual(keyboard[0][0]["callback_data"], "supplier_action:12345678-1234-1234-1234-123456789abc:accept")
        self.assertEqual(
            keyboard[2][0]["callback_data"], "supplier_action:12345678-1234-1234-1234-123456789abc:decline"
        )
        self.assertEqual(keyboard[3][0]["text"], "Открыть заявку")
        self.assertIn("startapp=", keyboard[3][0]["url"])

    def test_supplier_response_includes_delivery_details(self):
        delivery = SimpleNamespace(
            event="supplier_response",
            payload={
                "request_number": "REQ-1",
                "order_number": "ORD-1",
                "venue": "Кафе",
                "supplier": "Поставщик",
                "status_label": "Принято частично",
                "delivery_dates": ["2026-09-28"],
                "items": [
                    {
                        "name": "Картофель",
                        "quantity": "3",
                        "requested_quantity": "5",
                        "unit": "кг",
                    }
                ],
                "comment": "Остальное завтра",
            },
        )

        text, keyboard = render_message(delivery)

        self.assertIn("Ответ поставщика по заявке №REQ-1", text)
        self.assertIn("Дата поставки: 2026-09-28", text)
        self.assertIn("Картофель — 3 кг", text)
        self.assertIn("Комментарий поставщика: Остальное завтра", text)
        self.assertIsNone(keyboard)

    def test_buyer_order_notification_groups_items_by_supplier(self):
        delivery = SimpleNamespace(
            event="order_submitted",
            payload={"order_number": "ORD-1", "suppliers": {"Поставщик А": [{"name": "Молоко"}]}},
        )

        text, keyboard = render_message(delivery)

        self.assertIn("Поставщик А: Молоко", text)
        self.assertIsNone(keyboard)


class TenderDistributionRenderingTests(SimpleTestCase):
    """Raw tender ТЗ §32-33 (catalog docs/adr/0013): the product is chosen for venues; never «you won» or «order»."""

    def render(self, venues):
        delivery = SimpleNamespace(
            event="supplier_tender_distribution",
            payload={
                "tender_id": "12345678-1234-1234-1234-123456789abc",
                "tender_number": 15,
                "tender_name": "Молочка",
                "product_name": "Сливки Parmalat 35%",
                "price": "320.00",
                "currency": "RUB",
                "venues": venues,
            },
        )
        return render_message(delivery)

    def test_one_venue(self):
        text, keyboard = self.render(["Кафе 1"])

        self.assertEqual(
            text,
            "Ваш товар выбран для заведения\n"
            "Ваш товар «Сливки Parmalat 35%» добавлен в справочник Кафе 1.\n"
            "Цена 320 ₽ зафиксирована для этого заведения.\n"
            "Тендер №15 «Молочка».\n"
            "Это не является заказом.",
        )
        self.assertIsNone(keyboard)

    def test_several_venues(self):
        text, _ = self.render(["Кафе 1", "Кафе 2", "Кафе 3", "Кафе 4"])

        self.assertEqual(
            text,
            "Ваш товар выбран для 4 заведений\n"
            "Ваш товар «Сливки Parmalat 35%» добавлен в справочник заведений: Кафе 1, Кафе 2, Кафе 3, Кафе 4.\n"
            "Цена 320 ₽ зафиксирована для этих заведений.\n"
            "Тендер №15 «Молочка».\n"
            "Это не является заказом.",
        )

    def test_many_venues_are_cut_after_ten(self):
        text, _ = self.render([f"Кафе {n:02}" for n in range(1, 22)])

        self.assertIn("Ваш товар выбран для 21 заведения\n", text)
        self.assertIn("Кафе 10 и ещё 11.", text)
        self.assertNotIn("Кафе 11", text)
