from suppliers.models import PriceStatus, PriceType, SupplierProduct, SupplierProductPrice


def sync_supplier_product_price_snapshot(supplier_product_id):
    """Recompute SupplierProduct.current_price/previous_price/price_changed_at
    from SupplierProductPrice rows for one product. Lives here (not in
    api/v1/suppliers/services.py) so it has no dependency on the api app —
    it's called both explicitly from api/v1/suppliers/services.py after bulk
    .update() status transitions (which don't fire signals) and from
    suppliers/signals.py's post_save receiver (covers .create()/.save(), i.e.
    every other code path including tests that create price rows directly)."""
    current = (
        SupplierProductPrice.objects.filter(
            supplier_product_id=supplier_product_id,
            price_type=PriceType.PRICE_LIST,
            status=PriceStatus.ACTIVE,
            valid_to__isnull=True,
        )
        .order_by("-valid_from", "-created_at")
        .first()
    )
    previous = (
        SupplierProductPrice.objects.filter(
            supplier_product_id=supplier_product_id,
            price_type=PriceType.PRICE_LIST,
            status=PriceStatus.SUPERSEDED,
        )
        .order_by("-valid_to", "-created_at")
        .first()
    )
    SupplierProduct.objects.filter(pk=supplier_product_id).update(
        current_price=current.price if current else None,
        previous_price=previous.price if previous else None,
        price_changed_at=previous.valid_to if previous else None,
    )
