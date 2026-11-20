"""Transactional order approval commands and durable content revisions."""

import hashlib
import json
from copy import deepcopy

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from api.v1.permissions import user_can_access_venue
from core.models import (
    CompanyApprovalApprover,
    CompanyApprovalSettings,
    EntityStatus,
    OffSystemSupplier,
    Order,
    OrderApprovalCommand,
    OrderApprovalCycle,
    OrderApprovalStep,
    OrderBusinessEvent,
    OrderContact,
    OrderItem,
    OrderRevision,
    User,
    Venue,
    VenueAddress,
    VenueCart,
    VenueSubdivision,
    VenueUser,
)


class ApprovalConflict(APIException):
    status_code = 409
    default_code = "approval_conflict"

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def conflict(code, order=None, settings=None):
    detail = {"code": code, "detail": "Заказ или настройки изменились. Обновите данные."}
    if order:
        detail.update(
            current_revision=order.revision,
            current_cycle_id=str(latest_cycle(order).pk) if latest_cycle(order) else None,
        )
    if settings:
        detail["current_settings_version"] = settings.version
    raise ApprovalConflict(detail)


def actor_name(actor):
    return actor.full_name if isinstance(actor, User) else ""


def require_actor(actor, venue_id):
    if not isinstance(actor, User) or actor.status != EntityStatus.ACTIVE or not user_can_access_venue(actor, venue_id):
        raise PermissionDenied("Requires a personal account with venue access.")


def settings_for(venue_id, lock=False):
    # Serialize first-use creation against all other settings commands.
    if lock:
        Venue.objects.select_for_update().get(pk=venue_id)
    settings, _ = CompanyApprovalSettings.objects.get_or_create(venue_id=venue_id)
    if lock:
        settings = CompanyApprovalSettings.objects.select_for_update().get(pk=settings.pk)
    return settings


def settings_data(settings):
    return {
        "mode": settings.mode,
        "approval_type": settings.approval_type,
        "approver_membership_ids": [str(x) for x in settings.approvers.values_list("membership_id", flat=True)],
        "version": settings.version,
    }


@transaction.atomic
def update_settings(venue_id, actor, data):
    require_actor(actor, venue_id)
    settings = settings_for(venue_id, lock=True)
    if (
        actor.role != "admin"
        and not VenueUser.objects.select_for_update()
        .filter(user=actor, venue_id=venue_id, status=EntityStatus.ACTIVE)
        .exists()
    ):
        raise PermissionDenied("Requires an active company management membership.")
    if data["expected_version"] != settings.version:
        conflict("settings_conflict", settings=settings)
    ids = data["approver_membership_ids"]
    memberships = list(
        VenueUser.objects.select_for_update(of=("self",)).filter(
            pk__in=ids, venue_id=venue_id, status=EntityStatus.ACTIVE, user__status=EntityStatus.ACTIVE
        )
    )
    if len(set(ids)) != len(ids) or len(memberships) != len(ids):
        raise ValidationError("Approvers must be distinct active venue memberships.")
    if data["mode"] == "approval" and (not ids or data.get("approval_type") not in ("one_of", "sequential")):
        raise ValidationError("Approval requires a type and at least one approver.")
    if data["mode"] == "direct" and (ids or data.get("approval_type") is not None):
        raise ValidationError("Direct mode requires null approval_type and an empty route.")
    settings.mode = data["mode"]
    settings.approval_type = data.get("approval_type")
    settings.version += 1
    settings.updated_by = actor
    settings.save()
    settings.approvers.all().delete()
    CompanyApprovalApprover.objects.bulk_create(
        [CompanyApprovalApprover(settings=settings, membership_id=pk, position=i) for i, pk in enumerate(ids, 1)]
    )
    return settings


def latest_cycle(order):
    return order.approval_cycles.order_by("-cycle_number").first()


