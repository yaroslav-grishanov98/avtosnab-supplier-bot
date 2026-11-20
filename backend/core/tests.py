import uuid
from unittest.mock import patch

from django.contrib import admin
from django.core.management import call_command
from django.test import TestCase

from catalog.models import (
    Brand,
    GoogleSheetSyncLog,
    Product,
    ProductCategory,
    ProductEmbedding,
    ProductMapping,
    VenueSupplierProduct,
)
from core.audit import record_audit_log
from core.models import (
    AuditActorType,
    AuditLogAction,
    AuditLogEntityType,
    Order,
    OrderItem,
    Receiving,
    ReceivingDocument,
    Unit,
    User,
    UserRole,
    Venue,
)
from suppliers.models import (
    BotUserBinding,
    PriceStatus,
    SourceChannel,
    Supplier,
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    SupplierProduct,
    SupplierProductPrice,
    SupplierUser,
)


class AdminRegistrationTests(TestCase):
    def test_domain_models_are_registered_in_admin(self):
        registered_models = {
            Supplier,
            SupplierUser,
            BotUserBinding,
            SupplierPriceUpload,
            SupplierPriceUploadItem,
            SupplierProduct,
            SupplierProductPrice,
            Unit,
            Product,
            Brand,
            ProductCategory,
            ProductEmbedding,
            Venue,
            Order,
            OrderItem,
            Receiving,
            ReceivingDocument,
            ProductMapping,
            VenueSupplierProduct,
            GoogleSheetSyncLog,
        }

        for model in registered_models:
            with self.subTest(model=model.__name__):
                self.assertIn(model, admin.site._registry)


class SeedDemoDataCommandTests(TestCase):
    def test_seed_command_creates_demo_data(self):
        call_command("seed_demo_data", verbosity=0)

        self.assertEqual(Unit.objects.filter(code__in=["кг", "г", "л", "мл", "шт"]).count(), 5)
        self.assertEqual(Supplier.objects.filter(display_name="Демо Поставщик").count(), 1)
        self.assertEqual(Brand.objects.filter(normalized_name="demo-brand").count(), 1)
        self.assertEqual(ProductCategory.objects.filter(slug="demo-grocery").count(), 1)
        self.assertEqual(Product.objects.filter(normalized_name__startswith="demo-").count(), 3)
        self.assertEqual(SupplierProduct.objects.filter(supplier_sku__startswith="DEMO-").count(), 3)
        self.assertEqual(
            BotUserBinding.objects.filter(channel=SourceChannel.TELEGRAM, chat_id="demo-telegram-chat").count(), 1
        )

    def test_repeated_seed_does_not_create_duplicates(self):
        call_command("seed_demo_data", verbosity=0)
        counts_after_first_seed = self._demo_counts()

        call_command("seed_demo_data", verbosity=0)

        self.assertEqual(self._demo_counts(), counts_after_first_seed)

    def test_seed_command_creates_active_prices(self):
        call_command("seed_demo_data", verbosity=0)

        supplier_products = SupplierProduct.objects.filter(supplier_sku__startswith="DEMO-")
        active_prices = SupplierProductPrice.objects.filter(
            supplier_product__in=supplier_products,
            status=PriceStatus.ACTIVE,
            valid_to__isnull=True,
        )

        self.assertEqual(active_prices.count(), 3)
        self.assertEqual({price.currency for price in active_prices}, {"RUB"})

    def test_seed_command_creates_env_telegram_binding(self):
        with patch.dict(
            "os.environ",
            {
                "DEMO_TELEGRAM_CHAT_ID": "768372240",
                "DEMO_TELEGRAM_EXTERNAL_USER_ID": "768372240",
                "DEMO_TELEGRAM_USERNAME": "ViktorShadr",
            },
        ):
            call_command("seed_demo_data", verbosity=0)

        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="768372240")
        self.assertEqual(binding.supplier.display_name, "Демо Поставщик")
        self.assertEqual(binding.external_user_id, "768372240")
        self.assertEqual(binding.username, "ViktorShadr")
        self.assertEqual(binding.metadata["source"], "env")

    def _demo_counts(self):
        return {
            "units": Unit.objects.filter(code__in=["кг", "г", "л", "мл", "шт"]).count(),
            "suppliers": Supplier.objects.filter(display_name="Демо Поставщик").count(),
            "brands": Brand.objects.filter(normalized_name="demo-brand").count(),
            "categories": ProductCategory.objects.filter(slug="demo-grocery").count(),
            "products": Product.objects.filter(normalized_name__startswith="demo-").count(),
            "supplier_products": SupplierProduct.objects.filter(supplier_sku__startswith="DEMO-").count(),
            "bot_bindings": BotUserBinding.objects.filter(
                channel=SourceChannel.TELEGRAM, chat_id="demo-telegram-chat"
            ).count(),
            "active_prices": SupplierProductPrice.objects.filter(
                supplier_product__supplier_sku__startswith="DEMO-",
                status=PriceStatus.ACTIVE,
                valid_to__isnull=True,
            ).count(),
        }


