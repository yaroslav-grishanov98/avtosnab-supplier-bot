"""Approval response contracts and batched list regressions."""

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from api.v1.catalog import approval, test_approval
from api.v1.catalog.approval_reads import approval_rows
from api.v1.catalog.services import add_cart_item
from api.v1.catalog.tests import _make_supplier_product
from core.models import OffSystemSupplier, Order, OrderApprovalCycle, OrderApprovalStep, OrderRevision, Venue, VenueUser


class ApprovalPayloadTests(TestCase):
    setUp = test_approval.ApprovalTests.setUp
    configure = test_approval.ApprovalTests.configure
    submit = test_approval.ApprovalTests.submit
    command = test_approval.ApprovalTests.command

    row_fields = {
        "id",
        "order_number",
        "venue_id",
        "venue_name",
        "author",
        "total_amount",
        "currency",
        "has_complete_prices",
        "created_at",
        "business_updated_at",
        "approval",
    }

    def items(self, data):
        flat = data["items"]
        grouped = [item for group in data["supplier_groups"] for item in group["items"]]
        self.assertEqual(len(flat), len({item["id"] for item in flat}))
        self.assertEqual(len(grouped), len({item["id"] for item in grouped}))
        self.assertEqual({item["id"]: item for item in flat}, {item["id"]: item for item in grouped})
        return flat

    def row(self):
        response = self.client.get("/api/v1/catalog/venues/mine/approvals", {"filter": "all"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), {"count", "limit", "offset", "results"})
        row = response.data["results"][0]
        self.assertEqual(set(row), self.row_fields)
        return row

    def test_list_never_builds_order_details(self):
        self.configure()
        order = self.submit(2)
        with patch.object(approval, "detail", side_effect=AssertionError("list must not build details")):
            row = self.row()
        self.assertEqual(row["id"], str(order.pk))
        self.assertEqual(row["total_amount"], "1000.00")
        self.assertEqual(row["approval"], approval.approval_data(order, self.author))
        self.assertEqual(row["created_at"], approval.detail(order, self.author)["created_at"])

    def test_snapshot_price_and_author_are_preserved(self):
        self.configure()
        order = self.submit(2)
        self.product.prices.update(price=Decimal("999"))
        self.author.full_name = "Changed name"
        self.author.save()
        row = self.row()
        saved = order.revisions.first().content
        self.assertEqual(row["total_amount"], "1000.00")
        self.assertEqual(row["author"]["name"], saved["_author_name"])

    def test_null_zero_empty_and_multiple_currencies(self):
        self.configure()
        order = self.submit(2)
        revision = order.revisions.get(revision=1)
        original = revision.content["items"][0]
        cases = [
            ([{**original, "applied_price": None}], None, None, False),
            ([{**original, "applied_price": "0"}], "0.00", "RUB", True),
            ([], None, None, False),
            ([original, {**original, "applied_currency": "USD"}], None, None, False),
        ]
        for items, total, currency, complete in cases:
            with self.subTest(items=items):
                revision.content["items"] = items
                revision.save()
                row = self.row()
                self.assertEqual(
                    (row["total_amount"], row["currency"], row["has_complete_prices"]), (total, currency, complete)
                )

    def test_live_fallback_and_dispatched_snapshot_totals(self):
        self.configure()
        order = self.submit(2)
        self.command(order, "approve")
        self.command(order, "approve", self.second)
        order.refresh_from_db()
        order.items.update(applied_price=Decimal("999"))
        self.assertEqual(self.row()["total_amount"], "1000.00")
        order.revisions.all().delete()
        self.assertEqual(self.row()["total_amount"], "9990.00")

    def test_legacy_snapshot_without_items_uses_live_rows(self):
        self.configure()
        order = self.submit(2)
        saved = order.revisions.get(revision=1)
        saved.content.pop("items")
        saved.save()
        self.assertEqual(self.row()["total_amount"], "1000.00")

    def test_list_summary_matches_detail_in_one_of_and_rework(self):
        self.configure("one_of")
        order = self.submit(2)
        self.assertEqual(self.row()["approval"], approval.approval_data(order, self.author))
        self.command(order, "return-for-rework", comment="Please revise")
        order.refresh_from_db()
        self.assertEqual(self.row()["approval"], approval.approval_data(order, self.author))
        self.assertEqual(self.row()["approval"]["available_actions"], ["edit_rework", "resubmit", "cancel"])

    def test_list_query_count_does_not_grow_per_order(self):
        self.configure()
        order = self.submit(2)
        self.row()  # Warm JWT/cache behavior before comparing requests.
        with CaptureQueriesContext(connection) as one:
            self.row()
        for _ in range(19):
            copy = Order.objects.create(
                order_number=f"ORD-{uuid4()}",
                venue=self.venue,
                created_by_user=self.author,
                status="awaiting_dispatch",
                approval_status="pending",
                revision=1,
            )
            OrderRevision.objects.create(order=copy, revision=1, content=order.revisions.first().content)
            cycle = OrderApprovalCycle.objects.create(
                order=copy, cycle_number=1, approval_type="sequential", order_revision=1
            )
            OrderApprovalStep.objects.create(
                cycle=cycle,
                position=1,
                membership_id=self.first.pk,
                user_id=self.author.pk,
                name=self.author.full_name,
                order_revision=1,
                status="active",
            )
        with CaptureQueriesContext(connection) as twenty:
            response = self.client.get("/api/v1/catalog/venues/mine/approvals", {"filter": "all"})
        self.assertEqual(response.data["count"], 20)
        self.assertEqual(len(one), len(twenty))
        self.assertLessEqual(len(twenty), 12)

    def test_grouped_detail_submit_and_internal_snapshot(self):
        self.configure()
        other = _make_supplier_product(price="20.00")
        add_cart_item(cart=self.cart, supplier_product=other, requested_name="Second", requested_qty=2)
        external = OffSystemSupplier.objects.create(venue=self.venue, display_name="External")
        add_cart_item(cart=self.cart, off_system_supplier=external, requested_name="External item", requested_qty=1)
        add_cart_item(cart=self.cart, requested_name="Unknown supplier", requested_qty=1)
        response = self.client.post(
            self.submit_url, {"command_id": str(uuid4()), "expected_settings_version": 2}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        submitted = self.items(response.data)
        order = Order.objects.get(pk=response.data["id"])
        detail = self.client.get(f"/api/v1/catalog/venues/{self.venue.pk}/orders/{order.pk}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual([item["id"] for item in self.items(detail.data)], [item["id"] for item in submitted])
        self.assertEqual(len(submitted), 4)
        self.assertEqual(len({item["id"] for item in submitted}), 4)
        self.assertIn("items", order.revisions.first().content)
        self.assertIn("items", approval.detail(order, self.author))

    def test_all_command_responses_have_grouped_items(self):
        self.configure()
        order = self.submit(2)
        item_id = str(order.items.first().pk)
        saved = self.command(
            order, "save-and-approve", changes={"items": {"update": [{"id": item_id, "requested_qty": "5"}]}}
        )
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(self.items(saved.data)[0]["requested_qty"], "5.0000")
        returned = self.command(order, "return-for-rework", self.second, comment="Revise")
        self.assertEqual(returned.status_code, 200, returned.data)
        self.items(returned.data)
        edited = self.command(order, "rework", changes={"items": {"update": [{"id": item_id, "requested_qty": "4"}]}})
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(self.items(edited.data)[0]["requested_qty"], "4.0000")
        resubmitted = self.command(order, "resubmit", expected_settings_version=2)
        self.assertEqual(resubmitted.status_code, 200, resubmitted.data)
        self.items(resubmitted.data)
        for actor in [self.author, self.second]:
            approved = self.command(order, "approve", actor)
            self.assertEqual(approved.status_code, 200, approved.data)
            self.items(approved.data)

    def test_flat_items_preserve_snapshot_price_and_order_without_extra_queries(self):
        self.configure()
        order = self.submit(2)
        saved = order.revisions.get(revision=order.revision).content["items"]
        self.product.prices.update(price=Decimal("999"))
        with CaptureQueriesContext(connection) as internal:
            expected = approval.detail(order, self.author)
        with CaptureQueriesContext(connection) as public:
            actual = approval.public_detail(order, self.author)
        self.assertLessEqual(len(public), len(internal))
        self.assertEqual(actual["items"], expected["items"])
        self.assertEqual([item["id"] for item in actual["items"]], [item["id"] for item in saved])
        self.assertEqual(self.items(actual)[0]["applied_price"], saved[0]["applied_price"])

    def test_flat_items_survive_dispatch_cancel_and_legacy_fallback(self):
        self.configure()
        cancelled = self.submit(2)
        response = self.command(cancelled, "cancel")
        self.assertEqual(response.status_code, 200, response.data)
        self.items(response.data)
        self.assertEqual(response.data["approval"]["status"], "cancelled")
        self.assertEqual(response.data["status"], "cancelled")

        # The direct path dispatches immediately and retains the saved price.
        approval.update_settings(
            self.venue.pk,
            self.author,
            {"mode": "direct", "expected_version": 2, "approver_membership_ids": [], "approval_type": None},
        )
        add_cart_item(cart=self.cart, supplier_product=self.product, requested_name="Product", requested_qty=3)
        order = self.submit(3)
        saved_price = order.revisions.first().content["items"][0]["applied_price"]
        order.items.update(applied_price=Decimal("777"))
        url = f"/api/v1/catalog/venues/{self.venue.pk}/orders/{order.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.items(response.data)[0]["applied_price"], saved_price)
        order.revisions.all().delete()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.items(response.data)[0]["applied_price"], "777.00")

    def test_empty_legacy_order_has_both_empty_item_representations(self):
        order = Order.objects.create(venue=self.venue, order_number=f"ORD-{uuid4()}", created_by_user=self.author)
        response = self.client.get(f"/api/v1/catalog/venues/{self.venue.pk}/orders/{order.pk}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.items(response.data), [])

    def test_deleted_author_name_survives_in_list(self):
        self.configure()
        order = self.submit(2)
        saved = order.revisions.first().content
        self.author.delete()
        from api.v1.catalog.tests import _jwt_bearer

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.second))
        row = self.row()
        self.assertEqual(row["author"], {"user_id": saved["_author_id"], "name": saved["_author_name"]})

    def test_step_membership_must_belong_to_the_order_venue(self):
        self.configure()
        order = self.submit(2)
        other = Venue.objects.create(display_name="Other company")
        foreign = VenueUser.objects.create(venue=other, user=self.author, role="manager")
        order.approval_cycles.first().steps.filter(position=1).update(membership_id=foreign.pk)
        row = self.row()
        self.assertFalse(row["approval"]["requires_my_decision"])
        self.assertEqual(row["approval"], approval.approval_data(order, self.author))

    def test_filters_pagination_and_multiple_supplier_total(self):
        self.configure()
        other = _make_supplier_product(price="20.00")
        add_cart_item(cart=self.cart, supplier_product=other, requested_name="Other", requested_qty=2)
        self.submit(2)
        self.assertEqual(self.row()["total_amount"], "1040.00")
        for number, state in enumerate(["rework", "approved"], start=1):
            copy = Order.objects.create(
                order_number=f"ORD-FILTER-{number}",
                venue=self.venue,
                created_by_user=self.author,
                status="awaiting_dispatch",
                approval_status=state,
            )
            OrderApprovalCycle.objects.create(order=copy, cycle_number=1, approval_type="one_of", order_revision=1)
        url = "/api/v1/catalog/venues/mine/approvals"
        for name, state in [("pending", "pending"), ("rework", "rework"), ("completed", "approved")]:
            response = self.client.get(url, {"filter": name})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["count"], 1)
            self.assertEqual(response.data["results"][0]["approval"]["status"], state)
        response = self.client.get(url, {"filter": "all", "limit": 1, "offset": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual((response.data["limit"], response.data["offset"], len(response.data["results"])), (1, 1, 1))
        expected = list(
            Order.objects.order_by("-business_updated_at", "-created_at", "id").values_list("id", flat=True)
        )
        self.assertEqual(response.data["results"][0]["id"], str(expected[1]))
        empty = self.client.get(url, {"filter": "all", "offset": 100})
        self.assertEqual(empty.data["results"], [])

    def test_list_amount_uses_the_revision_read_with_the_order(self):
        self.configure()
        order = self.submit(2)
        captured = Order.objects.select_related("venue", "created_by_user").get(pk=order.pk)
        saved = order.revisions.get(revision=1).content
        saved["items"][0]["applied_price"] = "200.00"
        OrderRevision.objects.create(order=order, revision=2, content=saved)
        Order.objects.filter(pk=order.pk).update(revision=2)
        row = approval_rows([captured], self.author)[0]
        self.assertEqual(row["total_amount"], "1000.00")
