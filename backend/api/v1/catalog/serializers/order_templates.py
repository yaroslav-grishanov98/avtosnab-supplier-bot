"""Venue order templates."""

from rest_framework import serializers

from core.models import (
    EntityStatus,
)


class OrderTemplateSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID шаблона заявки.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения.")
    name = serializers.CharField(read_only=True, help_text="Название шаблона.")
    items_count = serializers.IntegerField(read_only=True, default=0, help_text="Количество позиций в шаблоне.")
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус шаблона: active / archived.",
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания шаблона.")


class SaveOrderAsTemplateSerializer(serializers.Serializer):
    """2026-09-18: venue_id убран отсюда — теперь берётся из URL
    (venues/{venue_id}/cart/save-as-template), корзина снова
    (user, venue)-scoped."""

    name = serializers.CharField(
        trim_whitespace=True,
        max_length=200,
        help_text="Название нового шаблона. Должно быть уникальным среди активных шаблонов заведения.",
    )
