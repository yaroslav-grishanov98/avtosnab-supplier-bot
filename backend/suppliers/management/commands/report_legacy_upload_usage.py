from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Max
from django.db.models.functions import TruncWeek
from django.utils import timezone

from suppliers.models import SupplierPriceUpload


class Command(BaseCommand):
    help = (
        "Инвентаризация legacy /suppliers/uploads/*: загрузки за период по каналам (telegram/max/web/api/…) и по "
        "неделям. Только чтение -- по этим данным решают, кого переводить на новый flow."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90, help="Период в днях (по умолчанию 90).")

    def handle(self, *args, **options):
        days = options["days"]
        since = timezone.now() - timedelta(days=days)
        uploads = SupplierPriceUpload.objects.filter(created_at__gte=since)

        self.stdout.write(f"Всего загрузок за {days} дн.: {uploads.count()}")
        by_channel = (
            uploads.values("source_channel")
            .annotate(uploads=Count("id"), suppliers=Count("supplier_id", distinct=True), last=Max("created_at"))
            .order_by("-uploads", "source_channel")
        )
        if not by_channel:
            self.stdout.write("Legacy-загрузок за период не было.")
            return

        self.stdout.write("\nПо каналам (канал, загрузок, поставщиков, последняя):")
        for row in by_channel:
            self.stdout.write(
                f"{row['source_channel']:<14}{row['uploads']:>8}{row['suppliers']:>8}   {row['last']:%Y-%m-%d}"
            )

        self.stdout.write("\nПо неделям (начало недели, загрузок):")
        weekly = (
            uploads.annotate(week=TruncWeek("created_at")).values("week").annotate(uploads=Count("id")).order_by("week")
        )
        for row in weekly:
            self.stdout.write(f"{row['week']:%Y-%m-%d}{row['uploads']:>8}")
