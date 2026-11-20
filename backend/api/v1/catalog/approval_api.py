"""Public contracts for internal order approval."""

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_field, extend_schema_view
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import OptionalCoreUserJWTAuthentication
from api.v1.catalog import approval
from api.v1.catalog.serializers import (
    ApprovalOrderDetailSerializer,
    CartItemCreateSerializer,
    CartItemUpdateSerializer,
    CartSubmitContactSerializer,
    OrderApprovalSummarySerializer,
    OrderAuthorSerializer,
)
from api.v1.permissions import user_managed_venue_ids
from core.models import Order, User, Venue, VenueUser


def _approval_schema(**kwargs):
    return extend_schema(auth=[{"CoreUserJWT": []}], **kwargs)


class ApprovalSettingsInput(serializers.Serializer):
    mode = serializers.ChoiceField(choices=["direct", "approval"])
    approval_type = serializers.ChoiceField(choices=["one_of", "sequential"], allow_null=True)
    approver_membership_ids = serializers.ListField(child=serializers.UUIDField())
    expected_version = serializers.IntegerField(min_value=1)


class ApprovalSettingsOutput(serializers.Serializer):
    mode = serializers.CharField()
    approval_type = serializers.CharField(allow_null=True)
    approver_membership_ids = serializers.ListField(child=serializers.UUIDField())
    version = serializers.IntegerField()


class ApprovalOutput(serializers.Serializer):
    status = serializers.ChoiceField(choices=["not_required", "pending", "rework", "approved", "cancelled"])
    type = serializers.CharField(allow_null=True)
    cycle_id = serializers.UUIDField(allow_null=True)
    cycle_number = serializers.IntegerField(allow_null=True)
    current_step = serializers.IntegerField(allow_null=True)
    steps_count = serializers.IntegerField(allow_null=True)
    requires_my_decision = serializers.BooleanField()
    restart_required_on_change = serializers.BooleanField()
    available_actions = serializers.ListField(child=serializers.CharField())


class ApprovalCommandInput(serializers.Serializer):
    command_id = serializers.UUIDField()
    expected_revision = serializers.IntegerField(min_value=1)
    expected_cycle_id = serializers.UUIDField()


class OrderFieldsInput(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    requested_delivery_date = serializers.DateField(required=False, allow_null=True)
    subdivision_id = serializers.UUIDField(required=False, allow_null=True)
    delivery_address_id = serializers.UUIDField(required=False, allow_null=True)
    contacts = CartSubmitContactSerializer(many=True, required=False)


class ChangedItemInput(CartItemUpdateSerializer):
    id = serializers.UUIDField()


class OrderItemsInput(serializers.Serializer):
    add = CartItemCreateSerializer(many=True, required=False)
    update = ChangedItemInput(many=True, required=False)
    remove = serializers.ListField(child=serializers.UUIDField(), required=False)


@extend_schema_field(OrderItemsInput)
class OrderItemsField(serializers.JSONField):
    pass


class OrderChangesInput(serializers.Serializer):
    fields = OrderFieldsInput(required=False)
    items = OrderItemsField(required=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("changes must be an object.")
        if set(data) - set(self.fields):
            raise serializers.ValidationError("Unknown change fields.")
        fields = data.get("fields", {})
        if not isinstance(fields, dict):
            raise serializers.ValidationError("fields must be an object.")
        if set(fields) - set(OrderFieldsInput().fields):
            raise serializers.ValidationError("Unknown order fields.")
        if not isinstance(data.get("items", {}), dict):
            raise serializers.ValidationError("items must be an object.")
        return super().to_internal_value(data)


class SaveApproveInput(ApprovalCommandInput):
    changes = OrderChangesInput()
    confirm_restart = serializers.BooleanField(default=False)
    expected_settings_version = serializers.IntegerField(min_value=1, required=False)


class ReturnInput(ApprovalCommandInput):
    comment = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Причина возврата. Необязательное поле."
    )


class CancelInput(ApprovalCommandInput):
    comment = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Причина отмены. Необязательное поле."
    )


class ResubmitInput(ApprovalCommandInput):
    expected_settings_version = serializers.IntegerField(min_value=1)


class ReworkInput(ApprovalCommandInput):
    changes = OrderChangesInput()


class ApprovalErrorOutput(serializers.Serializer):
    code = serializers.CharField()
    detail = serializers.CharField()
    current_revision = serializers.IntegerField(required=False)
    current_cycle_id = serializers.UUIDField(required=False, allow_null=True)
    current_settings_version = serializers.IntegerField(required=False)


class ApprovalOrderOutput(ApprovalOrderDetailSerializer):
    approval = ApprovalOutput()


class ApprovalQuery(serializers.Serializer):
    filter = serializers.ChoiceField(choices=["pending", "rework", "completed", "all"], default="pending")
    limit = serializers.IntegerField(min_value=1, max_value=100, default=50)
    offset = serializers.IntegerField(min_value=0, default=0)


