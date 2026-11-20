from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from api.v1.catalog import approval
from api.v1.catalog.services import add_cart_item, get_or_create_cart
from api.v1.catalog.tests import _jwt_bearer, _make_supplier_product, _make_venue_owner
from core.models import Order, User, UserRole, Venue, VenueAddress, VenueSubdivision, VenueUser
from notifications.models import NotificationDelivery


@override_settings(SERVICE_API_TOKEN="test-token")
class ApprovalTests(TestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe")
        self.author = _make_venue_owner(self.venue)
        self.second = User.objects.create(email="second@example.com", full_name="Second", role=UserRole.VENUE_USER)
        self.member = VenueUser.objects.create(venue=self.venue, user=self.second, role="manager")
        self.first = VenueUser.objects.get(venue=self.venue, user=self.author)
        self.product = _make_supplier_product(price="100.00")
        self.cart = get_or_create_cart(self.author, self.venue)
        add_cart_item(cart=self.cart, supplier_product=self.product, requested_name="Product", requested_qty=10)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.author))
        self.submit_url = f"/api/v1/catalog/venues/{self.venue.pk}/cart/submit"

    def configure(self, kind="sequential"):
        return approval.update_settings(
            self.venue.pk,
            self.author,
            {
                "mode": "approval",
                "approval_type": kind,
                "approver_membership_ids": [self.first.pk, self.member.pk],
                "expected_version": 1,
            },
        )

    def submit(self, version=1, **extra):
        response = self.client.post(
            self.submit_url, {"command_id": str(uuid4()), "expected_settings_version": version, **extra}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        return Order.objects.get(pk=response.data["id"])

    def command(self, order, action, actor=None, **extra):
        order.refresh_from_db()
        actor = actor or self.author
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(actor))
        cycle = approval.latest_cycle(order)
        payload = {
            "command_id": str(uuid4()),
            "expected_revision": order.revision,
            "expected_cycle_id": str(cycle.pk),
            **extra,
        }
        url = f"/api/v1/catalog/venues/{self.venue.pk}/orders/{order.pk}"
        return (
            self.client.patch(url + "/rework", payload, format="json")
            if action == "rework"
            else self.client.post(url + "/approval/" + action, payload, format="json")
        )

    def test_direct_and_required_fields(self):
        bad = self.client.post(self.submit_url, {}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(Order.objects.exists())
        order = self.submit()
        self.assertEqual(order.status, "submitted")
        self.assertEqual(order.created_by_user, self.author)
        self.assertEqual(order.supplier_requests.count(), 1)

    def test_sequential_no_dispatch_until_last(self):
        self.configure()
        order = self.submit(2)
        self.assertEqual(order.status, "awaiting_dispatch")
        self.assertFalse(order.supplier_requests.exists())
        self.assertFalse(NotificationDelivery.objects.exists())
        denied = self.command(order, "approve", self.second)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.command(order, "approve").status_code, 200)
        self.assertFalse(order.supplier_requests.exists())
        self.assertEqual(self.command(order, "approve", self.second).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.approval_status, "approved")
        self.assertEqual(order.supplier_requests.count(), 1)

    def test_one_of_cancel_others(self):
        self.configure("one_of")
        order = self.submit(2)
        self.assertEqual(self.command(order, "approve", self.second).status_code, 200)
        cycle = approval.latest_cycle(order)
        self.assertEqual(cycle.steps.filter(status="approved").count(), 1)
        self.assertEqual(cycle.steps.filter(status="cancelled").count(), 1)

    def test_revision_save_first(self):
        self.configure()
        order = self.submit(2)
        item = order.items.first()
        response = self.command(
            order, "save-and-approve", changes={"items": {"update": [{"id": str(item.pk), "requested_qty": "5"}]}}
        )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.revision, 2)
        cycle = approval.latest_cycle(order)
        self.assertEqual(cycle.order_revision, 2)
        self.assertEqual(cycle.steps.get(status="approved").order_revision, 2)
        self.assertEqual(order.revisions.count(), 2)

    def test_restart_rolls_back_without_confirmation(self):
        self.configure()
        order = self.submit(2)
        self.command(order, "approve")
        changes = {"fields": {"comment": "New"}}
        response = self.command(order, "save-and-approve", self.second, changes=changes)
        self.assertEqual(response.status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.revision, 1)
        response = self.command(
            order, "save-and-approve", self.second, changes=changes, confirm_restart=True, expected_settings_version=2
        )
        self.assertEqual(response.status_code, 200, response.data)
        cycle = approval.latest_cycle(order)
        self.assertEqual(cycle.cycle_number, 2)
        self.assertFalse(cycle.steps.filter(status="approved").exists())
        self.assertEqual(cycle.steps.get(status="active").user_id, self.author.pk)

    def test_rework_resubmit(self):
        self.configure()
        order = self.submit(2)
        self.assertEqual(self.command(order, "return-for-rework", comment="Fix quantity").status_code, 200)
        response = self.command(order, "rework", changes={"fields": {"comment": "Fixed"}})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.command(order, "resubmit", expected_settings_version=2).status_code, 200)
        self.assertEqual(order.approval_cycles.count(), 2)
        self.assertFalse(order.supplier_requests.exists())

    def test_snapshot_address(self):
        address = VenueAddress.objects.create(venue=self.venue, address_type="actual", address="Original")
        self.configure()
        order = self.submit(2, delivery_address_id=str(address.pk))
        address.address = "Changed"
        address.save()
        self.assertEqual(approval.detail(order, self.author)["delivery_address"]["address"], "Original")

    def test_revoked_legacy_user_cannot_change_settings(self):
        self.author.venue = self.venue
        self.author.save(update_fields=["venue"])
        self.first.status = "inactive"
        self.first.save(update_fields=["status"])
        response = self.client.put(
            f"/api/v1/venues/{self.venue.pk}/approval-settings",
            {"mode": "direct", "approval_type": None, "approver_membership_ids": [], "expected_version": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(approval.settings_for(self.venue.pk).version, 1)

    def test_clear_deleted_snapshot_references(self):
        self.configure()
        address = VenueAddress.objects.create(venue=self.venue, address_type="actual", address="Original")
        subdivision = VenueSubdivision.objects.create(venue=self.venue, name="Original")
        order = self.submit(2, delivery_address_id=str(address.pk), subdivision_id=str(subdivision.pk))
        item = order.items.get()
        self.assertEqual(item.subdivision_id, subdivision.pk)
        address.delete()
        subdivision.delete()
        response = self.command(
            order,
            "save-and-approve",
            changes={
                "fields": {"delivery_address_id": None, "subdivision_id": None},
                "items": {"update": [{"id": str(item.pk), "subdivision_id": None}]},
            },
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["revision"], 2)
        self.assertIsNone(response.data["delivery_address_id"])
        self.assertIsNone(response.data["delivery_address"])
        self.assertIsNone(response.data["subdivision_id"])
        self.assertIsNone(response.data["supplier_groups"][0]["items"][0]["subdivision_id"])

    def test_unchanged_deleted_reference_keeps_snapshot(self):
        self.configure()
        address = VenueAddress.objects.create(venue=self.venue, address_type="actual", address="Original")
        order = self.submit(2, delivery_address_id=str(address.pk))
        address_id = str(address.pk)
        address.delete()
        response = self.command(order, "save-and-approve", changes={})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["revision"], 1)
        self.assertEqual(response.data["delivery_address_id"], address_id)

    def test_retry_and_payload_conflict(self):
        payload = {"command_id": str(uuid4()), "expected_settings_version": 1}
        first = self.client.post(self.submit_url, payload, format="json")
        second = self.client.post(self.submit_url, payload, format="json")
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            self.client.post(
                self.submit_url, {**payload, "contacts": [{"full_name": "Other"}]}, format="json"
            ).status_code,
            409,
        )

    def test_dispatch_failure_rolls_back(self):
        self.configure("one_of")
        order = self.submit(2)
        with patch("api.v1.catalog.services._create_supplier_order_requests", side_effect=RuntimeError("fail")):
            with self.assertRaises(RuntimeError):
                self.command(order, "approve")
        order.refresh_from_db()
        self.assertEqual(order.approval_status, "pending")
        self.assertFalse(approval.latest_cycle(order).steps.filter(status="approved").exists())
        self.assertFalse(order.supplier_requests.exists())

    def test_noop_does_not_increment(self):
        self.configure()
        order = self.submit(2)
        response = self.command(order, "save-and-approve", changes={})
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.revision, 1)

    def test_stale_settings_revision_cycle(self):
        self.configure()
        bad = self.client.post(
            self.submit_url, {"command_id": str(uuid4()), "expected_settings_version": 1}, format="json"
        )
        self.assertEqual(bad.status_code, 409)
        self.assertFalse(Order.objects.exists())
        self.assertTrue(self.cart.items.exists())
        order = self.submit(2)
        response = self.command(order, "approve", expected_revision=99)
        self.assertEqual(response.status_code, 409)
        response = self.command(order, "approve", expected_cycle_id=str(uuid4()))
        self.assertEqual(response.status_code, 409)
        self.assertFalse(order.supplier_requests.exists())

    def test_settings_snapshot_and_restart_direct_conflict(self):
        self.configure()
        order = self.submit(2)
        cycle = approval.latest_cycle(order)
        approval.update_settings(
            self.venue.pk,
            self.author,
            {"mode": "direct", "approval_type": None, "approver_membership_ids": [], "expected_version": 2},
        )
        self.assertEqual(cycle.steps.count(), 2)
        self.assertEqual(self.command(order, "approve").status_code, 200)
        response = self.command(
            order,
            "save-and-approve",
            self.second,
            changes={"fields": {"comment": "Changed"}},
            confirm_restart=True,
            expected_settings_version=3,
        )
        self.assertEqual(response.status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.revision, 1)
        self.assertEqual(order.approval_cycles.count(), 1)

    def test_resubmit_direct_after_rework(self):
        self.configure()
        order = self.submit(2)
        self.command(order, "return-for-rework", comment="Fix")
        approval.update_settings(
            self.venue.pk,
            self.author,
            {"mode": "direct", "approval_type": None, "approver_membership_ids": [], "expected_version": 2},
        )
        response = self.command(order, "resubmit", expected_settings_version=3)
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "submitted")
        self.assertEqual(order.supplier_requests.count(), 1)
        self.assertEqual(order.approval_cycles.count(), 1)

    def test_inactive_member_cannot_decide(self):
        self.configure("one_of")
        order = self.submit(2)
        self.member.status = "inactive"
        self.member.save()
        response = self.command(order, "approve", self.second)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(order.supplier_requests.exists())

    def test_service_admin_and_cross_company_cannot_approve(self):
        self.configure("one_of")
        order = self.submit(2)
        admin = User.objects.create(email="admin@example.com", full_name="Admin", role="admin")
        self.assertEqual(self.command(order, "approve", admin).status_code, 403)
        outsider = User.objects.create(email="outsider@example.com", full_name="Outsider", role="venue_user")
        self.assertEqual(self.command(order, "approve", outsider).status_code, 403)
        cycle = approval.latest_cycle(order)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.post(
            f"/api/v1/catalog/venues/{self.venue.pk}/orders/{order.pk}/approval/approve",
            {"command_id": str(uuid4()), "expected_revision": 1, "expected_cycle_id": str(cycle.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_pending_legacy_mutations_and_supplier_visibility(self):
        self.configure()
        order = self.submit(2)
        item = order.items.first()
        from api.v1.catalog.services import (
            cancel_order_item,
            change_order_item_supplier,
            set_off_system_order_item_status,
        )

        self.assertFalse(cancel_order_item(item=item)[0])
        self.assertFalse(set_off_system_order_item_status(item=item, status="confirmed")[0])
        self.assertIsNone(change_order_item_supplier(item=item, new_supplier_product=self.product)[0])
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.get(f"/api/v1/catalog/suppliers/{self.product.supplier_id}/orders/{order.pk}")
        self.assertEqual(response.status_code, 404)

    def test_return_for_rework_comment_is_optional(self):
        self.configure()
        for extra in ({}, {"comment": ""}, {"comment": "   "}):
            order = self.submit(2)
            response = self.command(order, "return-for-rework", **extra)
            self.assertEqual(response.status_code, 200, response.data)
            order.refresh_from_db()
            self.assertEqual(order.approval_status, "rework")
            self.assertEqual(order.business_events.get(event="returned_for_rework").comment, "")
            self.add_cart_item_again()

    def add_cart_item_again(self):
        self.cart = get_or_create_cart(self.author, self.venue)
        add_cart_item(cart=self.cart, supplier_product=self.product, requested_name="Product", requested_qty=10)

    def test_cancel_pending_order(self):
        self.configure()
        order = self.submit(2)
        for actor, expected in ((self.author, True), (self.second, True)):
            self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(actor))
            url = f"/api/v1/catalog/venues/{self.venue.pk}/orders/{order.pk}"
            actions = self.client.get(url).data["approval"]["available_actions"]
            self.assertEqual("cancel" in actions, expected, actions)
        response = self.command(order, "cancel", comment="Ordered by mistake")
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual((order.status, order.approval_status), ("cancelled", "cancelled"))
        self.assertFalse(order.supplier_requests.exists())
        self.assertFalse(NotificationDelivery.objects.exists())
        cycle = approval.latest_cycle(order)
        self.assertEqual(cycle.status, "cancelled")
        self.assertFalse(cycle.steps.filter(status__in=["active", "waiting"]).exists())
        self.assertEqual(order.business_events.get(event="approval_cancelled").comment, "Ordered by mistake")

    def test_cancel_without_comment_and_idempotent(self):
        self.configure()
        order = self.submit(2)
        order.refresh_from_db()
        payload = {
            "command_id": str(uuid4()),
            "expected_revision": order.revision,
            "expected_cycle_id": str(approval.latest_cycle(order).pk),
        }
        url = f"/api/v1/catalog/venues/{self.venue.pk}/orders/{order.pk}/approval/cancel"
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 200)
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 200)
        self.assertEqual(order.business_events.filter(event="approval_cancelled").count(), 1)

    def test_cancel_after_return_for_rework(self):
        self.configure()
        order = self.submit(2)
        self.command(order, "return-for-rework", comment="Fix")
        self.assertEqual(self.command(order, "cancel").status_code, 200)
        order.refresh_from_db()
        self.assertEqual((order.status, order.approval_status), ("cancelled", "cancelled"))

    def test_cancel_permissions_and_states(self):
        self.configure()
        order = self.submit(2)
        outsider = User.objects.create(email="x@example.com", full_name="X", role=UserRole.VENUE_USER)
        self.assertIn(self.command(order, "cancel", outsider).status_code, (403, 404))
        self.assertEqual(self.command(order, "cancel", self.second).status_code, 200)  # manager of the venue
        self.assertEqual(self.command(order, "cancel").status_code, 409)  # already cancelled
        self.assertEqual(self.command(order, "approve").status_code, 409)
        self.add_cart_item_again()
        sent = self.submit(2)
        self.command(sent, "approve")
        self.command(sent, "approve", self.second)
        self.assertEqual(self.command(sent, "cancel").status_code, 409)  # already dispatched
        sent.refresh_from_db()
        self.assertEqual(sent.status, "submitted")

    def test_cancelled_order_leaves_list_and_badge(self):
        self.configure()
        order = self.submit(2)
        base = "/api/v1/catalog/venues/mine/approvals"
        self.assertEqual(self.client.get(base + "/count").data["count"], 1)
        self.command(order, "cancel")
        self.assertEqual(self.client.get(base + "/count").data["count"], 0)
        self.assertEqual(self.client.get(base + "?filter=pending").data["count"], 0)

    def test_bad_changes_rollback(self):
        self.configure()
        order = self.submit(2)
        for changes in (
            {"fields": {"price": "1"}},
            {"items": {"remove": [str(uuid4())]}},
            {"items": {"update": [{"id": str(order.items.first().pk), "applied_price": "1"}]}},
        ):
            response = self.command(order, "save-and-approve", changes=changes)
            self.assertEqual(response.status_code, 400, response.data)
        order.refresh_from_db()
        self.assertEqual(order.revision, 1)

    def test_list_badge_and_history(self):
        self.configure()
        order = self.submit(2)
        base = "/api/v1/catalog/venues/mine/approvals"
        self.assertEqual(self.client.get(base + "/count").data["count"], 1)
        row = self.client.get(base).data["results"][0]
        self.assertEqual(row["total_amount"], "1000.00")
        self.assertEqual(row["currency"], "RUB")
        self.assertEqual(self.client.get(base, {"filter": "requiring_my_decision"}).status_code, 400)
        self.command(order, "approve")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.author))
        self.assertEqual(self.client.get(base + "/count").data["count"], 0)
        history = self.client.get(f"/api/v1/catalog/venues/{self.venue.pk}/orders/{order.pk}/history").data["results"]
        self.assertEqual({x["event"] for x in history}, {"created", "submitted_for_approval", "step_approved"})

    def test_frozen_labels_survive_new_revision(self):
        address = VenueAddress.objects.create(venue=self.venue, address_type="actual", address="Original")
        self.configure()
        order = self.submit(2, delivery_address_id=str(address.pk))
        self.product.normalized_name = "Changed"
        self.product.save()
        address.address = "Changed"
        address.save()
        response = self.command(
            order,
            "save-and-approve",
            changes={"items": {"update": [{"id": str(order.items.first().pk), "requested_qty": "5"}]}},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["delivery_address"]["address"], "Original")
        self.assertEqual(response.data["supplier_groups"][0]["items"][0]["supplier_product_name"], "test-product")

    def test_settings_api_users_and_required_version(self):
        url = f"/api/v1/venues/{self.venue.pk}/approval-settings"
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.put(
            url,
            {
                "mode": "approval",
                "approval_type": "one_of",
                "approver_membership_ids": [str(self.first.pk)],
                "expected_version": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        users = self.client.get(f"/api/v1/venues/{self.venue.pk}/users")
        self.assertEqual(users.status_code, 200)
        self.assertTrue(users.data[0]["can_be_approver"])
        self.assertIn("membership_id", users.data[0])

    def test_repeated_decision_and_deleted_membership_history(self):
        self.configure("one_of")
        order = self.submit(2)
        cycle = approval.latest_cycle(order)
        payload = {"command_id": uuid4(), "expected_revision": 1, "expected_cycle_id": cycle.pk}
        approval.execute(order.pk, self.venue.pk, self.author, "approve", payload)
        approval.execute(order.pk, self.venue.pk, self.author, "approve", payload)
        self.assertEqual(cycle.steps.filter(status="approved").count(), 1)
        self.member.delete()
        self.assertEqual(cycle.steps.count(), 2)
        self.assertTrue(order.business_events.filter(event="approval_completed").exists())

    def test_empty_rework_cannot_be_resubmitted(self):
        self.configure()
        order = self.submit(2)
        self.command(order, "return-for-rework", comment="Fix")
        self.assertEqual(
            self.command(order, "rework", changes={"items": {"remove": [str(order.items.first().pk)]}}).status_code, 200
        )
        self.assertEqual(self.command(order, "resubmit", expected_settings_version=2).status_code, 400)
        self.assertEqual(order.approval_cycles.count(), 1)

    def test_notification_and_detail_use_approved_snapshot(self):
        address = VenueAddress.objects.create(venue=self.venue, address_type="actual", address="Original")
        self.configure("one_of")
        order = self.submit(2, delivery_address_id=str(address.pk))
        address.address = "Changed"
        address.save()
        with patch("notifications.services.enqueue_for_supplier") as notify:
            response = self.command(order, "approve")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(notify.call_args.kwargs["payload"]["delivery_address"], "Original")
        self.assertEqual(response.data["delivery_address"]["address"], "Original")

    def test_deleted_route_member_is_not_silently_removed(self):
        self.configure()
        self.member.delete()
        settings = approval.settings_for(self.venue.pk)
        self.assertEqual(settings.approvers.count(), 2)
        response = self.client.post(
            self.submit_url, {"command_id": str(uuid4()), "expected_settings_version": 2}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Order.objects.exists())
        self.assertTrue(self.cart.items.exists())

    def test_malformed_changes_are_400_and_do_not_mutate(self):
        self.configure()
        order = self.submit(2)
        for changes in [
            {"items": {"update": [{}]}},
            {"items": {"remove": ["bad-uuid"]}},
            {"items": {"add": "bad"}},
            {"fields": []},
            {"items": {"update": [False]}},
        ]:
            response = self.command(order, "save-and-approve", changes=changes)
            self.assertEqual(response.status_code, 400, response.data)
        order.refresh_from_db()
        self.assertEqual(order.revision, 1)

    def test_save_and_approve_retry_keeps_exactly_one_revision(self):
        self.configure()
        order = self.submit(2)
        cycle = approval.latest_cycle(order)
        item = order.items.first()
        payload = {
            "command_id": uuid4(),
            "expected_revision": 1,
            "expected_cycle_id": cycle.pk,
            "changes": {"items": {"update": [{"id": str(item.pk), "requested_qty": "5"}]}},
        }
        approval.execute(order.pk, self.venue.pk, self.author, "save-and-approve", payload)
        result = approval.execute(order.pk, self.venue.pk, self.author, "save-and-approve", payload)
        self.assertEqual(result.revision, 2)
        self.assertEqual(order.revisions.count(), 2)
        self.assertEqual(cycle.steps.filter(status="approved").count(), 1)

    def test_noop_contacts_and_order_fields(self):
        self.configure()
        order = self.submit(2, contacts=[{"full_name": "Contact"}])
        response = self.command(
            order,
            "save-and-approve",
            changes={"fields": {"contacts": [{"full_name": "Contact"}], "comment": order.comment}},
        )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.revision, 1)

    def test_author_and_history_survive_user_deletion(self):
        self.configure()
        order = self.submit(2)
        original_id = self.author.pk
        self.author.delete()
        order.refresh_from_db()
        result = approval.detail(order, self.second)
        self.assertEqual(result["author"]["user_id"], str(original_id))
        self.assertTrue(order.business_events.filter(actor_id=original_id).exists())
        self.assertEqual(approval.latest_cycle(order).steps.count(), 2)

    def test_migration_defaults_do_not_dispatch_or_guess_author(self):
        from importlib import import_module
        from types import SimpleNamespace

        from django.apps import apps
        from django.db import connection

        old = Order.objects.create(venue=self.venue, order_number="OLD-ORDER")
        migration = import_module("core.migrations.0044_initialize_approval_settings")
        migration.initialize(apps, SimpleNamespace(connection=connection))
        old.refresh_from_db()
        self.assertEqual(old.approval_status, "not_required")
        self.assertEqual(old.revision, 1)
        self.assertIsNone(old.created_by_user_id)
        self.assertFalse(old.approval_cycles.exists())
        self.assertFalse(old.supplier_requests.exists())
        self.assertIsNotNone(old.business_updated_at)
        self.assertEqual(approval.settings_for(self.venue.pk).mode, "direct")


class ApprovalConcurrencyTests(TransactionTestCase):
    setUp = ApprovalTests.setUp
    configure = ApprovalTests.configure
    submit = ApprovalTests.submit

    def test_decision_holds_membership_lock_until_commit(self):
        from django.db import OperationalError

        self.configure("one_of")
        order = self.submit(2)
        cycle = approval.latest_cycle(order)
        original = approval.active_step

        def attempt_revoke():
            try:
                with transaction.atomic():
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '200ms'")
                    VenueUser.objects.filter(pk=self.member.pk).update(status="inactive")
                return False
            except OperationalError as exc:
                self.assertEqual(exc.__cause__.sqlstate, "55P03")
                return True
            finally:
                connections.close_all()

        def checked(*args):
            step = original(*args)
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(attempt_revoke).result(timeout=5))
            return step

        with patch.object(approval, "active_step", side_effect=checked):
            approval.execute(
                order.pk,
                self.venue.pk,
                self.second,
                "approve",
                {"command_id": uuid4(), "expected_revision": 1, "expected_cycle_id": cycle.pk},
            )
        self.assertEqual(order.supplier_requests.count(), 1)
        VenueUser.objects.filter(pk=self.member.pk).update(status="inactive")

    def test_decision_rechecks_membership_after_waiting_for_revoke(self):
        from rest_framework.exceptions import PermissionDenied

        self.configure("one_of")
        order = self.submit(2)
        cycle = approval.latest_cycle(order)
        checking = Event()
        original = approval.active_step

        def checked(*args):
            checking.set()
            return original(*args)

        def decide():
            try:
                with self.assertRaises(PermissionDenied):
                    approval.execute(
                        order.pk,
                        self.venue.pk,
                        self.second,
                        "approve",
                        {"command_id": uuid4(), "expected_revision": 1, "expected_cycle_id": cycle.pk},
                    )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                VenueUser.objects.select_for_update().get(pk=self.member.pk)
                with patch.object(approval, "active_step", side_effect=checked):
                    future = pool.submit(decide)
                    self.assertTrue(checking.wait(timeout=5))
                    VenueUser.objects.filter(pk=self.member.pk).update(status="inactive")
            future.result(timeout=5)
        order.refresh_from_db()
        self.assertEqual(order.approval_status, "pending")
        self.assertFalse(order.supplier_requests.exists())

    def race(self, functions):
        barrier = Barrier(len(functions))

        def worker(fn):
            try:
                barrier.wait(timeout=10)
                return fn()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            futures = [pool.submit(worker, fn) for fn in functions]
            return [f.result(timeout=30) for f in futures]

    def test_competing_one_of_approvals(self):
        self.configure("one_of")
        order = self.submit(2)
        cycle = approval.latest_cycle(order)

        def fn(actor):
            return lambda: (
                approval.execute(
                    order.pk,
                    self.venue.pk,
                    User.objects.get(pk=actor.pk),
                    "approve",
                    {"command_id": uuid4(), "expected_revision": 1, "expected_cycle_id": cycle.pk},
                ).pk
            )

        self.assertEqual(self.race([fn(self.author), fn(self.second)]), [order.pk, order.pk])
        self.assertEqual(order.supplier_requests.count(), 1)
        self.assertEqual(cycle.steps.filter(status="approved").count(), 1)

    def test_approve_against_return(self):
        self.configure("one_of")
        order = self.submit(2)
        cycle = approval.latest_cycle(order)

        def fn(actor, op):
            return lambda: (
                approval.execute(
                    order.pk,
                    self.venue.pk,
                    User.objects.get(pk=actor.pk),
                    op,
                    {"command_id": uuid4(), "expected_revision": 1, "expected_cycle_id": cycle.pk, "comment": "Fix"},
                ).pk
            )

        self.race([fn(self.author, "approve"), fn(self.second, "return-for-rework")])
        order.refresh_from_db()
        self.assertIn(order.approval_status, ["approved", "rework"])
        self.assertEqual(order.supplier_requests.count(), int(order.approval_status == "approved"))
        self.assertEqual(cycle.steps.filter(status__in=["approved", "returned"]).count(), 1)

    def test_duplicate_submit(self):
        payload = {"command_id": uuid4(), "expected_settings_version": 1}

        def fn():
            return approval.submit(self.cart, User.objects.get(pk=self.author.pk), payload).pk

        results = self.race([fn, fn])
        self.assertEqual(results[0], results[1])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.first().supplier_requests.count(), 1)

    def test_settings_update_against_approval(self):
        self.configure("one_of")
        order = self.submit(2)
        cycle = approval.latest_cycle(order)

        def decide():
            return approval.execute(
                order.pk,
                self.venue.pk,
                User.objects.get(pk=self.second.pk),
                "approve",
                {"command_id": uuid4(), "expected_revision": 1, "expected_cycle_id": cycle.pk},
            ).pk

        def update():
            return approval.update_settings(
                self.venue.pk,
                User.objects.get(pk=self.author.pk),
                {"mode": "direct", "approval_type": None, "approver_membership_ids": [], "expected_version": 2},
            ).version

        self.race([decide, update])
        order.refresh_from_db()
        self.assertEqual(order.approval_status, "approved")
        self.assertEqual(order.supplier_requests.count(), 1)
        self.assertEqual(cycle.steps.count(), 2)

    def test_membership_deactivation_against_approval(self):
        self.configure("one_of")
        order = self.submit(2)
        cycle = approval.latest_cycle(order)

        def decide():
            from rest_framework.exceptions import PermissionDenied

            try:
                approval.execute(
                    order.pk,
                    self.venue.pk,
                    User.objects.get(pk=self.second.pk),
                    "approve",
                    {"command_id": uuid4(), "expected_revision": 1, "expected_cycle_id": cycle.pk},
                )
                return "approved"
            except PermissionDenied:
                return "denied"

        def deactivate():
            VenueUser.objects.filter(pk=self.member.pk).update(status="inactive")
            return "inactive"

        results = self.race([decide, deactivate])
        order.refresh_from_db()
        self.assertEqual(order.supplier_requests.count(), int(results[0] == "approved"))
        self.assertEqual(self.client.get("/api/v1/catalog/venues/mine/approvals/count").status_code, 200)
