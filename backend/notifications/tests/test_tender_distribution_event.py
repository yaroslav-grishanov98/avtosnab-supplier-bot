from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from notifications.models import NotificationEvent
from notifications.services import BUYER_EVENTS, SUPPLIER_EVENTS

EVENT = "supplier_tender_distribution"


class TenderDistributionEventTests(SimpleTestCase):
    def test_new_supplier_chats_get_the_event(self):
        self.assertIn(EVENT, NotificationEvent.values)
        self.assertIn(EVENT, SUPPLIER_EVENTS)
        self.assertNotIn(EVENT, BUYER_EVENTS)


class TenderDistributionEventMigrationTests(TransactionTestCase):
    """Supplier chats connected before the event existed get it enabled; buyer chats do not."""

    migrate_from = [("notifications", "0007_notificationdeliveryreceipt_result")]
    migrate_to = [("notifications", "0008_supplier_tender_distribution_event")]

    def setUp(self):
        self.addCleanup(self.restore_latest_schema)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("core", "User")
        Supplier = old_apps.get_model("suppliers", "Supplier")
        Destination = old_apps.get_model("notifications", "NotificationDestination")
        user = User.objects.create(email="notif-mig@example.com", full_name="Owner", role="venue_user")
        supplier = Supplier.objects.create(legal_name="Notif LLC", display_name="Notif Co")
        common = {"created_by": user, "channel": "telegram", "destination_type": "personal"}
        self.supplier_chat = Destination.objects.create(audience="supplier", supplier=supplier, chat_id="s-1", **common)
        self.inactive_chat = Destination.objects.create(
            audience="supplier", supplier=supplier, chat_id="s-2", is_active=False, **common
        )
        self.buyer_chat = Destination.objects.create(audience="buyer", chat_id="b-1", **common)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def restore_latest_schema(self):
        # Leave the database at the current graph for subsequent transaction
        # tests, including notification inbox consumers introduced after 0008.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_active_supplier_chats_get_the_rule(self):
        Rule = self.apps.get_model("notifications", "NotificationRule")
        chats_with_rule = set(
            Rule.objects.filter(event=EVENT, is_enabled=True).values_list("destination_id", flat=True)
        )
        self.assertEqual(chats_with_rule, {self.supplier_chat.pk})