def event(order, name, actor, cycle=None, comment="", changes=None):
    OrderBusinessEvent.objects.create(
        order=order,
        event=name,
        actor_id=getattr(actor, "pk", None),
        actor_name=actor_name(actor),
        revision=order.revision,
        cycle_id=getattr(cycle, "pk", None),
        comment=comment,
        changes=changes or [],
    )
    order.business_updated_at = timezone.now()
    order.save(update_fields=["business_updated_at", "updated_at"])


def editable_state(order):
    fields = {
        key: getattr(order, key)
        for key in ("comment", "requested_delivery_date", "subdivision_id", "delivery_address_id")
    }
    fields["contacts"] = list(order.contacts.order_by("created_at", "id").values("full_name", "email", "phone"))
    fields["items"] = list(
        order.items.order_by("created_at", "id").values(
            "id",
            "supplier_product_id",
            "off_system_supplier_id",
            "subdivision_id",
            "requested_name",
            "requested_qty",
            "requested_unit_id",
            "comment",
        )
    )
    return json.loads(json.dumps(fields, cls=DjangoJSONEncoder))


def snapshot(order, changes=None):
    from api.v1.catalog.serializers import OrderDetailSerializer

    content = json.loads(json.dumps(dict(OrderDetailSerializer(order).data), cls=DjangoJSONEncoder))
    prior = order.revisions.order_by("-revision").first()
    content["_editable"] = editable_state(order)
    content["_notification_unit_names"] = {
        str(item.pk): item.requested_unit.name if item.requested_unit_id else ""
        for item in order.items.select_related("requested_unit")
    }
    content["_author_name"] = actor_name(order.created_by_user)
    content["_author_id"] = str(order.created_by_user_id) if order.created_by_user_id else None
    if prior:
        old = prior.content
        old_items = {x["id"]: x for x in old.get("items", [])}
        for item_id, unit_name in old.get("_notification_unit_names", {}).items():
            if item_id in content["_notification_unit_names"]:
                content["_notification_unit_names"][item_id] = unit_name
        changed_fields = (changes or {}).get("fields", {})
        updated_items = {str(x["id"]): x for x in (changes or {}).get("items", {}).get("update", [])}
        editable = {"requested_qty", "comment", "subdivision_id", "subdivision_name", "item_total", "remaining_qty"}
        for item in content["items"]:
            if item["id"] in old_items:
                original = old_items[item["id"]]
                for key in original:
                    if key not in editable:
                        item[key] = original[key]
                if "subdivision_id" not in updated_items.get(item["id"], {}):
                    item["subdivision_id"] = original.get("subdivision_id")
                if original.get("subdivision_id") == item.get("subdivision_id"):
                    item["subdivision_name"] = original.get("subdivision_name")
        for key, id_key in [("delivery_address", "delivery_address_id"), ("subdivision_name", "subdivision_id")]:
            if id_key not in changed_fields or old.get(id_key) == content.get(id_key):
                content[key] = old.get(key)
                content[id_key] = old.get(id_key)
        content["_author_name"] = old.get("_author_name", content["_author_name"])
        content["_author_id"] = old.get("_author_id", content["_author_id"])
        old_groups = {(x.get("supplier_id"), x.get("is_off_system")): x for x in old.get("supplier_groups", [])}
        groups = {}
        for item in content["items"]:
            item["order_comment"] = order.comment
            item["order_requested_delivery_date"] = (
                order.requested_delivery_date.isoformat() if order.requested_delivery_date else None
            )
            key = (item.get("supplier_id"), bool(item.get("off_system_supplier_id")))
            if key not in groups:
                original = old_groups.get(key)
                groups[key] = (
                    {k: v for k, v in original.items() if k != "items"}
                    if original
                    else {
                        "supplier_id": key[0],
                        "supplier_name": item.get("supplier_name"),
                        "is_off_system": key[1],
                        "status": "submitted",
                    }
                )
                groups[key]["items"] = []
            groups[key]["items"].append(item)
        content["supplier_groups"] = list(groups.values())
    for key in ("approval", "author", "revision", "business_updated_at"):
        content.pop(key, None)
    OrderRevision.objects.create(order=order, revision=order.revision, content=content)
    return content


