from django.core.management.base import BaseCommand

from api.v1.catalog.embeddings import (
    build_supplier_product_embedding_text,
    catalog_embedding_model,
    catalog_qdrant_collection,
    ensure_product_for_supplier_product,
    sync_supplier_products_to_qdrant,
)
from core.models import EntityStatus
from suppliers.models import SupplierProduct


class Command(BaseCommand):
    help = "Generate MiniLM embeddings and synchronize active catalog products to Qdrant Cloud."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--batch-size", type=int, default=128)

    def handle(self, *args, **options):
        queryset = (
            SupplierProduct.objects.select_related(
                "supplier", "product", "unit", "brand", "category", "category__parent", "category__parent__parent"
            )
            .filter(status=EntityStatus.ACTIVE)
            .order_by("supplier__display_name", "normalized_name", "id")
        )
        if options["limit"]:
            queryset = queryset[: options["limit"]]
        items = list(queryset)
        if options["dry_run"]:
            for item in items:
                if text := build_supplier_product_embedding_text(item):
                    self.stdout.write(f"would embed: {item.id} {text}")
            self.stdout.write(
                self.style.SUCCESS(
                    f"dry-run: products={len(items)}, model={catalog_embedding_model()}, collection={catalog_qdrant_collection()}"
                )
            )
            return
        for item in items:
            ensure_product_for_supplier_product(item)
        processed, skipped = sync_supplier_products_to_qdrant(
            items, force=options["force"], batch_size=max(1, options["batch_size"])
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"done: processed={processed}, skipped={skipped}, collection={catalog_qdrant_collection()}"
            )
        )