class ApprovalRow(serializers.Serializer):
    id = serializers.UUIDField()
    author = OrderAuthorSerializer(allow_null=True, read_only=True)
    business_updated_at = serializers.DateTimeField(allow_null=True)
    approval = OrderApprovalSummarySerializer()
    order_number = serializers.CharField()
    venue_id = serializers.UUIDField()
    venue_name = serializers.CharField()
    total_amount = serializers.DecimalField(max_digits=20, decimal_places=2, allow_null=True)
    currency = serializers.CharField(allow_null=True)
    has_complete_prices = serializers.BooleanField()
    created_at = serializers.DateTimeField()


class ApprovalPage(serializers.Serializer):
    count = serializers.IntegerField()
    limit = serializers.IntegerField()
    offset = serializers.IntegerField()
    results = ApprovalRow(many=True)


class CountOutput(serializers.Serializer):
    count = serializers.IntegerField()


class ApprovalStepOutput(serializers.Serializer):
    id = serializers.UUIDField()
    position = serializers.IntegerField()
    membership_id = serializers.UUIDField()
    user_id = serializers.UUIDField()
    name = serializers.CharField()
    status = serializers.CharField()
    order_revision = serializers.IntegerField()
    activated_at = serializers.DateTimeField(allow_null=True)
    decided_at = serializers.DateTimeField(allow_null=True)
    comment = serializers.CharField()


class ApprovalCycleOutput(serializers.Serializer):
    id = serializers.UUIDField()
    cycle_number = serializers.IntegerField()
    approval_type = serializers.CharField()
    order_revision = serializers.IntegerField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    completed_at = serializers.DateTimeField(allow_null=True)


class RouteOutput(serializers.Serializer):
    cycle = ApprovalCycleOutput(allow_null=True)
    steps = ApprovalStepOutput(many=True)


class HistoryEventOutput(serializers.Serializer):
    id = serializers.UUIDField()
    event = serializers.CharField()
    created_at = serializers.DateTimeField()
    actor = serializers.JSONField()
    revision = serializers.IntegerField()
    cycle_id = serializers.UUIDField(allow_null=True)
    comment = serializers.CharField()
    changes = serializers.JSONField()


class HistoryPage(serializers.Serializer):
    count = serializers.IntegerField()
    limit = serializers.IntegerField()
    offset = serializers.IntegerField()
    results = HistoryEventOutput(many=True)


class ApprovalBase(APIView):
    permission_classes = []
    authentication_classes = [OptionalCoreUserJWTAuthentication]

    def checked_order(self, request, venue_id, order_id):
        approval.require_actor(request.user, venue_id)
        return get_object_or_404(Order, pk=order_id, venue_id=venue_id)


