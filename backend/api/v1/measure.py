"""Формат веса/объёма нетто (net_value) при импорте товаров: до 3 знаков после запятой, без хвостовых нулей
(100.000 -> 100, 0.500 -> 0.5). В БД колонка остаётся Decimal(14,4)."""

from decimal import ROUND_HALF_UP, Decimal

from rest_framework import serializers

NET_VALUE_PLACES = 3
_QUANT = Decimal(1).scaleb(-NET_VALUE_PLACES)


def round_net_value(value):
    """Округлить до 3 знаков; None остаётся None."""
    if value is None:
        return None
    return Decimal(value).quantize(_QUANT, rounding=ROUND_HALF_UP)


def format_net_value(value):
    """Строка без лишних нулей: 100.000 -> "100", 0.5000 -> "0.5", 1.2345 -> "1.235"."""
    if value is None:
        return None
    rounded = round_net_value(value)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class NetValueField(serializers.DecimalField):
    """DecimalField для net_value: принимает до 4 знаков, сохраняет 3, отдаёт строку без хвостовых нулей."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 14)
        kwargs.setdefault("decimal_places", 4)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        return round_net_value(super().to_internal_value(data))

    def to_representation(self, value):
        if value is None:
            return None
        return format_net_value(value)
