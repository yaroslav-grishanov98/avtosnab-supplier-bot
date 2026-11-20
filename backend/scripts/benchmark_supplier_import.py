"""Замер этапов импорта товаров поставщика на папке реальных прайсов.

    DJANGO_SETTINGS_MODULE=<eval settings> python scripts/benchmark_supplier_import.py "<папка>" [фрагмент имени]

Запускать ТОЛЬКО на отдельной БД: скрипт создаёт поставщиков и товары. Настоящие вызовы LLM идут по ключу из settings.
"""

import glob
import json
import os
import sys
import time

import django

django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402

import api.v1.suppliers.product_import_selectors as selectors  # noqa: E402
import api.v1.suppliers.tasks as tasks  # noqa: E402
from core.models import EntityStatus  # noqa: E402
from suppliers.models import Supplier, SupplierProduct  # noqa: E402

TIMINGS = {}


def timed(name, key):
    original = getattr(selectors, name)

    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            TIMINGS[key] = TIMINGS.get(key, 0) + time.perf_counter() - started

    setattr(selectors, name, wrapper)
    return original


# analyze не должен реально ставить задачу в брокер -- обработку вызываем сами.
tasks.process_supplier_product_import_session_task.delay = lambda **kwargs: None
timed("parse_supplier_product_import_file", "parse")
timed("enrich_supplier_product_import_rows", "llm")
timed_enrich = selectors.enrich_supplier_product_import_rows  # обёртка с замером, не оригинал
timed("find_supplier_product_import_candidates", "match_name")
timed("_apply_sku_matches", "match_sku")


def run(path, supplier, *, with_llm):
    TIMINGS.clear()
    selectors.enrich_supplier_product_import_rows = timed_enrich if with_llm else (lambda **kwargs: {})
    upload = SimpleUploadedFile(os.path.basename(path), open(path, "rb").read())
    started = time.perf_counter()
    session = selectors.analyze_supplier_product_import(
        supplier=supplier, file=upload, source_file_name=os.path.basename(path)
    )
    analyze_total = time.perf_counter() - started
    started = time.perf_counter()
    session = selectors.process_supplier_product_import_session(session_id=session.id)
    process_total = time.perf_counter() - started
    llm = TIMINGS.get("llm", 0)
    match = TIMINGS.get("match_name", 0) + TIMINGS.get("match_sku", 0)
    return {
        "rows": session.rows_total,
        "parse": round(TIMINGS.get("parse", 0), 2),
        "db_write": round(analyze_total - TIMINGS.get("parse", 0), 2),
        "llm": round(llm, 2),
        "match": round(match, 2),
        "other_process": round(process_total - llm - match, 2),
        "total_process": round(process_total, 2),
        "session": session,
    }


def main():
    directory = sys.argv[1]
    name_filter = sys.argv[2] if len(sys.argv) > 2 else ""
    for path in sorted(glob.glob(os.path.join(directory, "*"))):
        if name_filter not in os.path.basename(path):
            continue
        supplier = Supplier.objects.create(legal_name=os.path.basename(path), display_name=os.path.basename(path))
        first = run(path, supplier, with_llm=True)
        session = first.pop("session")
        SupplierProduct.objects.bulk_create(
            [
                SupplierProduct(
                    supplier=supplier, supplier_name=i.name, normalized_name=i.name.lower(), status=EntityStatus.ACTIVE
                )
                for i in session.items.all()
            ],
            batch_size=500,
            ignore_conflicts=True,
        )
        second = run(path, supplier, with_llm=False)
        second.pop("session")
        print(
            json.dumps(
                {"file": os.path.basename(path)[:40], **first, "match_full_catalog": second["match"]},
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
