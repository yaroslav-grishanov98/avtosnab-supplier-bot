"""Simulate the user filling mandatory fields (absent from the files) and pressing "Apply".

usage: run_commit.py <out_json> <file>   (LLM off, deterministic)

Run with DJANGO_SETTINGS_MODULE=eval_settings and PYTHONPATH pointing at this directory (see README.md).
"""

import collections
import json
import sys
import time
from pathlib import Path

import django

django.setup()

from django.conf import settings  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from api.v1.suppliers.services import issue_supplier_cabinet_tokens  # noqa: E402
from core.models import Unit  # noqa: E402
from suppliers.models import Supplier, SupplierProduct  # noqa: E402

settings.CANONICAL_IMPORT_LLM_ENABLED = False
out, path = Path(sys.argv[1]), Path(sys.argv[2])
supplier = Supplier.objects.create(legal_name=f"Commit {path.stem[:30]}", display_name=f"Commit {path.stem[:30]}")
client = APIClient()
client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_supplier_cabinet_tokens(supplier)['access']}")
base = "/api/v1/suppliers/products/import"
sht = Unit.objects.get(code="шт")
res = {"file": path.name}

r = client.post(f"{base}/analyze", {"file": SimpleUploadedFile(path.name, path.read_bytes())}, format="multipart")
res["analyze_status"] = r.status_code
if r.status_code != 202:
    res["analyze_body"] = r.content.decode()[:1000]
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    sys.exit(f"analyze failed: HTTP {r.status_code}")
sid = r.json()["session_id"]
for _ in range(600):
    st = client.get(f"{base}/{sid}", {"include_items": "false"}).json()
    if st["processing_status"] in ("ready", "failed"):
        break
    time.sleep(1)
res["processing_status"] = st["processing_status"]
if st["processing_status"] != "ready":
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    sys.exit(f"session not ready: {st['processing_status']}")

items, after = [], None
while True:
    params = {"limit": 1000}
    if after is not None:
        params["after_row_number"] = after
    body = client.get(f"{base}/{sid}", params).json()
    items += body["items"]
    if not body["has_more"]:
        break
    after = body["next_after_row_number"]
res["rows"] = len(items)

# 1. user resolves review rows -> new, then fills mandatory fields that the file does not contain
t0 = time.time()
action_failures = []
for it in items:
    if it["action"] == "review":
        put = client.put(f"{base}/{sid}/items/{it['id']}/action", {"action": "new"}, format="json")
        if put.status_code != 200:
            action_failures.append((it["row_number"], put.status_code, put.content.decode()[:200]))
res["action_failures"] = action_failures

# 1a. bulk defaults endpoint (fills only empty values)
defaults = client.patch(
    f"{base}/{sid}/items/defaults",
    {"country_of_origin": "Россия", "min_order_qty": "1", "delivery_multiple": "1", "availability": "in_stock"},
    format="json",
)
res["defaults_http"] = defaults.status_code
res["defaults_updated_count"] = defaults.json().get("updated_count") if defaults.status_code == 200 else None
if defaults.status_code != 200:
    res["defaults_body"] = defaults.content.decode()[:500]

# 1b. per-row patches only for what the defaults endpoint does not cover
patched = 0
for it in items:
    patch = {}
    if not it["unit_id"]:
        patch["unit_id"] = str(sht.id)
    if not it["net_value"]:
        patch["net_value"] = "1"
    if not it["net_unit_id"]:
        patch["net_unit_id"] = str(sht.id)
    if patch:
        resp = client.patch(f"{base}/{sid}/items/{it['id']}", patch, format="json")
        patched += 1
        if resp.status_code != 200:
            res.setdefault("patch_errors", []).append((it["row_number"], resp.status_code, resp.content.decode()[:200]))
res["user_edit_seconds"] = round(time.time() - t0, 1)
res["items_patched"] = patched
st = client.get(f"{base}/{sid}", {"include_items": "false"}).json()
res["counts_before_commit"] = st["counts"]

# 2. Apply
c = client.post(f"{base}/{sid}/commit")
res["commit_http"] = c.status_code
st = client.get(f"{base}/{sid}", {"include_items": "false"}).json()
cr = st.get("commit_result") or {}
res["commit_status"] = st.get("commit_status")
res["commit_error"] = st.get("commit_error")
res["result"] = {
    k: v for k, v in cr.items() if k not in ("errors", "draft_supplier_product_ids", "not_ready_row_numbers")
}
errs = collections.Counter()
for e in cr.get("errors", []):
    for m in e["messages"]:
        errs[m[:120]] += 1
res["error_messages"] = errs.most_common(10)
res["error_examples"] = cr.get("errors", [])[:8]
res["not_ready_rows_sample"] = cr.get("not_ready_row_numbers", [])[:20]
products = SupplierProduct.objects.filter(supplier=supplier)
res["products_created"] = products.count()
byrow = {it["row_number"]: it for it in items}
res["not_imported_row_names"] = [
    byrow[n]["raw_name"][:80] for n in cr.get("not_ready_row_numbers", [])[:25] if n in byrow
]
out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
failures = []
if res["commit_http"] != 202:
    failures.append(f"commit_http={res['commit_http']}")
if res["commit_status"] == "failed":
    failures.append("commit_status=failed")
if res["commit_error"]:
    failures.append(f"commit_error={res['commit_error']}")
if res["defaults_http"] != 200:
    failures.append(f"defaults_http={res['defaults_http']}")
if res.get("patch_errors"):
    failures.append(f"patch_errors={len(res['patch_errors'])}")
if action_failures:
    failures.append(f"failed_action_transitions={len(action_failures)}")
print(
    path.name,
    "rows",
    res["rows"],
    "defaults_updated",
    res["defaults_updated_count"],
    "patched",
    patched,
    "imported",
    res["result"].get("imported_count"),
    "not_ready",
    res["result"].get("skipped_incomplete_count"),
    "errors",
    dict(errs.most_common(3)),
)
if failures:
    sys.exit("FAILED: " + "; ".join(failures))