def start_cycle(order, settings, actor):
    assignments = list(settings.approvers.all())
    if not assignments:
        raise ValidationError("The approval route is empty.")
    ids = [a.membership_id for a in assignments]
    memberships = {
        m.pk: m
        for m in VenueUser.objects.select_for_update(of=("self",))
        .filter(pk__in=ids, venue_id=order.venue_id, status=EntityStatus.ACTIVE, user__status=EntityStatus.ACTIVE)
        .select_related("user")
    }
    if len(memberships) != len(ids):
        raise ValidationError("The route contains an inactive approver. Update approval settings.")
    number = (order.approval_cycles.aggregate(n=Max("cycle_number"))["n"] or 0) + 1
    cycle = OrderApprovalCycle.objects.create(
        order=order,
        cycle_number=number,
        approval_type=settings.approval_type,
        order_revision=order.revision,
        created_by=actor,
    )
    now = timezone.now()
    OrderApprovalStep.objects.bulk_create(
        [
            OrderApprovalStep(
                cycle=cycle,
                membership_id=a.membership_id,
                user_id=memberships[a.membership_id].user_id,
                name=memberships[a.membership_id].user.full_name,
                position=a.position,
                order_revision=order.revision,
                status="active" if settings.approval_type == "one_of" or i == 0 else "waiting",
                activated_at=now if settings.approval_type == "one_of" or i == 0 else None,
            )
            for i, a in enumerate(assignments)
        ]
    )
    order.approval_status = "pending"
    order.status = "awaiting_dispatch"
    order.save(update_fields=["approval_status", "status", "updated_at"])
    return cycle


def dispatch(order, actor):
    from api.v1.catalog.services import _create_supplier_order_requests, _enqueue_order_notifications

    if order.submitted_at is not None:
        return
    items = list(order.items.select_related("supplier_product__supplier"))
    if not items:
        raise ValidationError("Cannot submit an empty order.")
    order.status = "submitted"
    order.submitted_at = timezone.now()
    order.save(update_fields=["status", "submitted_at", "updated_at"])
    saved = order.revisions.get(revision=order.revision).content
    _create_supplier_order_requests(order=order, order_items=items, actor=actor, frozen=saved)
    _enqueue_order_notifications(order=order, order_items=items, frozen=saved)
    event(order, "dispatched_to_suppliers", actor, latest_cycle(order))


def fingerprint(operation, target, payload):
    return hashlib.sha256(
        json.dumps([operation, str(target), payload], sort_keys=True, cls=DjangoJSONEncoder).encode()
    ).hexdigest()


def previous_command(actor, command_id, operation, target, payload):
    # Actor row serializes command IDs even across distinct orders.
    locked_actor = User.objects.select_for_update(no_key=True).get(pk=actor.pk)
    if locked_actor.status != "active":
        raise PermissionDenied("Inactive account.")
    previous = OrderApprovalCommand.objects.filter(actor_id=actor.pk, command_id=command_id).first()
    if previous and previous.fingerprint != fingerprint(operation, target, payload):
        conflict("command_id_conflict")
    return previous


def remember(actor, operation, target, payload, order):
    OrderApprovalCommand.objects.create(
        actor_id=actor.pk,
        command_id=payload["command_id"],
        operation=operation,
        target_id=target,
        fingerprint=fingerprint(operation, target, payload),
        order=order,
    )


