"""Venue subdivisions, addresses and off-system suppliers."""

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
    venue_off_system_suppliers_queryset,
    venue_subdivisions_queryset,
)
from api.v1.catalog.serializers import (
    OffSystemSupplierCreateSerializer,
    OffSystemSupplierSerializer,
    OffSystemSupplierUpdateSerializer,
    VenueSubdivisionCreateSerializer,
    VenueSubdivisionSerializer,
)
from api.v1.catalog.services import (
    create_off_system_supplier,
    create_venue_subdivision,
    delete_off_system_supplier,
    delete_venue_subdivision,
    update_off_system_supplier,
)
from api.v1.permissions import (
    IsVenueMember,
)
from api.v1.venues.serializers import VenueAddressSerializer
from api.v1.venues.services import create_venue_address, delete_venue_address
from core.models import (
    OffSystemSupplier,
    Venue,
    VenueSubdivision,
)


@extend_schema_view(
    get=extend_schema(
        tags=["Подразделения"],
        summary="Список подразделений заведения",
        description="2026-08-26 sync call: плоский список названий (кухня/бар/зал), без вложенной структуры.",
        responses={
            200: VenueSubdivisionSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
    post=extend_schema(
        tags=["Подразделения"],
        summary="Создать подразделение",
        request=VenueSubdivisionCreateSerializer,
        responses={
            201: VenueSubdivisionSerializer,
            400: OpenApiResponse(description='Название уже занято: {"name": ["Запись с таким названием уже есть."]}'),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueSubdivisionListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        return Response(VenueSubdivisionSerializer(venue_subdivisions_queryset(venue_id), many=True).data)

    def post(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueSubdivisionCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        subdivision = create_venue_subdivision(venue=venue, name=ser.validated_data["name"])
        return Response(VenueSubdivisionSerializer(subdivision).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(
        tags=["Подразделения"],
        summary="Удалить подразделение",
        responses={
            204: None,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или подразделения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueSubdivisionDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def delete(self, request, venue_id, subdivision_id):
        get_object_or_404(Venue, pk=venue_id)
        subdivision = get_object_or_404(VenueSubdivision, pk=subdivision_id, venue_id=venue_id)
        delete_venue_subdivision(subdivision)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Адреса"],
        summary="Список адресов заведения",
        description=(
            "2026-09-03 (Витя, фронт): адреса заведения ведутся в кабинете (`GET/POST "
            "/venues/cabinet/addresses`), но та ручка требует отдельный cabinet-JWT — в контексте "
            "корзины/заявки (обычный core.User JWT + venue_id, как у `.../subdivisions` и `.../cart`) "
            "его нет. Эта ручка отдаёт тот же список `VenueAddress` через уже знакомый venue_id-паттерн, "
            "чтобы заполнить выбор «Адрес доставки» без второго токена — записи общие, не дублируются."
        ),
        responses={
            200: VenueAddressSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
    post=extend_schema(
        tags=["Адреса"],
        summary="Добавить адрес заведения",
        request=VenueAddressSerializer,
        responses={
            201: VenueAddressSerializer,
            400: OpenApiResponse(
                description='Не передан обязательный address_type: {"address_type": ["This field is required."]}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueAddressListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        return Response(VenueAddressSerializer(venue.addresses.all(), many=True).data)

    def post(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueAddressSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        address = create_venue_address(venue=venue, data=ser.validated_data)
        return Response(VenueAddressSerializer(address).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(
        tags=["Адреса"],
        summary="Удалить адрес заведения",
        responses={
            204: None,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или адреса с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueAddressDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def delete(self, request, venue_id, address_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        delete_venue_address(venue=venue, address_id=address_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Поставщики вне системы"],
        summary="Список поставщиков вне системы заведения",
        description="2026-08-26 sync call: ~40% поставщиков не зарегистрированы в системе (2026-08-21 call) — "
        "заводятся сюда как справочник, без цены/единицы измерения, статусы заявок им проставляются вручную.",
        responses={
            200: OffSystemSupplierSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
    post=extend_schema(
        tags=["Поставщики вне системы"],
        summary="Создать запись поставщика вне системы",
        request=OffSystemSupplierCreateSerializer,
        responses={
            201: OffSystemSupplierSerializer,
            400: OpenApiResponse(
                description='Название уже занято: {"display_name": ["Запись с таким названием уже есть."]}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class OffSystemSupplierListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        return Response(OffSystemSupplierSerializer(venue_off_system_suppliers_queryset(venue_id), many=True).data)

    def post(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = OffSystemSupplierCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        supplier = create_off_system_supplier(venue=venue, **ser.validated_data)
        return Response(OffSystemSupplierSerializer(supplier).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Поставщики вне системы"],
        summary="Изменить запись поставщика вне системы",
        request=OffSystemSupplierUpdateSerializer,
        responses={
            200: OffSystemSupplierSerializer,
            400: OpenApiResponse(
                description='Название уже занято: {"display_name": ["Запись с таким названием уже есть."]}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или поставщика с таким id нет: {"detail": "Not found."}'),
        },
    ),
    delete=extend_schema(
        tags=["Поставщики вне системы"],
        summary="Удалить запись поставщика вне системы",
        responses={
            204: None,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или поставщика с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class OffSystemSupplierDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def patch(self, request, venue_id, supplier_id):
        get_object_or_404(Venue, pk=venue_id)
        supplier = get_object_or_404(OffSystemSupplier, pk=supplier_id, venue_id=venue_id)
        ser = OffSystemSupplierUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        supplier = update_off_system_supplier(supplier, **ser.validated_data)
        return Response(OffSystemSupplierSerializer(supplier).data)

    def delete(self, request, venue_id, supplier_id):
        get_object_or_404(Venue, pk=venue_id)
        supplier = get_object_or_404(OffSystemSupplier, pk=supplier_id, venue_id=venue_id)
        delete_off_system_supplier(supplier)
        return Response(status=status.HTTP_204_NO_CONTENT)
