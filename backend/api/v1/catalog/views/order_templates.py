"""Venue order templates."""

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import OptionalCoreUserJWTAuthentication
from api.v1.catalog.selectors import (
    cart_detail_queryset,
    venue_order_templates_queryset,
)
from api.v1.catalog.serializers import (
    OrderTemplateSerializer,
    SaveOrderAsTemplateSerializer,
    VenueCartSerializer,
)
from api.v1.catalog.services import (
    apply_order_template,
    delete_order_template,
    get_or_create_cart,
    save_cart_as_template,
)
from api.v1.permissions import (
    IsVenueMember,
)
from core.models import (
    EntityStatus,
    OrderTemplate,
    User,
    Venue,
)


@extend_schema_view(
    get=extend_schema(
        tags=["Шаблоны заявок"],
        summary="Список шаблонов заявки",
        description="Со скриншота прототипа 2026-08-26 — не обсуждалось устно ни на одном созвоне.",
        responses={
            200: OrderTemplateSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class OrderTemplateListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        return Response(OrderTemplateSerializer(venue_order_templates_queryset(venue_id), many=True).data)


@extend_schema_view(
    delete=extend_schema(
        tags=["Шаблоны заявок"],
        summary="Удалить шаблон заявки",
        responses={
            204: None,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или шаблона с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class OrderTemplateDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def delete(self, request, venue_id, template_id):
        get_object_or_404(Venue, pk=venue_id)
        template = get_object_or_404(OrderTemplate, pk=template_id, venue_id=venue_id)
        delete_order_template(template)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    post=extend_schema(
        tags=["Шаблоны заявок"],
        summary="Сохранить корзину как шаблон",
        description="Снимок текущих позиций корзины этого заведения в новый именованный шаблон.",
        request=SaveOrderAsTemplateSerializer,
        responses={
            201: OrderTemplateSerializer,
            400: OpenApiResponse(description='Название уже занято: {"name": ["Запись с таким названием уже есть."]}'),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но нет активного membership в этом заведении."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class OrderSaveAsTemplateView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        if not isinstance(request.user, User):
            raise PermissionDenied(
                "Requires a core.User account — carts are account-scoped, not a service-token action."
            )
        ser = SaveOrderAsTemplateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        cart = get_or_create_cart(request.user, venue)
        template = save_cart_as_template(cart=cart, name=ser.validated_data["name"])
        template.items_count = template.items.count()
        return Response(OrderTemplateSerializer(template).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        tags=["Шаблоны заявок"],
        summary="Добавить шаблон в корзину",
        description=(
            "2026-08-27, по фидбеку Вити: добавляет позиции шаблона в мою текущую корзину "
            "(`get_or_create_cart` — та же логика, что у `GET /cart`), а не создаёт отдельную новую заявку "
            "каждый раз. Если в корзине уже есть позиции — позиции шаблона добавляются к уже имеющимся, а "
            "не заменяют их."
        ),
        request=None,
        responses={
            200: VenueCartSerializer,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или шаблона с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class OrderTemplateApplyView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id, template_id):
        get_object_or_404(Venue, pk=venue_id)
        if not isinstance(request.user, User):
            raise PermissionDenied(
                "Requires a core.User account — carts are account-scoped, not a service-token action."
            )
        template = get_object_or_404(OrderTemplate, pk=template_id, venue_id=venue_id, status=EntityStatus.ACTIVE)
        cart = apply_order_template(template=template, user=request.user)
        cart = get_object_or_404(cart_detail_queryset(request.user.id, venue_id), pk=cart.pk)
        return Response(VenueCartSerializer(cart, context={"request": request}).data)
