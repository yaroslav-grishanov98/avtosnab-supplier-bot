"""Supplier requests for a verified bot identity, using request IDs throughout."""

from django.db.models import Count
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.catalog.selectors import supplier_order_request_detail_queryset, supplier_order_requests_queryset
from api.v1.catalog.services import mark_supplier_order_request_viewed
from api.v1.permissions import HasServiceToken
from api.v1.suppliers.bot_companies import BotCompanyError, BotCompanyIdentitySerializer, _binding, _memberships
from core.models import OrderItemStatus, OrderStatus, SupplierOrderRequestStatus
from notifications.models import NotificationAudience, NotificationDestination, NotificationSupplierAction


class BotOrdersQuerySerializer(BotCompanyIdentitySerializer):
    supplier_id = serializers.UUIDField()
    limit = serializers.IntegerField(min_value=1, max_value=10, default=10)
    offset = serializers.IntegerField(min_value=0, max_value=1000000000, default=0)


class BotOrderItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    position = serializers.IntegerField()
    name = serializers.CharField()
    quantity = serializers.CharField()
    unit = serializers.CharField(allow_blank=True)
    status = serializers.CharField()


class BotOrderSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    number = serializers.CharField()
    supplier_id = serializers.UUIDField()
    buyer_name = serializers.CharField()
    supplier_name = serializers.CharField()
    created_at = serializers.DateTimeField()
    viewed_at = serializers.DateTimeField(allow_null=True)
    status = serializers.ChoiceField(choices=SupplierOrderRequestStatus.choices)
    item_count = serializers.IntegerField()
    delivery_address = serializers.CharField(allow_blank=True)
    comment = serializers.CharField(allow_blank=True)
    items = BotOrderItemSerializer(many=True, required=False)
    can_respond = serializers.BooleanField(required=False)
    response_unavailable_reason = serializers.ChoiceField(
        choices=["", "already_answered", "request_closed", "no_pending_items", "chat_not_connected"], required=False
    )


class BotOrderPageSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    limit = serializers.IntegerField()
    offset = serializers.IntegerField()
    results = BotOrderSerializer(many=True)


def _company(data):
    binding = _binding(data)
    supplier_id = data["supplier_id"]
    if binding.supplier_id != supplier_id or not _memberships(binding).filter(supplier_id=supplier_id).exists():
        raise BotCompanyError("BOT_COMPANY_NOT_AVAILABLE", "Select an available company again.", 404)
    return binding


def _row(row, *, details=False):
    order = row.order
    result = {
        "id": row.id,
        "number": row.number,
        "supplier_id": row.supplier_id,
        "supplier_name": row.supplier.display_name,
        "buyer_name": order.venue.display_name,
        "created_at": row.created_at,
        "viewed_at": row.viewed_at,
        "status": row.status,
        "item_count": row.items_count,
        "delivery_address": order.delivery_address.address or "" if order.delivery_address_id else "",
        "comment": order.comment or "",
    }
    if details:
        result["items"] = [
            {
                "id": item.id,
                "position": index,
                "name": item.requested_name,
                "quantity": str(item.requested_qty),
                "unit": item.requested_unit.name if item.requested_unit_id else "",
                "status": item.status,
            }
            for index, item in enumerate(row.items.all(), 1)
        ]
    return result


class BotSupplierOrdersView(APIView):
    authentication_classes = []
    permission_classes = [HasServiceToken]

    @extend_schema(
        operation_id="bot_supplier_requests_list",
        tags=["Бот"],
        summary="Заявки выбранной компании для связанного пользователя бота",
        parameters=[BotOrdersQuerySerializer],
        responses={
            200: BotOrderPageSerializer,
            400: OpenApiResponse(description="Некорректные параметры"),
            403: OpenApiResponse(description="Нет сервисного доступа или аккаунт неактивен"),
            404: OpenApiResponse(description="Привязка или выбранная компания недоступна"),
        },
        auth=[{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
    )
    def get(self, request):
        serializer = BotOrdersQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        _company(data)
        rows = (
            supplier_order_requests_queryset(data["supplier_id"], {})
            .exclude(order__approval_status__in=("pending", "rework"))
            .select_related("order__delivery_address")
        )
        count = rows.count()
        offset, limit = data["offset"], data["limit"]
        return Response(
            BotOrderPageSerializer(
                {
                    "count": count,
                    "limit": limit,
                    "offset": offset,
                    "results": [_row(r) for r in rows[offset : offset + limit]],
                }
            ).data
        )


class BotSupplierOrderDetailView(APIView):
    authentication_classes = []
    permission_classes = [HasServiceToken]

    @extend_schema(
        operation_id="bot_supplier_requests_retrieve",
        tags=["Бот"],
        summary="Заявка по ID заявки поставщику; открытие отмечает её просмотренной",
        parameters=[BotOrdersQuerySerializer],
        responses={
            200: BotOrderSerializer,
            400: OpenApiResponse(description="Некорректные параметры"),
            403: OpenApiResponse(description="Нет сервисного доступа или аккаунт неактивен"),
            404: OpenApiResponse(description="Заявка, привязка или компания недоступна"),
        },
        auth=[{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
    )
    def get(self, request, request_id):
        serializer = BotOrdersQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        binding = _company(data)
        rows = (
            supplier_order_request_detail_queryset(data["supplier_id"])
            .exclude(order__approval_status__in=("pending", "rework"))
            .select_related("order__delivery_address")
            .annotate(items_count=Count("items"))
        )
        row = get_object_or_404(rows, pk=request_id)
        mark_supplier_order_request_viewed(row, actor=binding.user)
        result = _row(row, details=True)
        # Reuse the notification reply contract; a bot account binding alone
        # does not authorise actions on behalf of a notification chat.
        reason = ""
        if (
            row.status != SupplierOrderRequestStatus.SUBMITTED
            or NotificationSupplierAction.objects.filter(supplier_request=row).exists()
        ):
            reason = "already_answered"
        elif row.order.status != OrderStatus.SUBMITTED:
            reason = "request_closed"
        elif not any(item.status == OrderItemStatus.SUBMITTED for item in row.items.all()):
            reason = "no_pending_items"
        elif not NotificationDestination.objects.filter(
            audience=NotificationAudience.SUPPLIER,
            supplier_id=row.supplier_id,
            channel=data["platform"],
            chat_id=data["chat_id"],
            is_active=True,
        ).exists():
            reason = "chat_not_connected"
        result["can_respond"] = not reason
        result["response_unavailable_reason"] = reason
        return Response(BotOrderSerializer(result).data)
