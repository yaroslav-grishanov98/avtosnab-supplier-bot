# ruff: noqa
"""Live re-run of the supplier import on the real stack (Django runserver + Celery + PostgreSQL + Redis), LLM off.

Runs as the cabinet user would over HTTP: analyze -> poll -> read rows -> fill defaults -> commit -> poll.
Writes results.json, items-before-N.json, db-evidence.json next to this file. Local use only.
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, "/home/itmedia_pc/projects/auto-snab-import-quality")
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rerun_settings")
import django

django.setup()

from django.contrib.auth.hashers import make_password

from api.v1.suppliers.services import issue_supplier_cabinet_tokens
from core.models import EntityStatus, User, UserRole
from suppliers.models import (
    Supplier,
    SupplierProduct,
    SupplierProductImportSession,
    SupplierProductPrice,
    SupplierUser,
    SupplierUserRole,
)

ROOT = Path(__file__).parent
DOWNLOADS = Path("/mnt/c/Users/itmedia pc/Downloads")
BASE = "http://127.0.0.1:8010/api/v1/suppliers/products/import"
NAMES = [
    "Очаково (ООО Компания Цефей, ООО Мэджик, ИП Иванов О.Н.) Прайс-лист 22.05.26.xlsx",
    "Натурово Прайс 25.05.26.xlsx",
    "Левина ИП Бакалея Прайс Бакалея Февраль 22.05.26.xlsx",
    "Левина ИП Охлажденка Прайс Охлажденка Хорека Январь 22.05.26.xls",
]


def call(method, url, token, *, body=None, headers=None):
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read() or b"{}"), time.perf_counter() - started
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}"), time.perf_counter() - started


def multipart(name, content):
    boundary = uuid.uuid4().hex
    mime = (
        "application/vnd.ms-excel"
        if name.endswith(".xls")
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode(), {
        "Content-Type": f"multipart/form-data; boundary={boundary}"
    }


def main():
    results = []
    evidence = {"settings": {}, "suppliers": []}
    from django.conf import settings

    for key in (
        "CANONICAL_SUPPLIER_PARSER_ENABLED",
        "CANONICAL_IMPORT_LLM_ENABLED",
        "SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC",
        "SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED",
        "CELERY_TASK_ALWAYS_EAGER",
    ):
        evidence["settings"][key] = getattr(settings, key)
    for index, name in enumerate(NAMES, 1):
        content = (DOWNLOADS / name).read_bytes()
        supplier = Supplier.objects.create(
            legal_name=f"Live Rerun {index}", display_name=f"Live Rerun {index}", status=EntityStatus.ACTIVE
        )
        user = User.objects.create(
            email=f"rerun{index}@autosnab.local",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
            full_name=f"Rerun {index}",
            password=make_password("LocalRerun2026!"),
        )
        SupplierUser.objects.create(
            user=user, supplier=supplier, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        token = issue_supplier_cabinet_tokens(supplier, user)["access"]
        result = {
            "index": index,
            "file": name,
            "supplier_id": str(supplier.pk),
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "poll": [],
        }
        results.append(result)
        body, headers = multipart(name, content)
        started = time.perf_counter()
        status, data, seconds = call("POST", f"{BASE}/analyze", token, body=body, headers=headers)
        result["analyze_http"], result["analyze_http_seconds"] = status, round(seconds, 3)
        if status != 202:
            result["error"] = f"analyze {status} {json.dumps(data, ensure_ascii=False)[:300]}"
            print("ERROR", index, result["error"], flush=True)
            continue
        sid = data.get("session_id") or data.get("id")
        result["session_id"] = sid
        last = None
        while True:
            _, state, _ = call("GET", f"{BASE}/{sid}?include_items=false", token)
            snapshot = (state["processing_status"], state["rows_done"], state["rows_total"])
            if snapshot != last:
                result["poll"].append(
                    {
                        "seconds": round(time.perf_counter() - started, 2),
                        "status": snapshot[0],
                        "done": snapshot[1],
                        "total": snapshot[2],
                    }
                )
                last = snapshot
            if state["processing_status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        result["ready_seconds"] = round(time.perf_counter() - started, 2)
        result["initial_status"] = state
        if state["processing_status"] != "ready":
            result["error"] = f"processing {state}"
            print("ERROR", index, result["error"], flush=True)
            continue
        items, after = [], ""
        while True:
            _, page, _ = call("GET", f"{BASE}/{sid}?limit=1000&include_meta=true{after}", token)
            items.extend(page["items"])
            if not page.get("has_more"):
                break
            after = f"&after_row_number={page['next_after_row_number']}"
        (ROOT / f"items-before-{index}.json").write_text(json.dumps(items, ensure_ascii=False))
        # Re-upload of the same bytes must hand back the open session instead of a second processing run.
        body2, headers2 = multipart(name, content)
        s2, d2, _ = call("POST", f"{BASE}/analyze", token, body=body2, headers=headers2)
        result["reupload"] = {"http": s2, "same_session": (d2.get("session_id") or d2.get("id")) == sid}
        result["synthetic_defaults"] = {
            "country_of_origin": "Россия",
            "min_order_qty": "1",
            "delivery_multiple": "1",
            "availability": "in_stock",
        }
        result["defaults_http"] = call(
            "PATCH",
            f"{BASE}/{sid}/items/defaults",
            token,
            body=json.dumps(result["synthetic_defaults"]).encode(),
            headers={"Content-Type": "application/json"},
        )[:2]
        _, before, _ = call("GET", f"{BASE}/{sid}?include_items=false", token)
        result["before_commit_counts"] = before["counts"]
        commit_started = time.perf_counter()
        status, data, _ = call("POST", f"{BASE}/{sid}/commit", token)
        result["commit_http"] = status
        while True:
            _, state, _ = call("GET", f"{BASE}/{sid}?include_items=false", token)
            if state.get("commit_status") in ("done", "completed", "succeeded", "failed") or state.get("commit_result"):
                break
            if time.perf_counter() - commit_started > 900:
                break
            time.sleep(0.5)
        result["commit_seconds"] = round(time.perf_counter() - commit_started, 2)
        result["final_status"] = state
        (ROOT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str))
        cr = state.get("commit_result") or {}
        print(
            "DONE",
            index,
            "rows",
            len(items),
            "ready_s",
            result["ready_seconds"],
            "commit_s",
            result["commit_seconds"],
            "imported",
            cr.get("imported_count"),
            "reupload_same",
            result["reupload"]["same_session"],
            flush=True,
        )

        products = list(SupplierProduct.objects.filter(supplier=supplier))
        prices = dict(
            SupplierProductPrice.objects.filter(supplier_product_id__in=[p.id for p in products]).values_list(
                "supplier_product_id", "price"
            )
        )
        by_name = {p.supplier_name: p for p in products}
        session = SupplierProductImportSession.objects.get(pk=sid)
        db_items = list(session.items.order_by("row_number"))
        mismatches = [
            {"row": i.row_number, "name": i.name}
            for i in db_items
            if i.imported_at is not None and (by_name.get(i.name) is None or prices.get(by_name[i.name].id) != i.price)
        ]
        evidence["suppliers"].append(
            {
                "index": index,
                "products": len(products),
                "statuses": {s: sum(1 for p in products if p.status == s) for s in {p.status for p in products}},
                "imported_items": sum(1 for i in db_items if i.imported_at is not None),
                "left_in_session": sum(1 for i in db_items if i.imported_at is None),
                "price_records": len(prices),
                "price_mismatches": mismatches[:20],
                "price_mismatch_count": len(mismatches),
                "with_composition": sum(1 for p in products if p.pack_items_count),
                "with_country": sum(1 for p in products if p.country_of_origin),
            }
        )
        (ROOT / "db-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=1, default=str))
    (ROOT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
