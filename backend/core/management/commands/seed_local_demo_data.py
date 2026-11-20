from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from catalog.models import (
    Brand,
    MasterProduct,
    Product,
    ProductCategory,
    Tender,
    TenderAccess,
    TenderItem,
    TenderOffer,
    TenderStatus,
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
    Unit,
    User,
    UserRole,
    Venue,
    VenueAddress,
    VenueAddressType,
    VenueCart,
    VenueCartItem,
    VenueContact,
    VenueContactType,
    VenueUser,
    VenueUserRole,
)
from suppliers.models import (
    PriceStatus,
    PriceType,
    Supplier,
    SupplierProduct,
    SupplierProductPrice,
    SupplierUser,
    SupplierUserRole,
)


class Command(BaseCommand):
    help = "Create local-only demo accounts, products, a venue cart, and a tender."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("This seed is local-only and can run only when DEBUG=True.")

        call_command("seed_demo_data", verbosity=0)
        supplier = Supplier.objects.get(inn="0000000000")
        buyer = self._seed_user(
            email="demo.venue@autosnab.local",
            full_name="Демо Закупщик",
            role=UserRole.VENUE_USER,
        )
        supplier_user = self._seed_user(
            email="demo.supplier@autosnab.local",
            full_name="Демо Поставщик",
            role=UserRole.SUPPLIER,
        )
        self._seed_supplier_membership(supplier=supplier, user=supplier_user)
        venue = self._seed_venue(user=buyer)
        products = self._seed_products(supplier=supplier)
        master_products = self._seed_venue_directory(owner=buyer, venue=venue, products=products)
        self._seed_cart(user=buyer, venue=venue, products=products)
        self._seed_tender(
            owner=buyer,
            venue=venue,
            supplier=supplier,
            products=products,
            master_products=master_products,
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Local demo data ready: 2 login accounts, 1 supplier, 1 venue, "
                f"{len(products)} supplier products, {len(master_products)} venue directory items, "
                "a populated cart, and an active tender."
            )
        )
        self.stdout.write("Venue login: demo.venue@autosnab.local / LocalDemo2026!")
        self.stdout.write("Supplier login: demo.supplier@autosnab.local / LocalDemo2026!")

    def _seed_user(self, *, email, full_name, role):
        user, _ = User.objects.update_or_create(
            email=email,
            role=role,
            defaults={
                "full_name": full_name,
                "first_name": full_name.split()[0],
                "last_name": " ".join(full_name.split()[1:]),
                "status": EntityStatus.ACTIVE,
                "password": make_password("LocalDemo2026!"),
            },
        )
        return user

    def _seed_supplier_membership(self, *, supplier, user):
        SupplierUser.objects.update_or_create(
            supplier=supplier,
            user=user,
            defaults={"role": SupplierUserRole.OWNER, "status": EntityStatus.ACTIVE},
        )

    def _seed_venue(self, *, user):
        venue, _ = Venue.objects.update_or_create(
            display_name="Демо Кафе АвтоСнаб",
            defaults={
                "legal_name": "ООО Демо Кафе",
                "short_name": "ООО Демо Кафе",
                "entity_type": "organization",
                "legal_address": "Демо-адрес, Минск",
                "phone": "+375 00 000-00-00",
                "email": "demo.venue@autosnab.local",
                "description": "Тестовая компания для локальной разработки AutoSnab.",
                "metadata": {"seed": "seed_local_demo_data", "demo": True},
                "status": EntityStatus.ACTIVE,
            },
        )
        if user.venue_id != venue.id:
            user.venue = venue
            user.save(update_fields=("venue", "updated_at"))
        VenueUser.objects.update_or_create(
            venue=venue,
            user=user,
            defaults={"role": VenueUserRole.OWNER, "status": EntityStatus.ACTIVE},
        )
        VenueAddress.objects.update_or_create(
            venue=venue,
            address_type=VenueAddressType.ACTUAL,
            defaults={
                "address": "Тестовый адрес, Минск",
                "country": "Беларусь",
                "locality": "Минск",
                "street": "Демо-проспект, 1",
            },
        )
        VenueContact.objects.update_or_create(
            venue=venue,
            contact_type=VenueContactType.PHONE,
            value="+375 00 000-00-00",
            defaults={"value": "+375 00 000-00-00"},
        )
        VenueContact.objects.update_or_create(
            venue=venue,
            contact_type=VenueContactType.EMAIL,
            value="demo.venue@autosnab.local",
            defaults={"value": "demo.venue@autosnab.local"},
        )
        return venue

    def _seed_products(self, *, supplier):
        kg = Unit.objects.get(code="кг")
        liter = Unit.objects.get(code="л")
        piece = Unit.objects.get(code="шт")
        brand, _ = Brand.objects.update_or_create(
            normalized_name="demo-brand",
            defaults={"canonical_name": "Demo Brand", "status": EntityStatus.ACTIVE},
        )
        category, _ = ProductCategory.objects.update_or_create(
            slug="demo-grocery",
            defaults={"name": "Демо бакалея", "status": EntityStatus.ACTIVE},
        )
        specs = [
            (
                "DEMO-FLOUR-25",
                "Мука пшеничная высший сорт 25 кг",
                "Мука пшеничная",
                "мешок 25 кг",
                kg,
                Decimal("1490.00"),
                Decimal("25"),
                "Россия",
            ),
            (
                "DEMO-OIL-5",
                "Масло подсолнечное рафинированное 5 л",
                "Масло подсолнечное",
                "канистра 5 л",
                liter,
                Decimal("640.00"),
                Decimal("5"),
                "Россия",
            ),
            (
                "DEMO-SUGAR-10",
                "Сахар песок 10 кг",
                "Сахар песок",
                "мешок 10 кг",
                kg,
                Decimal("990.00"),
                Decimal("10"),
                "Россия",
            ),
            (
                "DEMO-RICE-5",
                "Рис круглозёрный 5 кг",
                "Рис круглозёрный",
                "пакет 5 кг",
                kg,
                Decimal("520.00"),
                Decimal("5"),
                "Россия",
            ),
            (
                "DEMO-BUCKWHEAT-5",
                "Крупа гречневая 5 кг",
                "Крупа гречневая",
                "пакет 5 кг",
                kg,
                Decimal("465.00"),
                Decimal("5"),
                "Россия",
            ),
            (
                "DEMO-PASTA-5",
                "Макароны перья 5 кг",
                "Макароны перья",
                "коробка 5 кг",
                kg,
                Decimal("575.00"),
                Decimal("5"),
                "Россия",
            ),
            (
                "DEMO-MILK-1",
                "Молоко пастеризованное 3.2% 1 л",
                "Молоко 3.2%",
                "бутылка 1 л",
                liter,
                Decimal("88.00"),
                Decimal("1"),
                "Беларусь",
            ),
            (
                "DEMO-CREAM-1",
                "Сливки 20% 1 л",
                "Сливки 20%",
                "пакет 1 л",
                liter,
                Decimal("215.00"),
                Decimal("1"),
                "Беларусь",
            ),
            (
                "DEMO-CHEESE-1",
                "Сыр Гауда 1 кг",
                "Сыр Гауда",
                "головка 1 кг",
                kg,
                Decimal("890.00"),
                Decimal("1"),
                "Беларусь",
            ),
            (
                "DEMO-TOMATO-2",
                "Томаты консервированные 2.5 кг",
                "Томаты консервированные",
                "банка 2.5 кг",
                kg,
                Decimal("310.00"),
                Decimal("1"),
                "Италия",
            ),
            (
                "DEMO-EGGS-30",
                "Яйцо куриное С1 30 шт",
                "Яйцо куриное С1",
                "лоток 30 шт",
                piece,
                Decimal("420.00"),
                Decimal("30"),
                "Беларусь",
            ),
            (
                "DEMO-COFFEE-1",
                "Кофе зерновой 1 кг",
                "Кофе зерновой",
                "пакет 1 кг",
                kg,
                Decimal("1680.00"),
                Decimal("1"),
                "Бразилия",
            ),
        ]

        products = []
        for sku, supplier_name, canonical_name, package, unit, price, multiple, country in specs:
            product, _ = Product.objects.update_or_create(
                normalized_name=sku.lower(),
                brand=brand,
                category=category,
                package=package,
                defaults={
                    "canonical_name": canonical_name,
                    "base_unit": unit,
                    "enriched_text": f"{canonical_name} {package} {country}",
                    "status": EntityStatus.ACTIVE,
                },
            )
            supplier_product, _ = SupplierProduct.objects.update_or_create(
                supplier=supplier,
                supplier_sku=sku,
                defaults={
                    "supplier_name": supplier_name,
                    "normalized_name": canonical_name.lower(),
                    "product": product,
                    "unit": unit,
                    "brand": brand,
                    "category": category,
                    "country_of_origin": country,
                    "availability": "in_stock",
                    "is_promo": sku in {"DEMO-MILK-1", "DEMO-TOMATO-2"},
                    "is_new": sku == "DEMO-COFFEE-1",
                    "package": package,
                    "net_value": multiple,
                    "net_unit": unit,
                    "min_order_qty": multiple,
                    "delivery_multiple": multiple,
                    "comment": "Синтетическая демонстрационная позиция.",
                    "status": EntityStatus.ACTIVE,
                },
            )
            active_price = (
                SupplierProductPrice.objects.filter(
                    supplier_product=supplier_product,
                    price_type=PriceType.PRICE_LIST,
                    status=PriceStatus.ACTIVE,
                    valid_to__isnull=True,
                )
                .order_by("-valid_from")
                .first()
            )
            if active_price is None:
                active_price = SupplierProductPrice.objects.create(
                    supplier_product=supplier_product,
                    price_type=PriceType.PRICE_LIST,
                    price=price,
                    currency="RUB",
                    source_document="Локальный демонстрационный прайс",
                    status=PriceStatus.ACTIVE,
                )
            elif active_price.price != price or active_price.currency != "RUB":
                active_price.price = price
                active_price.currency = "RUB"
                active_price.save(update_fields=("price", "currency", "updated_at"))
            supplier_product.current_price = price
            supplier_product.save(update_fields=("current_price", "updated_at"))
            products.append((supplier_product, price, unit))

        self._seed_price_history(products)
        return products

    def _seed_price_history(self, products):
        supplier_product, current_price, _unit = products[0]
        old_price_exists = SupplierProductPrice.objects.filter(
            supplier_product=supplier_product,
            status=PriceStatus.SUPERSEDED,
            comment="Демо-история изменения цены",
        ).exists()
        if old_price_exists:
            supplier_product.previous_price = current_price - Decimal("50.00")
            supplier_product.save(update_fields=("previous_price", "updated_at"))
            return
        active_price = (
            SupplierProductPrice.objects.filter(
                supplier_product=supplier_product,
                status=PriceStatus.ACTIVE,
                valid_to__isnull=True,
            )
            .order_by("-valid_from")
            .first()
        )
        now = timezone.now()
        SupplierProductPrice.objects.create(
            supplier_product=supplier_product,
            price_type=PriceType.PRICE_LIST,
            price=current_price - Decimal("50.00"),
            currency="RUB",
            source_document="Локальный демонстрационный прайс, предыдущий выпуск",
            status=PriceStatus.SUPERSEDED,
            valid_from=now - timedelta(days=14),
            valid_to=now - timedelta(days=7),
            comment="Демо-история изменения цены",
        )
        supplier_product.previous_price = current_price - Decimal("50.00")
        supplier_product.save(update_fields=("previous_price", "updated_at"))
        if active_price and active_price.valid_from < now - timedelta(days=7):
            active_price.valid_from = now - timedelta(days=7)
            active_price.save(update_fields=("valid_from", "updated_at"))

    def _seed_venue_directory(self, *, owner, venue, products):
        entries = []
        for supplier_product, _price, unit in products:
            master, _ = MasterProduct.objects.get_or_create(
                owner=owner,
                canonical_name=supplier_product.product.canonical_name,
                defaults={"unit": unit, "status": EntityStatus.ACTIVE},
            )
            matrix_item, _ = VenueProductMatrixItem.objects.update_or_create(
                venue=venue,
                master_product=master,
                defaults={
                    "requested_name": supplier_product.product.canonical_name,
                    "requested_unit_label": unit.code,
                    "supplier_product": supplier_product,
                    "source": "manual",
                    "status": EntityStatus.ACTIVE,
                    "needs_review": False,
                },
            )
            entries.append((master, matrix_item))
        return [master for master, _item in entries]

    def _seed_cart(self, *, user, venue, products):
        cart, _ = VenueCart.objects.update_or_create(
            user=user,
            venue=venue,
            defaults={"comment": "Демо-корзина для проверки оформления заказа"},
        )
        for supplier_product, _price, unit in products[:3]:
            VenueCartItem.objects.update_or_create(
                cart=cart,
                supplier_product=supplier_product,
                defaults={
                    "requested_name": supplier_product.supplier_name,
                    "requested_qty": supplier_product.min_order_qty or Decimal("1"),
                    "requested_unit": unit,
                    "comment": "Демо-позиция. Количество можно изменить.",
                },
            )

    def _seed_tender(self, *, owner, venue, supplier, products, master_products):
        tender, _ = Tender.objects.update_or_create(
            created_by=owner,
            name="Демо-тендер на продукты для кафе",
            defaults={
                "description": "Тестовый тендер AutoSnab. Все позиции и цены синтетические.",
                "access": TenderAccess.OPEN,
                "status": TenderStatus.ACTIVE,
                "accepts_offers_until": timezone.now() + timedelta(days=30),
            },
        )
        tender.invited_suppliers.add(supplier)
        pairs = zip(master_products[:3], products[:3])
        for index, (master, (supplier_product, price, _unit)) in enumerate(pairs):
            tender_item, _ = TenderItem.objects.get_or_create(
                tender=tender,
                master_product=master,
                defaults={
                    "product_kind_label": "Продукты",
                    "comment": "Демо-позиция тендера",
                    "published": True,
                },
            )
            tender_item.venues.add(venue)
            offer, _ = TenderOffer.objects.update_or_create(
                tender_item=tender_item,
                supplier=supplier,
                defaults={
                    "supplier_product": supplier_product,
                    "price": price + Decimal(index * 10),
                    "currency": "RUB",
                    "comment": "Демо-предложение поставщика",
                },
            )
            offer.venues.add(venue)
