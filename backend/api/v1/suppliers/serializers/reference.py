"""Reference lists for the supplier cabinet."""

from rest_framework import serializers

from core.models import UnitKind


class SupplierUnitListQuerySerializer(serializers.Serializer):
    kind = serializers.ChoiceField(
        choices=UnitKind.choices, required=False, help_text="Фильтр по типу: weight / volume / piece / package."
    )


class SupplierUnitSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID единицы измерения — сюда передавать в unit_id и т.п.")
    code = serializers.CharField(read_only=True, help_text="Короткий код, например 'кг'.")
    name = serializers.CharField(read_only=True, help_text="Полное название, например 'Килограмм'.")
    kind = serializers.ChoiceField(
        choices=UnitKind.choices, read_only=True, help_text="Тип: weight/volume/piece/package."
    )


class SupplierCountrySerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True, help_text="Название страны — сюда передавать в country_of_origin.")


class SupplierBrandListQuerySerializer(serializers.Serializer):
    q = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Подстрока для поиска по уже введённым на платформе брендам (автокомплит).",
    )


class SupplierBrandSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID бренда.")
    name = serializers.CharField(source="canonical_name", read_only=True, help_text="Название бренда.")


class SupplierCategorySerializer(serializers.Serializer):
    # 2026-08-14: старый текст ("сюда передавать элементом category_path")
    # был неверным и опасным — category_path резолвится по имени
    # (_resolve_category_path() в services.py), не по id. Если этот совет
    # выполнить буквально, создаётся мусорная категория с UUID вместо
    # названия — так на проде появились категории "45cbcd3a-..." и
    # "7e7ae0f2-..." (see docs/wiki/logs/2026-08-14-category-taxonomy-cleanup.md).
    id = serializers.UUIDField(
        read_only=True,
        help_text="ID категории — для ссылок на конкретную запись. В category_path (создание/обновление "
        "товара) передаются названия, а не этот id.",
    )
    name = serializers.CharField(read_only=True, help_text="Название (группы/подгруппы/вида).")
    parent_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID родителя: null для группы, group_id для подгруппы и т.д."
    )
    depth = serializers.IntegerField(
        read_only=True, help_text="Уровень вложенности: 0 — группа, 1 — подгруппа, 2 — вид."
    )