class RecordAuditLogTests(TestCase):
    def test_core_user_actor(self):
        user = User.objects.create(email="owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        entity_id = uuid.uuid4()
        log = record_audit_log(
            actor=user,
            action=AuditLogAction.DELETED,
            entity_type=AuditLogEntityType.MASTER_PRODUCT,
            entity_id=entity_id,
            summary="Сахар",
            scope_id=user.id,
        )
        self.assertEqual(log.actor_type, AuditActorType.CORE_USER)
        self.assertEqual(log.actor_id, user.id)
        self.assertIn("owner@example.com", log.actor_label)
        self.assertEqual(log.entity_id, entity_id)
        self.assertEqual(log.scope_id, user.id)

    def test_supplier_actor(self):
        supplier = Supplier.objects.create(legal_name="ООО Ромашка", display_name="Ромашка")
        log = record_audit_log(
            actor=supplier,
            action=AuditLogAction.ARCHIVED,
            entity_type=AuditLogEntityType.SUPPLIER_PRODUCT,
            entity_id=uuid.uuid4(),
            summary="Мука",
        )
        self.assertEqual(log.actor_type, AuditActorType.SUPPLIER)
        self.assertEqual(log.actor_id, supplier.id)
        self.assertEqual(log.actor_label, "Ромашка")

    def test_venue_actor(self):
        venue = Venue.objects.create(display_name="Кафе Тест")
        log = record_audit_log(
            actor=venue,
            action=AuditLogAction.ARCHIVED,
            entity_type=AuditLogEntityType.VENUE,
            entity_id=venue.id,
            summary="Кафе Тест",
        )
        self.assertEqual(log.actor_type, AuditActorType.VENUE)
        self.assertEqual(log.actor_id, venue.id)

    def test_no_identifiable_actor_is_service(self):
        log = record_audit_log(
            actor=None,
            action=AuditLogAction.ARCHIVED,
            entity_type=AuditLogEntityType.VENUE,
            entity_id=uuid.uuid4(),
            summary="Кафе Тест",
        )
        self.assertEqual(log.actor_type, AuditActorType.SERVICE)
        self.assertIsNone(log.actor_id)

    def test_anonymous_user_is_service(self):
        """OptionalCoreUserJWTAuthentication leaves request.user as
        AnonymousUser (not None) on a pure service-token call — must not be
        mistaken for a core.User."""
        from django.contrib.auth.models import AnonymousUser

        log = record_audit_log(
            actor=AnonymousUser(),
            action=AuditLogAction.DELETED,
            entity_type=AuditLogEntityType.MASTER_PRODUCT,
            entity_id=uuid.uuid4(),
            summary="Сахар",
        )
        self.assertEqual(log.actor_type, AuditActorType.SERVICE)
