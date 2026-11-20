"""Venue subdivisions and off-system suppliers."""

from rest_framework import serializers

from core.models import (
    EntityStatus,
)


class VenueSubdivisionSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID подразделения.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения.")
    name = serializers.CharField(read_only=True, help_text="Название подразделения (например «Кухня», «Бар»).")
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус записи: active / archived.",
    )


class VenueSubdivisionCreateSerializer(serializers.Serializer):
    name = serializers.CharField(
        trim_whitespace=True,
        max_length=200,
        help_text="Название подразделения. Должно быть уникальным среди активных подразделений заведения.",
    )


class OffSystemSupplierSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID поставщика вне системы.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения, которому принадлежит запись.")
    display_name = serializers.CharField(read_only=True, help_text="Название поставщика.")
    phone = serializers.CharField(read_only=True, allow_null=True, help_text="Телефон для ручной связи.")
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Заметка снабженца.")
    delivery_min_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="2026-09-25 (задача #Лк1): минимальная сумма заказа, проставляется снабженцем вручную (нет "
        "прайса/API, чтобы получить это от самого поставщика вне системы) — тот же смысл, что "
        "Supplier.delivery_min_order_amount у обычного поставщика. null — минимум не указан.",
    )
    delivery_currency = serializers.CharField(
        read_only=True, help_text="Код валюты delivery_min_order_amount, обычно RUB."
    )
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус записи: active / archived.",
    )


class OffSystemSupplierCreateSerializer(serializers.Serializer):
    display_name = serializers.CharField(
        trim_whitespace=True,
        max_length=300,
        help_text="Название поставщика. Должно быть уникальным среди активных записей заведения.",
    )
    phone = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Телефон для связи.")
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Заметка снабженца.")
    delivery_min_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=0,
        help_text="Минимальная сумма заказа (задача #Лк1). Необязательно — по умолчанию не задана.",
    )
    delivery_currency = serializers.CharField(
        required=False,
        max_length=3,
        default="RUB",
        help_text="Код валюты delivery_min_order_amount. По умолчанию RUB.",
    )


class OffSystemSupplierUpdateSerializer(serializers.Serializer):
    display_name = serializers.CharField(
        required=False, trim_whitespace=True, max_length=300, help_text="Новое название поставщика."
    )
    phone = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Новый телефон.")
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Новая заметка.")
    delivery_min_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=0,
        help_text="Новая минимальная сумма заказа. null очищает (минимум не указан).",
    )
    delivery_currency = serializers.CharField(
        required=False, max_length=3, help_text="Новый код валюты delivery_min_order_amount."
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs
