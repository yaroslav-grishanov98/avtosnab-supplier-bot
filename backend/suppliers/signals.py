from django.db.models.signals import post_save
from django.dispatch import receiver

from suppliers.models import SupplierProductPrice
from suppliers.services import sync_supplier_product_price_snapshot


@receiver(post_save, sender=SupplierProductPrice)
def _on_supplier_product_price_saved(sender, instance, **kwargs):
    # Safety net for the denormalized price snapshot (see suppliers/services.py):
    # covers any .create()/.save() on SupplierProductPrice, including test setup
    # and future code paths that don't go through api/v1/suppliers/services.py's
    # explicit sync calls (those still handle .update()-based bulk status
    # transitions, which don't fire this signal).
    sync_supplier_product_price_snapshot(instance.supplier_product_id)
