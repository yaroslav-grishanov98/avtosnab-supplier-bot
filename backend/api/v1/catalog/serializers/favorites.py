"""Venue favorites and wishlists."""

from rest_framework import serializers

from core.models import (
    EntityStatus,
    VenueAreaRole,
)
from suppliers.models import SupplierProduct


class VenueFavoriteProductSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID товара поставщика.")
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    supplier_name = serializers.CharField(
        source="supplier.display_name", read_only=True, help_text="Название компании-поставщика."
    )
    normalized_name = serializers.CharField(read_only=True, help_text="Обработанное название товара.")
    unit = serializers.CharField(
        source="unit.code", read_only=True, allow_null=True, help_text="Код единицы измерения."
    )
    package = serializers.CharField(read_only=True, allow_null=True, help_text="Расфасовка/упаковка товара.")


class VenueFavoriteSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID записи избранного.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения, которому принадлежит запись.")
    supplier_product_id = serializers.UUIDField(read_only=True, help_text="ID добавленного в избранное товара.")
    supplier_product = VenueFavoriteProductSerializer(read_only=True, help_text="Краткая карточка товара.")
    role = serializers.ChoiceField(
        choices=VenueAreaRole.choices,
        read_only=True,
        allow_null=True,
        help_text="Зона заведения, к которой отнесён товар: kitchen / bar / hall / manager.",
    )
    custom_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Собственное название товара, заданное заведением."
    )
    last_ordered_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Когда товар последний раз заказывали."
    )
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус записи: active / archived.",
    )


class VenueFavoriteCreateSerializer(serializers.Serializer):
    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier_product",
        help_text="ID товара, который нужно добавить в избранное.",
    )
    role = serializers.ChoiceField(
        choices=VenueAreaRole.values,
        required=False,
        allow_null=True,
        help_text="Зона заведения, к которой отнести товар: kitchen / bar / hall / manager.",
    )
    custom_name = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Собственное название товара для заведения."
    )


class WishlistSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID вишлиста.")
    user_id = serializers.UUIDField(read_only=True, help_text="ID пользователя-владельца вишлиста.")
    name = serializers.CharField(read_only=True, help_text="Название вишлиста, заданное пользователем.")
    items_count = serializers.IntegerField(read_only=True, default=0, help_text="Количество активных товаров в списке.")
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус списка: active / archived.",
    )


class WishlistCreateSerializer(serializers.Serializer):
    name = serializers.CharField(
        trim_whitespace=True,
        max_length=200,
        help_text="Название нового вишлиста. Должно быть уникальным среди активных списков пользователя.",
    )


class WishlistUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(trim_whitespace=True, max_length=200, help_text="Новое название вишлиста.")


class WishlistItemSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID записи товара в вишлисте.")
    wishlist_id = serializers.UUIDField(read_only=True, help_text="ID вишлиста, которому принадлежит запись.")
    supplier_product_id = serializers.UUIDField(read_only=True, help_text="ID товара, добавленного в вишлист.")
    supplier_product = VenueFavoriteProductSerializer(read_only=True, help_text="Краткая карточка товара.")
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус записи: active / archived.",
    )


class WishlistItemCreateSerializer(serializers.Serializer):
    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier_product",
        help_text="ID товара, который нужно добавить в вишлист.",
    )
