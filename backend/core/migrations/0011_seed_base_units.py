# Data-only migration, no schema change.
#
# 2026-08-09: Витя спросил "GET /suppliers/products создание товара — где брать
# weight_unit_id/volume_unit_id?" — оказалось, что этих ID вообще нет ни в одной среде.
# `core/management/commands/seed_units.py` (тот же idempotent update_or_create по code,
# 5 базовых единиц: кг/г/л/мл/шт) существует уже давно (см. историю коммитов вокруг
# "деплой seed_units прошёл, ждём Александра для разового запуска на проде"), но
# management-команды не запускаются автоматически при деплое — только `migrate`
# (проверено этой же сессией через логи миграций на dev-стенде) — а разовый ручной
# запуск так и не случился. Данные так и не появились ни на dev, ни (предположительно)
# на проде. Перенесено в data-migration с той же idempotent-логикой, чтобы сработало
# само при следующем автодеплое, без ожидания ручного вмешательства.

from decimal import Decimal

from django.db import migrations

UNIT_SPECS = [
    ("кг", "Килограмм", "weight", Decimal("1.000000")),
    ("г", "Грамм", "weight", Decimal("0.001000")),
    ("л", "Литр", "volume", Decimal("1.000000")),
    ("мл", "Миллилитр", "volume", Decimal("0.001000")),
    ("шт", "Штука", "piece", Decimal("1.000000")),
]


def seed_base_units(apps, schema_editor):
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
        ("core", "0010_external_auth_id_unique_per_role"),
    ]

    operations = [
        migrations.RunPython(seed_base_units, noop_reverse),
    ]
