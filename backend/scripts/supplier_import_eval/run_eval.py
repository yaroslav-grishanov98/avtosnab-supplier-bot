"""Drive the supplier product import pipeline like the web UI does, for local price files.

usage: run_eval.py <out_dir> <mode: llm|nollm> <file> [<file> ...]

Run with DJANGO_SETTINGS_MODULE=eval_settings and PYTHONPATH pointing at this directory (see README.md).
Mode "llm" needs the key in the EVAL_OPENAI_KEY environment variable.
"""

import json
import sys
import time
from pathlib import Path

import django

django.setup()

from django.conf import settings  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from api.llm.connector import LLMConnector  # noqa: E402
from api.v1.suppliers.services import issue_supplier_cabinet_tokens  # noqa: E402
from suppliers.models import Supplier, SupplierProduct  # noqa: E402

if len(sys.argv) < 4 or sys.argv[2] not in ("llm", "nollm"):
    sys.exit("usage: run_eval.py <out_dir> <llm|nollm> <file> [<file> ...]")
out_dir = Path(sys.argv[1])
mode = sys.argv[2]
files = sys.argv[3:]
out_dir.mkdir(parents=True, exist_ok=True)
settings.CANONICAL_IMPORT_LLM_ENABLED = mode == "llm"
if mode == "llm" and not settings.LLM_PRIMARY_API_KEY:
    sys.exit("mode llm needs the EVAL_OPENAI_KEY environment variable")

LLM_LOG = []
_orig_structured = LLMConnector.structured


def _logged_structured(self, *, scenario, system_prompt, payload, response_model, max_tokens=1200):
    started = time.perf_counter()
    entry = {"scenario": scenario, "payload": payload}
    try:
        result = _orig_structured(
            self,
            scenario=scenario,
            system_prompt=system_prompt,
            payload=payload,
            response_model=response_model,
            max_tokens=max_tokens,
        )
        entry["response"] = result.model_dump()
        return result
    except Exception as error:  # noqa: BLE001
        entry["error"] = f"{error.__class__.__name__}: {error}"
        raise
    finally:
        entry["seconds"] = round(time.perf_counter() - started, 2)
        LLM_LOG.append(entry)


LLMConnector.structured = _logged_structured


def fetch_all_items(client, session_id):
    items, after = [], None
    while True:
        params = {"limit": 1000, "include_meta": "true"}
        if after is not None:
            params["after_row_number"] = after
        response = client.get(f"/api/v1/suppliers/products/import/{session_id}", params)
        if response.status_code != 200:
            raise RuntimeError(f"items fetch failed: HTTP {response.status_code} {response.content[:500]!r}")
        body = response.json()
        items.extend(body["items"])
        if not body["has_more"]:
            return body, items
        after = body["next_after_row_number"]


failed = []
for path in files:
    path = Path(path)
    tag = f"{path.stem[:40]}__{mode}"
    LLM_LOG.clear()
    supplier = Supplier.objects.create(legal_name=f"Eval {tag}", display_name=f"Eval {tag}")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_supplier_cabinet_tokens(supplier)['access']}")
    result = {"file": path.name, "mode": mode}

    started = time.perf_counter()
    response = client.post(
        "/api/v1/suppliers/products/import/analyze",
        {"file": SimpleUploadedFile(path.name, path.read_bytes())},
        format="multipart",
    )
    result["analyze_status"] = response.status_code
    result["analyze_seconds"] = round(time.perf_counter() - started, 1)
    if response.status_code != 202:
        result["analyze_body"] = response.content.decode()[:1000]
        (out_dir / f"{tag}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(tag, "analyze_status", response.status_code, flush=True)
        failed.append(f"{tag}: analyze HTTP {response.status_code}")
        continue
    session_id = response.json()["session_id"]

    for _ in range(600):
        status_body = client.get(f"/api/v1/suppliers/products/import/{session_id}", {"include_items": "false"}).json()
        if status_body["processing_status"] in ("ready", "failed"):
            break
        time.sleep(1)
    result["session"] = {k: v for k, v in status_body.items() if k != "items"}
    if status_body["processing_status"] != "ready":
        failed.append(f"{tag}: processing_status={status_body['processing_status']}")
    else:
        body, items = fetch_all_items(client, session_id)
        result["counts"] = body["counts"]
        result["items"] = items

        commit = client.post(f"/api/v1/suppliers/products/import/{session_id}/commit")
        result["commit_http"] = commit.status_code
        after = client.get(f"/api/v1/suppliers/products/import/{session_id}", {"include_items": "false"}).json()
        result["commit_status"] = after.get("commit_status")
        result["commit_error"] = after.get("commit_error")
        result["commit_result"] = after.get("commit_result")
        result["session_status_after_commit"] = after.get("status")
        if commit.status_code != 202 or after.get("commit_status") == "failed" or after.get("commit_error"):
            failed.append(
                f"{tag}: commit_http={commit.status_code} commit_status={after.get('commit_status')} "
                f"commit_error={after.get('commit_error')}"
            )
        products = SupplierProduct.objects.filter(supplier=supplier)
        result["products_created"] = products.count()
        result["products_by_status"] = {
            s: products.filter(status=s).count() for s in products.values_list("status", flat=True).distinct()
        }
    result["llm_calls"] = LLM_LOG[:]
    (out_dir / f"{tag}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    print(
        tag,
        "analyze_status",
        result["analyze_status"],
        "status",
        status_body["processing_status"],
        "rows",
        status_body.get("rows_total"),
        "counts",
        result.get("counts"),
        "commit_http",
        result.get("commit_http"),
        "products_created",
        result.get("products_created"),
        "llm_calls",
        len(LLM_LOG),
        "analyze_s",
        result["analyze_seconds"],
        flush=True,
    )

if failed:
    sys.exit("FAILED: " + "; ".join(failed))
