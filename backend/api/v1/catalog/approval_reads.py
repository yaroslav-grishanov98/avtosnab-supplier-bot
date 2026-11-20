"""Batched summaries for the approval table, without constructing order details."""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Q
from rest_framework.fields import DateTimeField

from api.v1.catalog.approval import approval_summary
from core.models import OrderApprovalCycle, OrderApprovalStep, OrderItem, OrderRevision, VenueUser


def approval_rows(orders, actor):
    if not orders:
        return []
    ids = [order.pk for order in orders]
    snapshot_filter = Q()
    for order in orders:
        snapshot_filter |= Q(order_id=order.pk, revision=order.revision)
    snapshots = {
        row["order_id"]: row["content__items"]
        for row in OrderRevision.objects.filter(snapshot_filter).values("order_id", "content__items")
    }
    authors = {
        row["order_id"]: row
        for row in OrderRevision.objects.filter(order_id__in=ids)
        .order_by("order_id", "revision")
        .distinct("order_id")
        .values("order_id", "content___author_id", "content___author_name")
    }
    cycles = {
        cycle.order_id: cycle
        for cycle in OrderApprovalCycle.objects.filter(order_id__in=ids)
        .order_by("order_id", "-cycle_number")
        .distinct("order_id")
    }
    steps = defaultdict(list)
    for step in OrderApprovalStep.objects.filter(cycle_id__in=[cycle.pk for cycle in cycles.values()]).order_by(
        "position"
    ):
        steps[step.cycle_id].append(step)
    memberships = list(VenueUser.objects.filter(user=actor, status="active").values("id", "venue_id", "role"))
    active_ids = defaultdict(set)
    for membership in memberships:
        active_ids[membership["venue_id"]].add(membership["id"])
    editable_venues = {row["venue_id"] for row in memberships if row["role"] in ("owner", "manager")}

    live_ids = [order.pk for order in orders if order.status != "awaiting_dispatch" or snapshots.get(order.pk) is None]
    live_items = defaultdict(list)
    for item in OrderItem.objects.filter(order_id__in=live_ids).values(
        "id", "order_id", "applied_price", "applied_currency", "requested_qty"
    ):
        live_items[item["order_id"]].append(item)

    created_at_field = DateTimeField()
    rows = []
    for order in orders:
        saved = snapshots.get(order.pk)
        if order.status == "awaiting_dispatch" and saved is not None:
            items = saved
        else:
            originals = {item["id"]: item for item in saved or []}
            items = [{**item, **originals.get(str(item["id"]), {})} for item in live_items[order.pk]]
        currencies = {item.get("applied_currency") for item in items}
        complete = bool(items) and all(item.get("applied_price") is not None for item in items)
        complete = complete and len(currencies) == 1 and None not in currencies
        total = (
            sum(
                (Decimal(str(item["applied_price"])) * Decimal(str(item["requested_qty"])) for item in items),
                Decimal("0"),
            )
            if complete
            else None
        )
        author = authors.get(order.pk, {})
        if author.get("content___author_id"):
            author = {"user_id": author["content___author_id"], "name": author.get("content___author_name")}
        elif order.created_by_user_id:
            author = {"user_id": str(order.created_by_user_id), "name": order.created_by_user.full_name}
        else:
            author = None
        cycle = cycles.get(order.pk)
        rows.append(
            {
                "id": str(order.pk),
                "order_number": order.order_number,
                "venue_id": str(order.venue_id),
                "venue_name": order.venue.display_name,
                "author": author,
                "total_amount": str(total.quantize(Decimal("0.01"))) if total is not None else None,
                "currency": next(iter(currencies)) if complete else None,
                "has_complete_prices": complete,
                "created_at": created_at_field.to_representation(order.created_at),
                "business_updated_at": order.business_updated_at,
                "approval": approval_summary(
                    order,
                    cycle,
                    steps[cycle.pk] if cycle else [],
                    actor_id=actor.pk,
                    active_member_ids=active_ids[order.venue_id],
                    can_rework=(
                        order.created_by_user_id == actor.pk
                        or order.venue_id in editable_venues
                        or actor.role == "admin"
                    ),
                ),
            }
        )
    return rows
