# ruff: noqa
"""Frozen-task LLM comparison (see docs/wiki/logs/2026-10-15-llm-model-comparison.md). Local paths are hard-coded: run on the author machine."""

import sys, json, glob, math, collections, copy
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, "/home/itmedia_pc/projects/auto-snab-import-quality")
import django

django.setup()
from django.conf import settings

settings.CANONICAL_IMPORT_LLM_ENABLED = False
from django.core.files.uploadedfile import SimpleUploadedFile
from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file, _field_for_header_key
from file_imports.canonical import parse_product_row
from file_imports.types import RawImportRow
from file_imports import llm_candidates as L

OUT = Path("/home/itmedia_pc/projects/auto-snab-import-quality/var/llm-eval")
D = Path("/mnt/c/Users/itmedia pc/Downloads")
tasks = json.load(open(OUT / "tasks.json"))["tasks"]
by_key = {(t["file"], str(t["row_number"])): t for t in tasks}
parsed0 = {}
for name in sorted({t["file"] for t in tasks}):
    for row in parse_supplier_product_import_file(
        SimpleUploadedFile(name, (D / name).read_bytes()), canonical_enabled=True
    ):
        k = (name, str(row["row_number"]))
        if k in by_key:
            raw = row["_parsed"]["raw"]
            parsed0[k] = parse_product_row(
                RawImportRow(raw["row_number"], raw["cells"], raw["source"]), _field_for_header_key
            )
LABEL = {
    "unit_label": "true_unit",
    "country_of_origin": "true_country",
    "pack_items_count": "true_pack_count",
    "pack_item_value": "true_pack_item_value",
    "pack_item_unit_label": "true_pack_item_unit",
}


def dec(v):
    try:
        return Decimal(str(v).replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return None


def same(a, b):
    a, b = (str(a) or "").strip().lower(), (str(b) or "").strip().lower()
    return a == b or (dec(a) is not None and dec(b) is not None and dec(a) == dec(b))


def wilson_low(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d


def evaluate(path):
    runs = json.load(open(path))
    res = collections.defaultdict(collections.Counter)
    errs = 0
    usd = 0
    secs = []
    accepted_set = {}
    tok = collections.Counter()
    for chunk in runs:
        usd += chunk.get("usd", 0)
        secs.append(chunk.get("seconds", 0))
        tok["in"] += chunk.get("prompt_tokens") or 0
        tok["out"] += chunk.get("completion_tokens") or 0
        tok["reason"] += chunk.get("reasoning_tokens") or 0
        if "error" in chunk or not chunk.get("content"):
            errs += 1
            continue
        try:
            resp = L.BatchCandidateResponse.model_validate_json(chunk["content"])
        except Exception:
            errs += 1
            continue
        for rc in resp.rows:
            k = (chunk["file"], str(rc.row_number))
            t = by_key.get(k)
            if t is None:
                continue
            parsed = copy.deepcopy(parsed0[k])
            for s in rc.candidates:
                if not L._apply_suggestion(parsed, s, t["requested"], t["cells"]):
                    res[t["split"]]["rejected_by_validators"] += 1
                    continue
                f = s.field
                if f not in LABEL:
                    res[t["split"]]["accepted_unscored"] += 1
                    continue
                value = parsed.fields[f].value
                truth = t["label"][LABEL[f]]
                accepted_set[(k, f)] = str(value)
                if truth in ("unknown", ""):
                    res[t["split"]]["accepted_unsupported"] += 1
                elif same(value, truth):
                    res[t["split"]]["accepted_correct"] += 1
                else:
                    res[t["split"]]["accepted_wrong"] += 1
    return res, errs, usd, secs, tok, accepted_set


names = sorted(glob.glob(str(OUT / "run-*.json")))
summary = {}
for p in names:
    res, errs, usd, secs, tok, acc = evaluate(p)
    summary[Path(p).stem] = acc
    tot = collections.Counter()
    for s in res.values():
        tot.update(s)
    n_acc = tot["accepted_correct"] + tot["accepted_wrong"] + tot["accepted_unsupported"]
    print(
        f"{Path(p).stem:34} err={errs} usd={usd:.4f} avg_s={sum(secs) / max(1, len(secs)):.1f} tok in/out/reason={tok['in']}/{tok['out']}/{tok['reason']} | accepted={n_acc} correct={tot['accepted_correct']} wrong={tot['accepted_wrong']} unsupported={tot['accepted_unsupported']} validator_rejected={tot['rejected_by_validators']}"
    )
    for sp in ("dev", "holdout"):
        s = res[sp]
        n = s["accepted_correct"] + s["accepted_wrong"] + s["accepted_unsupported"]
        print(
            f"    {sp:8} accepted={n} correct={s['accepted_correct']} wrong={s['accepted_wrong']} unsupported={s['accepted_unsupported']} wilson_low(correct/accepted)={wilson_low(s['accepted_correct'], n):.2f}"
        )
print("\nrepeat agreement (same accepted (row,field,value) in run 1 vs run 2):")
for prefix in sorted({n.rsplit("-", 1)[0] for n in summary}):
    a, b = summary.get(prefix + "-1"), summary.get(prefix + "-2")
    if a is not None and b is not None:
        union = set(a) | set(b)
        agree = sum(1 for k in union if a.get(k) == b.get(k))
        print(f"  {prefix}: {agree}/{len(union)} identical")