@transaction.atomic
def submit(cart, actor, payload, **fields):
    from api.v1.catalog.services import create_order_from_cart

    require_actor(actor, cart.venue_id)
    previous = previous_command(actor, payload["command_id"], "submit", cart.pk, payload)
    if previous:
        return Order.objects.get(pk=previous.order_id)
    cart = VenueCart.objects.select_for_update().get(pk=cart.pk)
    settings = settings_for(cart.venue_id, lock=True)
    if settings.version != payload["expected_settings_version"]:
        conflict("settings_conflict", settings=settings)
    order, error = create_order_from_cart(cart, actor=actor, **fields)
    if error:
        raise ApprovalConflict({"code": "invalid_state", "detail": error})
    snapshot(order)
    event(order, "created", actor)
    if settings.mode == "approval":
        cycle = start_cycle(order, settings, actor)
        event(order, "submitted_for_approval", actor, cycle)
    else:
        dispatch(order, actor)
    remember(actor, "submit", cart.pk, payload, order)
    return order


def active_step(order, cycle, actor):
    if not cycle or cycle.status != "active" or order.approval_status != "pending":
        conflict("invalid_state", order)
    step = cycle.steps.filter(user_id=actor.pk, status="active").first()
    if (
        not step
        or not VenueUser.objects.select_for_update(of=("self",))
        .filter(
            pk=step.membership_id,
            venue_id=order.venue_id,
            user_id=actor.pk,
            status=EntityStatus.ACTIVE,
            user__status=EntityStatus.ACTIVE,
        )
        .exists()
    ):
        raise PermissionDenied("Only the active appointed member can decide.")
    return step


def may_rework(order, actor):
    return (
        (order.created_by_user_id == actor.pk and user_can_access_venue(actor, order.venue_id))
        or VenueUser.objects.filter(
            user=actor, venue_id=order.venue_id, status=EntityStatus.ACTIVE, role__in=["owner", "manager"]
        ).exists()
        or actor.role == "admin"
    )


def close_cycle(cycle, status):
    cycle.status = status
    cycle.completed_at = timezone.now()
    cycle.save(update_fields=["status", "completed_at", "updated_at"])
    cycle.steps.filter(status__in=["active", "waiting"]).update(status="cancelled", decided_at=timezone.now())


def approve(order, cycle, step, actor):
    if not order.items.exists():
        raise ValidationError("Cannot approve an empty order.")
    step.order_revision = order.revision
    step.status = "approved"
    step.decided_at = timezone.now()
    step.save(update_fields=["status", "order_revision", "decided_at", "updated_at"])
    event(order, "step_approved", actor, cycle)
    following = cycle.steps.filter(status="waiting").first() if cycle.approval_type == "sequential" else None
    if following:
        following.status = "active"
        following.activated_at = timezone.now()
        following.save(update_fields=["status", "activated_at", "updated_at"])
    else:
        close_cycle(cycle, "approved")
        order.approval_status = "approved"
        order.save(update_fields=["approval_status", "updated_at"])
        event(order, "approval_completed", actor, cycle)
        dispatch(order, actor)


def owned(model, pk, venue_id):
    if pk is None:
        return None
    obj = model.objects.filter(pk=pk, venue_id=venue_id).first()
    if obj is None or getattr(obj, "status", "active") != "active":
        raise ValidationError("Reference must belong to this venue and be active.")
    return obj


