"""Venue product lists."""

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import OptionalCoreUserJWTAuthentication
from api.v1.catalog.selectors import (
    venue_product_list_items_queryset,
    venue_product_lists_queryset,
)
from api.v1.catalog.serializers import (
    VenueProductListCreateSerializer,
    VenueProductListItemCreateSerializer,
    VenueProductListSerializer,
    VenueProductListUpdateSerializer,
    VenueProductMatrixItemSerializer,
)
from api.v1.catalog.services import (
    add_matrix_item_to_list,
    archive_venue_product_list,
    create_venue_product_list,
    remove_matrix_item_from_list,
    rename_venue_product_list,
)
from api.v1.permissions import (
    IsVenueMember,
)
from catalog.models import (
    VenueProductList,
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
    Venue,
)


@extend_schema_view(
    get=extend_schema(
        tags=["Списки"],
        summary="Списки товарной матрицы заведения",
        description=(
            "2026-08-31 design (спека Мастер-справочника §3.4): именованные группировки товаров "
            "заведения («Для бара», «Для кухни»), один товар может входить в несколько списков."
        ),
        responses={
            200: VenueProductListSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
    post=extend_schema(
        tags=["Списки"],
        summary="Создать список",
        request=VenueProductListCreateSerializer,
        responses={
            201: VenueProductListSerializer,
            400: OpenApiResponse(description='Название уже занято: {"name": ["Запись с таким названием уже есть."]}'),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        return Response(VenueProductListSerializer(venue_product_lists_queryset(venue_id), many=True).data)

    def post(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueProductListCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        product_list = create_venue_product_list(venue=venue, name=ser.validated_data["name"])
        product_list.items_count = 0
        return Response(VenueProductListSerializer(product_list).data, status=status.HTTP_201_CREATED)


def _get_active_venue_product_list(venue_id, list_id):
    return get_object_or_404(VenueProductList, pk=list_id, venue_id=venue_id, status=EntityStatus.ACTIVE)


@extend_schema_view(
    patch=extend_schema(
        tags=["Списки"],
        summary="Переименовать список",
        request=VenueProductListUpdateSerializer,
        responses={
            200: VenueProductListSerializer,
            400: OpenApiResponse(description='Название уже занято: {"name": ["Запись с таким названием уже есть."]}'),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или списка с таким id нет: {"detail": "Not found."}'),
        },
    ),
    delete=extend_schema(
        tags=["Списки"],
        summary="Архивировать список",
        description="Архивирует список (не удаляет). Товары матрицы, входившие в этот список, не затрагиваются.",
        responses={
            204: None,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или списка с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductListDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def patch(self, request, venue_id, list_id):
        get_object_or_404(Venue, pk=venue_id)
        product_list = _get_active_venue_product_list(venue_id, list_id)
        ser = VenueProductListUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        product_list = rename_venue_product_list(product_list, ser.validated_data["name"])
        product_list.items_count = product_list.items.filter(status=EntityStatus.ACTIVE).count()
        return Response(VenueProductListSerializer(product_list).data)

    def delete(self, request, venue_id, list_id):
        get_object_or_404(Venue, pk=venue_id)
        product_list = _get_active_venue_product_list(venue_id, list_id)
        archive_venue_product_list(product_list)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Списки"],
        summary="Товары в списке",
        responses={
            200: VenueProductMatrixItemSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или списка с таким id нет: {"detail": "Not found."}'),
        },
    ),
    post=extend_schema(
        tags=["Списки"],
        summary="Добавить товар в список",
        description="Идемпотентно: если товар уже в списке — просто возвращает 201.",
        request=VenueProductListItemCreateSerializer,
        responses={
            201: VenueProductMatrixItemSerializer,
            400: OpenApiResponse(
                description=(
                    'item_id не найден: {"item_id": ["Invalid pk ... - object does not exist."]} ИЛИ '
                    'товар из другого заведения: {"item_id": "Товар принадлежит другому заведению."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или списка с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductListItemView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id, list_id):
        get_object_or_404(Venue, pk=venue_id)
        product_list = _get_active_venue_product_list(venue_id, list_id)
        items = venue_product_list_items_queryset(product_list)
        return Response(VenueProductMatrixItemSerializer(items, many=True).data)

    def post(self, request, venue_id, list_id):
        get_object_or_404(Venue, pk=venue_id)
        product_list = _get_active_venue_product_list(venue_id, list_id)
        ser = VenueProductListItemCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        item = add_matrix_item_to_list(product_list=product_list, item=ser.validated_data["item"])
        item = VenueProductMatrixItem.objects.filter(pk=item.pk).prefetch_related("lists").get()
        return Response(VenueProductMatrixItemSerializer(item).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(
        tags=["Списки"],
        summary="Убрать товар из списка",
        description="Снимает связь — сама строка товарной матрицы не удаляется.",
        responses={
            204: None,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения, списка или товара с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductListItemDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def delete(self, request, venue_id, list_id, item_id):
        get_object_or_404(Venue, pk=venue_id)
        product_list = _get_active_venue_product_list(venue_id, list_id)
        item = get_object_or_404(VenueProductMatrixItem, pk=item_id, venue_id=venue_id, lists=product_list)
        remove_matrix_item_from_list(product_list=product_list, item=item)
        return Response(status=status.HTTP_204_NO_CONTENT)
