# Data-only migration, no schema change.
#
# 2026-08-12: Павел прислал Google Sheets (вкладка "Списки") с полным справочником
# группа/подгруппа/вид (см. catalog/migrations/0008_seed_category_taxonomy) и единиц
# измерения — 38 штук, заметно шире 5 базовых из core/0011_seed_base_units. По прямой
# просьбе пользователя досеяно. Тот же idempotent update_or_create-паттерн, что и 0011 —
# 5 уже существующих (кг/г/л/мл/шт) просто обновятся теми же значениями, без дублей.
#
# kind классифицирован вручную по каждой единице — таблица Павла не содержит эту колонку.
# Метр/квадратный метр не имеют отдельного UnitKind (модель знает только piece/weight/
# volume/package) — заведены как piece за неимением категории "длина"/"площадь"; если
# понадобится точная классификация, потребуется добавить новые значения в UnitKind
# (схемное изменение, не сделано без отдельного запроса). Кубический метр закономерно
# попадает в volume (1 м3 = 1000 л). Единицы-упаковки (банка/коробка/ящик и т.п.) без
# ratio_to_base — их вес/объём не фиксирован, зависит от конкретного товара.

from decimal import Decimal

from django.db import migrations

UNIT_SPECS = [
    # (code, name, kind, ratio_to_base)
    ("г", "Грамм", "weight", Decimal("0.001000")),
    ("кг", "Килограмм", "weight", Decimal("1.000000")),
    ("мл", "Миллилитр", "volume", Decimal("0.001000")),
    ("л", "Литр", "volume", Decimal("1.000000")),
    ("м3", "Кубический метр", "volume", Decimal("1000.000000")),
    ("шт", "Штука", "piece", Decimal("1.000000")),
    ("доз", "Доза", "piece", None),
    ("капс", "Капсула", "piece", None),
    ("компл", "Комплект", "piece", None),
    ("лист", "Лист", "piece", None),
    ("мест", "Место", "piece", None),
    ("наб", "Набор", "piece", None),
    ("пар", "Пара", "piece", None),
    ("порц", "Порция", "piece", None),
    ("пуч", "Пучок", "piece", None),
    ("рул", "Рулон", "piece", None),
    ("табл", "Таблетка", "piece", None),
    ("м", "Метр", "piece", None),
    ("м2", "Квадратный метр", "piece", None),
    ("бал", "Баллон", "package", None),
    ("бан", "Банка", "package", None),
    ("бл", "Блок", "package", None),
    ("бокс", "Бокс", "package", None),
    ("боч", "Бочка", "package", None),
    ("бут", "Бутылка", "package", None),
    ("вед", "Ведро", "package", None),
    ("кан", "Канистра", "package", None),
    ("кор", "Коробка", "package", None),
    ("лот", "Лоток", "package", None),
    ("меш", "Мешок", "package", None),
    ("пак", "Пакет", "package", None),
    ("пал", "Палета", "package", None),
    ("пач", "Пачка", "package", None),
    ("сет", "Сетка", "package", None),
    ("туба", "Туба", "package", None),
    ("уп", "Упаковка", "package", None),
    ("фл", "Флакон", "package", None),
    ("ящ", "Ящик", "package", None),
]


def seed_extended_units(apps, schema_editor):
    Unit = apps.get_model("core", "Unit")
    for code, name, kind, ratio_to_base in UNIT_SPECS:
        Unit.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "kind": kind,
                "ratio_to_base": ratio_to_base,
                "status": "active",
            },
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_seed_base_units"),
    ]

    operations = [
        migrations.RunPython(seed_extended_units, noop_reverse),
    ]