def apply_changes(order, changes):
    from api.v1.catalog.selectors import resolve_supplier_product_price
    from api.v1.catalog.serializers import (
        CartItemCreateSerializer,
        CartItemUpdateSerializer,
        validate_supplier_product_qty,
    )

    changes = deepcopy(changes or {})
    allowed = {"comment", "requested_delivery_date", "subdivision_id", "delivery_address_id", "contacts"}
    if set(changes) - {"fields", "items"} or set(changes.get("fields", {})) - allowed:
        raise ValidationError("Unknown order fields.")
    before = editable_state(order)
    saved = order.revisions.filter(revision=order.revision).first()
    if saved:
        # Explicit edits target the approved snapshot, even when SET_NULL has
        # already cleared the corresponding live reference.
        for key in ("delivery_address_id", "subdivision_id"):
            if key in changes.get("fields", {}):
                before[key] = saved.content.get(key)
    for key, value in changes.get("fields", {}).items():
        if key == "contacts":
            current_contacts = list(order.contacts.order_by("created_at", "id").values("full_name", "email", "phone"))
            normalized = [
                {"full_name": c.get("full_name"), "email": c.get("email"), "phone": c.get("phone")} for c in value
            ]
            if current_contacts == normalized:
                continue
            order.contacts.all().delete()
            OrderContact.objects.bulk_create([OrderContact(order=order, **c) for c in value])
        elif key == "delivery_address_id":
            order.delivery_address = owned(VenueAddress, value, order.venue_id)
        elif key == "subdivision_id":
            order.subdivision = owned(VenueSubdivision, value, order.venue_id)
        else:
            setattr(order, key, value)
    ops = changes.get("items", {})
    if set(ops) - {"add", "update", "remove"}:
        raise ValidationError("Unknown item operations.")
    from rest_framework import serializers

    for key in ("add", "update", "remove"):
        if not isinstance(ops.get(key, []), list):
            raise ValidationError("Item operations must be arrays.")
    if any(not isinstance(x, dict) for x in ops.get("add", []) + ops.get("update", [])):
        raise ValidationError("Item inputs must be objects.")
    for update in ops.get("update", []):
        if "id" not in update:
            raise ValidationError("Updated item requires id.")
        update["id"] = serializers.UUIDField().run_validation(update["id"])
    ops["remove"] = [serializers.UUIDField().run_validation(x) for x in ops.get("remove", [])]
    referenced = [str(x["id"]) for x in ops.get("update", [])] + [str(x) for x in ops.get("remove", [])]
    if len(set(referenced)) != len(referenced) or order.items.filter(pk__in=referenced).count() != len(referenced):
        raise ValidationError("Duplicate or foreign order items.")
    if saved:
        saved_items = {item["id"]: item for item in saved.content.get("items", [])}
        updated_ids = {
            str(item["id"])
            for item in changes.get("items", {}).get("update", [])
            if isinstance(item, dict) and "id" in item and "subdivision_id" in item
        }
        for item in before["items"]:
            if item["id"] in updated_ids and item["id"] in saved_items:
                item["subdivision_id"] = saved_items[item["id"]].get("subdivision_id")
    order.items.filter(pk__in=ops.get("remove", [])).delete()
    for update in ops.get("update", []):
        item = order.items.get(pk=update["id"])
        raw = {k: v for k, v in update.items() if k != "id"}
        if set(raw) - set(CartItemUpdateSerializer().fields):
            raise ValidationError("Unknown item fields.")
        ser = CartItemUpdateSerializer(data=raw)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if "subdivision_id" in data:
            item.subdivision = owned(VenueSubdivision, data.pop("subdivision_id"), order.venue_id)
        if "requested_qty" in data:
            validate_supplier_product_qty(supplier_product=item.supplier_product, requested_qty=data["requested_qty"])
        for key, value in data.items():
            setattr(item, key, value)
        item.save()
    for raw in ops.get("add", []):
        if set(raw) - set(CartItemCreateSerializer().fields):
            raise ValidationError("Unknown item fields.")
        ser = CartItemCreateSerializer(data=raw)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        data["subdivision"] = owned(VenueSubdivision, data.pop("subdivision_id", None), order.venue_id)
        data["off_system_supplier"] = owned(OffSystemSupplier, data.pop("off_system_supplier_id", None), order.venue_id)
        product = data.get("supplier_product")
        if product:
            price, currency, price_id = resolve_supplier_product_price(
                supplier_product=product, venue_id=order.venue_id
            )
            data.update(
                applied_price=price,
                applied_currency=currency,
                applied_price_type="individual" if price_id else "base",
                applied_individual_price_id=price_id,
            )
        OrderItem.objects.create(order=order, **data)
    order.save()
    after = editable_state(order)
    diffs = [{"field": key, "before": before[key], "after": after[key]} for key in before if before[key] != after[key]]
    if diffs:
        order.revision += 1
        order.save(update_fields=["revision", "updated_at"])
        snapshot(order, changes=changes)
    return diffs


