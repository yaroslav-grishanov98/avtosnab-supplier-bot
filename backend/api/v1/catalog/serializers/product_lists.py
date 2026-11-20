"""Venue product lists."""

from rest_framework import serializers

from catalog.models import (
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
)


class VenueProductListSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID списка.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения.")
    name = serializers.CharField(read_only=True, help_text="Название списка.")
    items_count = serializers.IntegerField(read_only=True, default=0, help_text="Количество активных товаров в списке.")
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус списка: active / archived.",
    )


class VenueProductListCreateSerializer(serializers.Serializer):
    name = serializers.CharField(
        trim_whitespace=True,
        max_length=200,
        help_text="Название нового списка. Должно быть уникальным среди активных списков этого заведения.",
    )


class VenueProductListUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(trim_whitespace=True, max_length=200, help_text="Новое название списка.")


class VenueProductListItemCreateSerializer(serializers.Serializer):
    item_id = serializers.PrimaryKeyRelatedField(
        queryset=VenueProductMatrixItem.objects.filter(status=EntityStatus.ACTIVE),
        source="item",
        help_text="ID строки товарной матрицы, которую нужно добавить в список.",
    )
