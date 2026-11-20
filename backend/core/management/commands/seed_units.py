from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import EntityStatus, Unit, UnitKind

UNIT_SPECS = [
    ("кг", "Килограмм", UnitKind.WEIGHT, Decimal("1.000000")),
    ("г", "Грамм", UnitKind.WEIGHT, Decimal("0.001000")),
    ("л", "Литр", UnitKind.VOLUME, Decimal("1.000000")),
    ("мл", "Миллилитр", UnitKind.VOLUME, Decimal("0.001000")),
    ("шт", "Штука", UnitKind.PIECE, Decimal("1.000000")),
]


class Command(BaseCommand):
    help = (
        "Create idempotent base units (кг/г/л/мл/шт) — required for supplier price normalization to resolve raw_unit."
    )

    @transaction.atomic
    def handle(self, *args, **options):
        created_or_updated = []
        for code, name, kind, ratio_to_base in UNIT_SPECS:
            unit, _ = Unit.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "kind": kind,
                    "ratio_to_base": ratio_to_base,
                    "status": EntityStatus.ACTIVE,
                },
            )
            created_or_updated.append(unit.code)

        if options["verbosity"] > 0:
            self.stdout.write(self.style.SUCCESS(f"Units ready: {', '.join(created_or_updated)}"))