@transaction.atomic
def execute(order_id, venue_id, actor, operation, payload):
    require_actor(actor, venue_id)
    previous = previous_command(actor, payload["command_id"], operation, order_id, payload)
    order = Order.objects.select_for_update().get(pk=order_id, venue_id=venue_id)
    if previous:
        return order
    if order.revision != payload["expected_revision"]:
        conflict("revision_conflict", order)
    settings = settings_for(venue_id, lock=True)
    cycle = latest_cycle(order)
    if cycle is None or str(cycle.pk) != str(payload["expected_cycle_id"]):
        conflict("cycle_conflict", order)
    if operation == "cancel":
        if order.approval_status not in ("pending", "rework") or order.submitted_at is not None:
            conflict("invalid_state", order)
        if not may_rework(order, actor):
            raise PermissionDenied("Order is not available for cancellation.")
        if cycle.status == "active":
            close_cycle(cycle, "cancelled")
        order.approval_status = "cancelled"
        order.status = "cancelled"
        order.save(update_fields=["approval_status", "status", "updated_at"])
        event(order, "approval_cancelled", actor, cycle, comment=payload.get("comment", "").strip())
    elif operation in ("rework", "resubmit"):
        if order.approval_status != "rework":
            conflict("invalid_state", order)
        if not may_rework(order, actor):
            raise PermissionDenied("Order is not available for rework.")
        if operation == "rework":
            diffs = apply_changes(order, payload["changes"])
            if diffs:
                event(order, "changed", actor, cycle, changes=diffs)
        else:
            settings = settings_for(venue_id, lock=True)
            if settings.version != payload["expected_settings_version"]:
                conflict("settings_conflict", order, settings)
            if not order.items.exists():
                raise ValidationError("Cannot submit an empty order.")
            if settings.mode == "approval":
                cycle = start_cycle(order, settings, actor)
            else:
                order.approval_status = "not_required"
                order.save(update_fields=["approval_status", "updated_at"])
            event(order, "resubmitted", actor, cycle)
            if settings.mode == "direct":
                dispatch(order, actor)
    else:
        # A concurrent completed decision on this exact cycle is an idempotent observation.
        if cycle.status != "active":
            if (
                operation in ("approve", "return-for-rework")
                and cycle.status != "cancelled"
                and cycle.steps.filter(
                    user_id=actor.pk, status__in=["approved", "returned", "cancelled"], activated_at__isnull=False
                ).exists()
                and VenueUser.objects.filter(
                    user=actor, venue_id=venue_id, status="active", pk__in=cycle.steps.values("membership_id")
                ).exists()
            ):
                remember(actor, operation, order_id, payload, order)
                return order
            conflict("invalid_state", order)
        step = active_step(order, cycle, actor)
        if operation == "return-for-rework":
            comment = payload.get("comment", "").strip()
            step.status, step.comment, step.decided_at = "returned", comment, timezone.now()
            step.save()
            close_cycle(cycle, "returned")
            order.approval_status = "rework"
            order.save(update_fields=["approval_status", "updated_at"])
            event(order, "returned_for_rework", actor, cycle, comment=comment)
        elif operation == "save-and-approve":
            had_approvals = cycle.steps.filter(status="approved").exists()
            diffs = apply_changes(order, payload["changes"])
            if diffs and had_approvals:
                if not payload.get("confirm_restart"):
                    order.revision = payload["expected_revision"]
                    conflict("restart_confirmation_required", order)
                settings = settings_for(venue_id, lock=True)
                if settings.mode != "approval" or settings.version != payload.get("expected_settings_version"):
                    order.revision = payload["expected_revision"]
                    conflict("settings_conflict", order, settings)
                event(order, "changed", actor, cycle, changes=diffs)
                close_cycle(cycle, "restarted")
                cycle = start_cycle(order, settings, actor)
                event(order, "approval_restarted", actor, cycle)
            else:
                if diffs:
                    cycle.order_revision = order.revision
                    cycle.save()
                    cycle.steps.filter(status__in=["active", "waiting"]).update(order_revision=order.revision)
                    event(order, "changed", actor, cycle, changes=diffs)
                approve(order, cycle, step, actor)
        else:
            approve(order, cycle, step, actor)
    remember(actor, operation, order_id, payload, order)
    return order