@extend_schema_view(
    get=_approval_schema(responses=ApprovalSettingsOutput, tags=["Согласования"]),
    put=_approval_schema(
        request=ApprovalSettingsInput,
        responses={200: ApprovalSettingsOutput, 409: ApprovalErrorOutput},
        tags=["Согласования"],
    ),
)
class ApprovalSettingsView(ApprovalBase):
    def get(self, request, venue_id):
        approval.require_actor(request.user, venue_id)
        get_object_or_404(Venue, pk=venue_id)
        return Response(approval.settings_data(approval.settings_for(venue_id)))

    def put(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        ser = ApprovalSettingsInput(data=request.data)
        ser.is_valid(raise_exception=True)
        return Response(approval.settings_data(approval.update_settings(venue_id, request.user, ser.validated_data)))


class ApprovalCommandView(ApprovalBase):
    operation = "approve"
    input_serializer = ApprovalCommandInput

    def post(self, request, venue_id, order_id):
        self.checked_order(request, venue_id, order_id)
        ser = self.input_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if set(request.data) - set(ser.fields):
            raise ValidationError("Unknown command fields.")
        order = approval.execute(order_id, venue_id, request.user, self.operation, ser.validated_data)
        return Response(approval.public_detail(order, request.user))


@extend_schema_view(
    post=_approval_schema(
        request=ApprovalCommandInput,
        responses={200: ApprovalOrderOutput, 409: ApprovalErrorOutput},
        tags=["Согласования"],
    )
)
class ApproveView(ApprovalCommandView):
    pass


@extend_schema_view(
    post=_approval_schema(
        request=SaveApproveInput, responses={200: ApprovalOrderOutput, 409: ApprovalErrorOutput}, tags=["Согласования"]
    )
)
class SaveApproveView(ApprovalCommandView):
    operation = "save-and-approve"
    input_serializer = SaveApproveInput


@extend_schema_view(
    post=_approval_schema(
        request=ReturnInput, responses={200: ApprovalOrderOutput, 409: ApprovalErrorOutput}, tags=["Согласования"]
    )
)
class ReturnForReworkView(ApprovalCommandView):
    operation = "return-for-rework"
    input_serializer = ReturnInput


@extend_schema_view(
    post=_approval_schema(
        request=ResubmitInput, responses={200: ApprovalOrderOutput, 409: ApprovalErrorOutput}, tags=["Согласования"]
    )
)
class ResubmitView(ApprovalCommandView):
    operation = "resubmit"
    input_serializer = ResubmitInput


@extend_schema_view(
    post=_approval_schema(
        summary="Отменить заказ на согласовании",
        description=(
            "Отзывает заказ, пока он не ушёл поставщикам: `approval_status` pending или rework. "
            "Заказ и согласование получают статус `cancelled`, поставщикам ничего не отправляется. "
            "Доступно автору заказа, owner/manager заведения и admin (`available_actions` содержит `cancel`). "
            "Повтор с тем же `command_id` возвращает тот же результат."
        ),
        request=CancelInput,
        responses={200: ApprovalOrderOutput, 409: ApprovalErrorOutput},
        tags=["Согласования"],
    )
)
class CancelView(ApprovalCommandView):
    operation = "cancel"
    input_serializer = CancelInput


@extend_schema_view(
    patch=_approval_schema(
        request=ReworkInput, responses={200: ApprovalOrderOutput, 409: ApprovalErrorOutput}, tags=["Согласования"]
    )
)
class ReworkView(ApprovalCommandView):
    operation = "rework"
    input_serializer = ReworkInput
    http_method_names = ["patch", "options"]

    def patch(self, request, venue_id, order_id):
        return super().post(request, venue_id, order_id)


@extend_schema_view(get=_approval_schema(responses=RouteOutput, tags=["Согласования"]))
class ApprovalRouteView(ApprovalBase):
    def get(self, request, venue_id, order_id):
        order = self.checked_order(request, venue_id, order_id)
        cycle = approval.latest_cycle(order)
        return Response(
            {
                "cycle": ApprovalCycleOutput(cycle).data if cycle else None,
                "steps": ApprovalStepOutput(cycle.steps.all(), many=True).data if cycle else [],
            }
        )


def approvals_queryset(actor):
    if not isinstance(actor, User) or actor.status != "active":
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied("Personal account required.")
    ids = user_managed_venue_ids(actor)
    qs = (
        Order.objects.filter(approval_cycles__isnull=False)
        .distinct()
        .select_related("venue", "created_by_user")
        .order_by("-business_updated_at", "-created_at", "id")
    )
    return qs.filter(venue_id__in=ids) if ids is not None else qs


def pagination(data):
    ser = ApprovalQuery(data=data)
    ser.is_valid(raise_exception=True)
    return ser.validated_data


@extend_schema_view(get=_approval_schema(parameters=[ApprovalQuery], responses=ApprovalPage, tags=["Согласования"]))
class ApprovalListView(ApprovalBase):
    def get(self, request):
        query = pagination(request.query_params)
        qs = approvals_queryset(request.user)
        status = query["filter"]
        if status != "all":
            qs = qs.filter(approval_status="approved" if status == "completed" else status)
        from api.v1.catalog.approval_reads import approval_rows

        rows = approval_rows(list(qs[query["offset"] : query["offset"] + query["limit"]]), request.user)
        return Response({"count": qs.count(), "limit": query["limit"], "offset": query["offset"], "results": rows})


@extend_schema_view(get=_approval_schema(responses=CountOutput, tags=["Согласования"]))
class ApprovalCountView(ApprovalBase):
    def get(self, request):
        qs = approvals_queryset(request.user).filter(approval_status="pending")
        memberships = VenueUser.objects.filter(user=request.user, status="active").values("id")
        count = (
            qs.filter(
                approval_cycles__status="active",
                approval_cycles__steps__status="active",
                approval_cycles__steps__user_id=request.user.pk,
                approval_cycles__steps__membership_id__in=memberships,
            )
            .distinct()
            .count()
        )
        return Response({"count": count})


@extend_schema_view(get=_approval_schema(parameters=[ApprovalQuery], responses=HistoryPage, tags=["Согласования"]))
class ApprovalHistoryView(ApprovalBase):
    def get(self, request, venue_id, order_id):
        order = self.checked_order(request, venue_id, order_id)
        query = pagination(request.query_params)
        qs = order.business_events.order_by("-created_at", "-id")
        rows = [
            {
                "id": row.pk,
                "event": row.event,
                "created_at": row.created_at,
                "actor": {"user_id": row.actor_id, "name": row.actor_name},
                "revision": row.revision,
                "cycle_id": row.cycle_id,
                "comment": row.comment,
                "changes": row.changes,
            }
            for row in qs[query["offset"] : query["offset"] + query["limit"]]
        ]
        return Response({"count": qs.count(), "limit": query["limit"], "offset": query["offset"], "results": rows})
