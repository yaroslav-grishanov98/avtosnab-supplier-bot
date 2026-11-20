from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from api.v1.suppliers.product_import_services import commit_supplier_product_import_session
from core.models import Unit, UnitKind
from suppliers.models import (
    ProductAvailability,
    Supplier,
    SupplierProductImportAction,
    SupplierProductImportItem,
    SupplierProductImportSession,
)

ROWS = 20


def _ready_session(rows):
    supplier = Supplier.objects.create(legal_name="Perf LLC", display_name="Perf LLC")
    unit = Unit.objects.create(code="perf-kg", name="perf-kg", kind=UnitKind.PIECE)
    session = SupplierProductImportSession.objects.create(supplier=supplier)
    SupplierProductImportItem.objects.bulk_create(
        SupplierProductImportItem(
            session=session,
            row_number=number,
            raw_name=f"Товар {number}",
            name=f"Товар {number}",
            unit=unit,
            country_of_origin="Россия",
            price=Decimal("10"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("1"),
            net_unit=unit,
            min_order_qty=Decimal("1"),
            availability=ProductAvailability.IN_STOCK,
            action=SupplierProductImportAction.NEW,
        )
        for number in range(1, rows + 1)
    )
    return session


class CommitQueryBudgetTests(TestCase):
    def test_commit_query_count_per_row_is_bounded(self):
        session = _ready_session(ROWS)

        with CaptureQueriesContext(connection) as queries:
            result = commit_supplier_product_import_session(session=session)

        self.assertEqual(result["imported_count"], ROWS)
        # До оптимизации было 371 запрос на 20 строк (18.6 на строку, замер 2026-10-07).
        self.assertLessEqual(len(queries), ROWS * 15 + 10, f"{len(queries)} запросов на {ROWS} строк")