def approval_summary(order, cycle, steps, *, actor_id=None, active_member_ids=(), can_rework=False):
    active_steps = [step for step in steps if step.status == "active"]
    active = bool(
        cycle
        and cycle.status == "active"
        and any(step.user_id == actor_id and step.membership_id in active_member_ids for step in active_steps)
    )
    actions = ["approve", "save_and_approve", "return_for_rework"] if active else []
    if order.approval_status == "rework" and can_rework:
        actions = ["edit_rework", "resubmit", "cancel"]
    elif order.approval_status == "pending" and can_rework:
        actions = [*actions, "cancel"]
    current = active_steps[0] if active_steps else None
    sequential = cycle and cycle.approval_type == "sequential"
    return {
        "status": order.approval_status,
        "type": cycle.approval_type if cycle else None,
        "cycle_id": str(cycle.pk) if cycle else None,
        "cycle_number": cycle.cycle_number if cycle else None,
        "current_step": current.position if current and sequential else None,
        "steps_count": len(steps) if sequential else None,
        "requires_my_decision": active,
        "restart_required_on_change": bool(cycle and any(step.status == "approved" for step in steps)),
        "available_actions": actions,
    }


def approval_data(order, actor):
    cycle = latest_cycle(order)
    personal = isinstance(actor, User) and actor.status == "active"
    members = (
        set(VenueUser.objects.filter(user=actor, venue_id=order.venue_id, status="active").values_list("id", flat=True))
        if personal and cycle and cycle.status == "active"
        else set()
    )
    return approval_summary(
        order,
        cycle,
        list(cycle.steps.all()) if cycle else [],
        actor_id=actor.pk if personal else None,
        active_member_ids=members,
        can_rework=bool(
            isinstance(actor, User)
            and order.approval_status in ("pending", "rework")
            and user_can_access_venue(actor, order.venue_id)
            and may_rework(order, actor)
        ),
    )


def detail(order, actor):
    from api.v1.catalog.serializers import VenueOrderDetailSerializer

    result = dict(VenueOrderDetailSerializer(order).data)
    saved = order.revisions.filter(revision=order.revision).first()
    if saved:
        if order.status == "awaiting_dispatch":
            for key, value in saved.content.items():
                if not key.startswith("_") and key not in ("status", "submitted_at", "created_at", "updated_at"):
                    result[key] = value
        else:
            for key in (
                "delivery_address",
                "delivery_address_id",
                "subdivision_name",
                "subdivision_id",
                "contacts",
                "comment",
                "requested_delivery_date",
            ):
                result[key] = saved.content.get(key)
            originals = {x["id"]: x for x in saved.content.get("items", [])}
            immutable = (
                "supplier_product_id",
                "supplier_id",
                "supplier_name",
                "supplier_product_name",
                "requested_name",
                "requested_qty",
                "requested_unit",
                "applied_price",
                "applied_currency",
                "applied_price_type",
                "applied_individual_price_id",
                "comment",
                "subdivision_id",
                "subdivision_name",
            )
            for item in result["items"]:
                original = originals.get(item["id"])
                if original:
                    for key in immutable:
                        item[key] = original.get(key)
            frozen_items = {x["id"]: x for x in result["items"]}
            for group in result["supplier_groups"]:
                group["items"] = [frozen_items[x["id"]] for x in group["items"]]
    result.update(
        revision=order.revision,
        author=result.get("author"),
        business_updated_at=order.business_updated_at,
        approval=approval_data(order, actor),
    )
    return result


def public_detail(order, actor):
    """HTTP detail includes both flat items and supplier-grouped items."""
    return detail(order, actor)
