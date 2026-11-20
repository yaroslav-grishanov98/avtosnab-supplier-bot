"""Venue favorites and wishlists."""

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication, OptionalCoreUserJWTAuthentication
from api.v1.catalog.selectors import (
    user_wishlists_queryset,
    venue_favorites_queryset,
    wishlist_items_queryset,
)
from api.v1.catalog.serializers import (
    VenueFavoriteCreateSerializer,
    VenueFavoriteSerializer,
    WishlistCreateSerializer,
    WishlistItemCreateSerializer,
    WishlistItemSerializer,
    WishlistSerializer,
    WishlistUpdateSerializer,
)
from api.v1.catalog.services import (
    add_venue_favorite,
    add_wishlist_item,
    archive_wishlist,
    create_wishlist,
    remove_venue_favorite,
    remove_wishlist_item,
    rename_wishlist,
)
from api.v1.permissions import (
    IsCoreUser,
    IsVenueMember,
)
from catalog.models import (
    Wishlist,
    WishlistItem,
)
from core.models import (
    EntityStatus,
    Venue,
)


@extend_schema_view(
    get=extend_schema(
        tags=["Избранное"],
        summary="Список избранных товаров заведения",
        description="Возвращает все активные записи избранного для указанного заведения.",
        responses={200: VenueFavoriteSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Избранное"],
        summary="Добавить товар в избранное",
        description=(
            "Добавляет товар поставщика в избранное заведения. "
            "Идемпотентно: если уже есть — обновляет роль/название и возвращает 201."
        ),
        request=VenueFavoriteCreateSerializer,
        responses={201: VenueFavoriteSerializer},
    ),
)
class VenueFavoriteView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        favorites = venue_favorites_queryset(venue_id)
        return Response(VenueFavoriteSerializer(favorites, many=True).data)

    def post(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueFavoriteCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        fav = add_venue_favorite(
            venue=venue,
            supplier_product=ser.validated_data["supplier_product"],
            role=ser.validated_data.get("role"),
            custom_name=ser.validated_data.get("custom_name"),
        )
        fav.refresh_from_db()
        return Response(
            VenueFavoriteSerializer(fav).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    delete=extend_schema(
        tags=["Избранное"],
        summary="Убрать товар из избранного",
        description="Архивирует запись избранного (физически не удаляет). Возвращает 204.",
        responses={204: None},
    ),
)
class VenueFavoriteDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def delete(self, request, venue_id, favorite_id):
        get_object_or_404(Venue, pk=venue_id)
        from catalog.models import VenueSupplierProduct
        from core.models import EntityStatus

        fav = get_object_or_404(
            VenueSupplierProduct,
            pk=favorite_id,
            venue_id=venue_id,
            status=EntityStatus.ACTIVE,
        )
        remove_venue_favorite(fav)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _get_own_wishlist(request, wishlist_id):
    return get_object_or_404(Wishlist, pk=wishlist_id, user=request.user, status=EntityStatus.ACTIVE)


@extend_schema_view(
    get=extend_schema(
        tags=["Вишлисты"],
        summary="Мои вишлисты",
        description="Список именованных списков товаров текущего пользователя.",
        responses={200: WishlistSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Вишлисты"],
        summary="Создать вишлист",
        request=WishlistCreateSerializer,
        responses={201: WishlistSerializer},
    ),
)
class WishlistView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        wishlists = user_wishlists_queryset(request.user)
        return Response(WishlistSerializer(wishlists, many=True).data)

    def post(self, request):
        ser = WishlistCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        wishlist = create_wishlist(user=request.user, name=ser.validated_data["name"])
        wishlist.items_count = 0
        return Response(WishlistSerializer(wishlist).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Вишлисты"],
        summary="Переименовать вишлист",
        request=WishlistUpdateSerializer,
        responses={200: WishlistSerializer},
    ),
    delete=extend_schema(
        tags=["Вишлисты"],
        summary="Удалить вишлист",
        description="Архивирует список и все его позиции (физически не удаляет). Возвращает 204.",
        responses={204: None},
    ),
)
class WishlistDetailView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def patch(self, request, wishlist_id):
        wishlist = _get_own_wishlist(request, wishlist_id)
        ser = WishlistUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        wishlist = rename_wishlist(wishlist, ser.validated_data["name"])
        wishlist.items_count = wishlist.items.filter(status=EntityStatus.ACTIVE).count()
        return Response(WishlistSerializer(wishlist).data)

    def delete(self, request, wishlist_id):
        wishlist = _get_own_wishlist(request, wishlist_id)
        archive_wishlist(wishlist)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Вишлисты"],
        summary="Товары в вишлисте",
        responses={200: WishlistItemSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Вишлисты"],
        summary="Добавить товар в вишлист",
        description="Идемпотентно: если товар уже в списке — просто возвращает 201.",
        request=WishlistItemCreateSerializer,
        responses={201: WishlistItemSerializer},
    ),
)
class WishlistItemView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request, wishlist_id):
        wishlist = _get_own_wishlist(request, wishlist_id)
        items = wishlist_items_queryset(wishlist)
        return Response(WishlistItemSerializer(items, many=True).data)

    def post(self, request, wishlist_id):
        wishlist = _get_own_wishlist(request, wishlist_id)
        ser = WishlistItemCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        item = add_wishlist_item(wishlist=wishlist, supplier_product=ser.validated_data["supplier_product"])
        item.refresh_from_db()
        return Response(WishlistItemSerializer(item).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(
        tags=["Вишлисты"],
        summary="Убрать товар из вишлиста",
        responses={204: None},
    ),
)
class WishlistItemDetailView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def delete(self, request, wishlist_id, item_id):
        wishlist = _get_own_wishlist(request, wishlist_id)
        item = get_object_or_404(WishlistItem, pk=item_id, wishlist=wishlist, status=EntityStatus.ACTIVE)
        remove_wishlist_item(item)
        return Response(status=status.HTTP_204_NO_CONTENT)
