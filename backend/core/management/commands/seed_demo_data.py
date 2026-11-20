import os
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Brand, Product, ProductCategory
from core.models import EntityStatus, Unit, UnitKind
from suppliers.models import BotUserBinding, PriceStatus, SourceChannel, Supplier, SupplierProduct, SupplierProductPrice


class Command(BaseCommand):
    help = "Create idempotent demo data for Supplier Catalog MVP."

    @transaction.atomic
    def handle(self, *args, **options):
        units = self._seed_units()
        supplier = self._seed_supplier()
        brand = self._seed_brand()
        category = self._seed_category()
        products = self._seed_products(units=units, brand=brand, category=category)
        supplier_products = self._seed_supplier_products(supplier=supplier, units=units, products=products)
        self._seed_prices(supplier_products=supplier_products)
        self._seed_bot_binding(supplier=supplier)

        if options["verbosity"] > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "Demo data ready: "
                    f"{len(units)} units, 1 supplier, 1 brand, 1 category, "
                    f"{len(products)} products, {len(supplier_products)} supplier products."
                )
            )

    def _seed_units(self):
        unit_specs = [
            ("кг", "Килограмм", UnitKind.WEIGHT, Decimal("1.000000")),
            ("г", "Грамм", UnitKind.WEIGHT, Decimal("0.001000")),
            ("л", "Литр", UnitKind.VOLUME, Decimal("1.000000")),
            ("мл", "Миллилитр", UnitKind.VOLUME, Decimal("0.001000")),
            ("шт", "Штука", UnitKind.PIECE, Decimal("1.000000")),
        ]

        units = {}
        for code, name, kind, ratio_to_base in unit_specs:
            unit, _ = Unit.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "kind": kind,
                    "ratio_to_base": ratio_to_base,
                    "status": EntityStatus.ACTIVE,
                },
            )
            units[code] = unit
        return units

    def _seed_supplier(self):
        supplier, _ = Supplier.objects.update_or_create(
            inn="0000000000",
            defaults={
                "legal_name": "ООО Демо Поставщик",
                "display_name": "Демо Поставщик",
                "phone": "+7 000 000-00-00",
                "email": "demo-supplier@example.com",
                "status": EntityStatus.ACTIVE,
                "delivery_terms": "Демо-доставка на следующий день",
                "delivery_min_order_amount": Decimal("5000.00"),
                "metadata": {"seed": "seed_demo_data"},
            },
        )
        return supplier

    def _seed_brand(self):
        brand, _ = Brand.objects.update_or_create(
            normalized_name="demo-brand",
            defaults={
                "canonical_name": "Demo Brand",
                "status": EntityStatus.ACTIVE,
            },
        )
        return brand

    def _seed_category(self):
        category, _ = ProductCategory.objects.update_or_create(
            slug="demo-grocery",
            defaults={
                "name": "Демо бакалея",
                "status": EntityStatus.ACTIVE,
            },
        )
        return category

    def _seed_products(self, *, units, brand, category):
        product_specs = [
            {
                "normalized_name": "demo-flour",
                "canonical_name": "Мука пшеничная",
                "base_unit": units["кг"],
                "package": "мешок 25 кг",
                "enriched_text": "Мука пшеничная Demo Brand мешок 25 кг",
            },
            {
                "normalized_name": "demo-sunflower-oil",
                "canonical_name": "Масло подсолнечное",
                "base_unit": units["л"],
                "package": "канистра 5 л",
                "enriched_text": "Масло подсолнечное Demo Brand канистра 5 л",
            },
            {
                "normalized_name": "demo-sugar",
                "canonical_name": "Сахар песок",
                "base_unit": units["кг"],
                "package": "мешок 10 кг",
                "enriched_text": "Сахар песок Demo Brand мешок 10 кг",
            },
        ]

        products = {}
        for spec in product_specs:
            product, _ = Product.objects.update_or_create(
                normalized_name=spec["normalized_name"],
                brand=brand,
                category=category,
                package=spec["package"],
                defaults={
                    "canonical_name": spec["canonical_name"],
                    "base_unit": spec["base_unit"],
                    "enriched_text": spec["enriched_text"],
                    "status": EntityStatus.ACTIVE,
                },
            )
            products[spec["normalized_name"]] = product
        return products

    def _seed_supplier_products(self, *, supplier, units, products):
        supplier_product_specs = [
            {
                "supplier_sku": "DEMO-FLOUR-25",
                "supplier_name": "Мука пшеничная в/с 25 кг",
                "normalized_name": "demo-flour",
                "product": products["demo-flour"],
                "unit": units["кг"],
                "package": "мешок 25 кг",
                "min_order_qty": Decimal("25.0000"),
                "delivery_multiple": Decimal("25.0000"),
                "comment": "Demo seed item",
            },
            {
                "supplier_sku": "DEMO-OIL-5",
                "supplier_name": "Масло подсолнечное 5 л",
                "normalized_name": "demo-sunflower-oil",
                "product": products["demo-sunflower-oil"],
                "unit": units["л"],
                "package": "канистра 5 л",
                "min_order_qty": Decimal("5.0000"),
                "delivery_multiple": Decimal("5.0000"),
                "comment": "Demo seed item",
            },
            {
                "supplier_sku": "DEMO-SUGAR-10",
                "supplier_name": "Сахар песок 10 кг",
                "normalized_name": "demo-sugar",
                "product": products["demo-sugar"],
                "unit": units["кг"],
                "package": "мешок 10 кг",
                "min_order_qty": Decimal("10.0000"),
                "delivery_multiple": Decimal("10.0000"),
                "comment": "Demo seed item",
            },
        ]

        supplier_products = {}
        for spec in supplier_product_specs:
            supplier_product, _ = SupplierProduct.objects.update_or_create(
                supplier=supplier,
                supplier_sku=spec["supplier_sku"],
                defaults={
                    "supplier_name": spec["supplier_name"],
                    "normalized_name": spec["normalized_name"],
                    "product": spec["product"],
                    "unit": spec["unit"],
                    "package": spec["package"],
                    "min_order_qty": spec["min_order_qty"],
                    "delivery_multiple": spec["delivery_multiple"],
                    "comment": spec["comment"],
                    "status": EntityStatus.ACTIVE,
                },
            )
            supplier_products[spec["supplier_sku"]] = supplier_product
        return supplier_products

    def _seed_prices(self, *, supplier_products):
        price_specs = {
            "DEMO-FLOUR-25": Decimal("1450.00"),
            "DEMO-OIL-5": Decimal("620.00"),
            "DEMO-SUGAR-10": Decimal("980.00"),
        }

        for supplier_sku, price in price_specs.items():
            supplier_product = supplier_products[supplier_sku]
            active_price = (
                SupplierProductPrice.objects.filter(
                    supplier_product=supplier_product,
                    status=PriceStatus.ACTIVE,
                    valid_to__isnull=True,
                )
                .order_by("-valid_from", "-created_at")
                .first()
            )

            if active_price:
                if active_price.price != price or active_price.currency != "RUB":
                    active_price.price = price
                    active_price.currency = "RUB"
                    active_price.save(update_fields=("price", "currency", "updated_at"))
                continue

            SupplierProductPrice.objects.create(
                supplier_product=supplier_product,
                price=price,
                currency="RUB",
                status=PriceStatus.ACTIVE,
            )

    def _seed_bot_binding(self, *, supplier):
        self._upsert_bot_binding(
            supplier=supplier,
            chat_id="demo-telegram-chat",
            external_user_id="demo-telegram-user",
            username="demo_supplier",
            metadata={"seed": "seed_demo_data", "demo": True},
        )

        demo_chat_id = os.environ.get("DEMO_TELEGRAM_CHAT_ID", "").strip()
        if demo_chat_id and demo_chat_id != "demo-telegram-chat":
            self._upsert_bot_binding(
                supplier=supplier,
                chat_id=demo_chat_id,
                external_user_id=os.environ.get("DEMO_TELEGRAM_EXTERNAL_USER_ID", "").strip() or None,
                username=os.environ.get("DEMO_TELEGRAM_USERNAME", "").strip() or None,
                metadata={"seed": "seed_demo_data", "source": "env"},
            )

    def _upsert_bot_binding(self, *, supplier, chat_id, external_user_id, username, metadata):
        BotUserBinding.objects.update_or_create(
            channel=SourceChannel.TELEGRAM,
            chat_id=chat_id,
            defaults={
                "supplier": supplier,
                "external_user_id": external_user_id,
                "username": username,
                "status": EntityStatus.ACTIVE,
                "metadata": metadata,
            },
        )
