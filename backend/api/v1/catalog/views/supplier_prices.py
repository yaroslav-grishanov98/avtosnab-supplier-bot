"""Supplier product prices per venue."""

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication
from api.v1.catalog.selectors import (
    supplier_product_venue_price_audit_log,
    supplier_product_venue_price_history_queryset,
)
from api.v1.catalog.serializers import (
    AuditLogEntrySerializer,
    SupplierProductVenuePriceListResponseSerializer,
    SupplierProductVenuePriceQuerySerializer,
    SupplierProductVenuePriceSerializer,
)
from api.v1.permissions import (
    HasSupplierProductVenuePriceAccess,
    HasSupplierProductVenuePriceObjectAccess,
)
from catalog.models import (
    SupplierProductVenuePrice,
)


class _SupplierProductVenuePricePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


@extend_schema_view(
    get=extend_schema(
        tags=["Мастер-справочник"],
        summary="История цен товара поставщика по компании",
        description="Только чтение — назначение и архивация индивидуальной цены переехали в ЛК поставщика "
        "(POST `/api/v1/suppliers/products/{id}/individual-prices`, 2026-09-07): цену заводит "
        "поставщик на свой товар, заведение может только видеть, что для него уже действует.",
        parameters=[
            OpenApiParameter(
                name="supplier_product_id", type=OpenApiTypes.UUID, required=True, description="ID товара поставщика."
            ),
            OpenApiParameter(
                name="venue_id", type=OpenApiTypes.UUID, required=True, description="ID компании (venue)."
            ),
            OpenApiParameter(name="page", type=OpenApiTypes.INT, required=False, description="Номер страницы."),
            OpenApiParameter(
                name="page_size", type=OpenApiTypes.INT, required=False, description="Размер страницы (макс. 100)."
            ),
        ],
        responses={
            200: SupplierProductVenuePriceListResponseSerializer,
            400: OpenApiResponse(
                description="supplier_product_id или venue_id не передан либо не является валидным UUID."
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="venue_id не передан, либо пользователь не admin и не owner/manager этой компании."
            ),
        },
    ),
)
class SupplierProductVenuePriceListView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasSupplierProductVenuePriceAccess]

    def get(self, request):
        ser = SupplierProductVenuePriceQuerySerializer(data=request.query_params)
        ser.is_valid(raise_exception=True)
        qs = supplier_product_venue_price_history_queryset(**ser.validated_data)
        paginator = _SupplierProductVenuePricePagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(SupplierProductVenuePriceSerializer(page, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Мастер-справочник"],
        summary="История архивации индивидуальной цены",
        responses={
            200: AuditLogEntrySerializer(many=True),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="price_id не найден, либо пользователь не admin и не owner/manager хотя бы одной из компаний этой цены."
            ),
        },
    ),
)
class SupplierProductVenuePriceAuditLogView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasSupplierProductVenuePriceObjectAccess]

    def get(self, request, price_id):
        get_object_or_404(SupplierProductVenuePrice, pk=price_id)
        return Response(AuditLogEntrySerializer(supplier_product_venue_price_audit_log(price_id), many=True).data)
