#!/usr/bin/env python3
"""Live check of the supplier product import on a deployed stand.

Uploads real price lists through POST /suppliers/products/import/analyze, polls the session once a
second until it is ready/failed and reports what a user would see: POST latency, time in the queue
(pending), time until the first rows are visible, the longest pause between visible progress updates
(a pause > ~10 s looks like a hang), fill statistics. Nothing is committed, so catalog data stays
untouched (only import sessions and stored source files are created).

The token is read from the AUTOSNAB_SUPPLIER_JWT environment variable (a login access token of a user
with a company is exchanged for a cabinet session) and is never printed or written anywhere:

    AUTOSNAB_SUPPLIER_JWT='<access token>' python3 scripts/live_import_check.py --fresh

Options:
  --base-url, --dir (default ~/Документы/тесты), --only <substring>, --timeout seconds per file
  --fresh        upload a byte-wise modified copy of every file (same rows, new sha256), so the stand
                 does not hand back an old open session
  --parallel N   upload N files at once (queue behaviour with several users)
  --derived      also upload a TXT and a DOCX built from the smallest xlsx (async extraction path);
                 run with the project venv python (needs openpyxl and python-docx)
  --errors       send broken/empty/oversized/foreign files and record the status and message
  --legacy-probe call GET /suppliers/uploads/<random id> once (404 expected) to produce a
                 legacy_uploads telemetry line
Report: /tmp/autosnab_live_report.json (session ids, absolute timestamps for log correlation).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE = "https://avtosnab.testant.online/api/v1"
SPREADSHEETS = {".xlsx", ".xlsm", ".xls", ".csv"}
POLL_INTERVAL = 1.0
HANG_THRESHOLD = 10.0


def _request(method: str, url: str, token: str, *, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw[:300].decode("utf-8", "replace")}


def _multipart(path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode()
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def upload(base: str, token: str, path: Path):
    body, content_type = _multipart(path)
    started = time.monotonic()
    status, data = _request(
        "POST", f"{base}/suppliers/products/import/analyze", token, body=body, headers={"Content-Type": content_type}
    )
    return status, data, time.monotonic() - started


def watch(base: str, token: str, session_id: str, timeout: float) -> dict:
    """Polls like a UI would (include_items + after_row_number) and measures what is visible when."""
    started = time.monotonic()
    after = 0
    visible = 0
    first_processing = None
    first_rows = None
    last_change = started
    max_gap = 0.0
    get_latencies = []
    timeline = []
    last_key = None
    while True:
        poll_started = time.monotonic()
        status, data = _request(
            "GET", f"{base}/suppliers/products/import/{session_id}?include_items=true&after_row_number={after}", token
        )
        latency = time.monotonic() - poll_started
        now = time.monotonic()
        if status != 200 or not isinstance(data, dict):
            return {"poll_status": status, "poll_body": data, "total_s": round(now - started, 1)}
        get_latencies.append(latency)
        items = data.get("items") or []
        if items:
            after = max(i["row_number"] for i in items)
            visible += len(items)
            if first_rows is None:
                first_rows = now - started
        state = data["processing_status"]
        if state != "pending" and first_processing is None:
            first_processing = now - started
        key = (state, data["rows_done"], visible)
        if key != last_key:
            if last_key is not None and state == "processing":
                max_gap = max(max_gap, now - last_change)
            last_change = now
            last_key = key
            timeline.append((round(now - started, 1), state, data["rows_done"], data["rows_total"], visible))
        if state in ("ready", "failed"):
            return {
                "final": state,
                "error_message": data.get("error_message"),
                "rows_total": data.get("rows_total"),
                "total_s": round(now - started, 1),
                "pending_s": round(first_processing, 1) if first_processing is not None else None,
                "first_rows_s": round(first_rows, 1) if first_rows is not None else None,
                "max_pause_s": round(max_gap, 1),
                "get_avg_s": round(sum(get_latencies) / len(get_latencies), 2),
                "get_max_s": round(max(get_latencies), 2),
                "polls": len(get_latencies),
                "timeline": timeline[:40],
                "counts": data.get("counts"),
            }
        if now - started > timeout:
            return {"final": "timeout", "total_s": round(now - started, 1), "timeline": timeline[:40]}
        time.sleep(POLL_INTERVAL)


def fill_stats(base: str, token: str, session_id: str) -> dict:
    status, data = _request("GET", f"{base}/suppliers/products/import/{session_id}?include_items=true", token)
    if status != 200:
        return {"items_status": status}
    items = data.get("items") or []
    total = len(items) or 1

    def count(field):
        return sum(1 for i in items if i.get(field))

    return {
        "items": len(items),
        "pct_unit": round(100 * count("unit_id") / total),
        "pct_country": round(100 * count("country_of_origin") / total),
        "pct_net": round(100 * count("net_value") / total),
        "with_price": sum(1 for i in items if i.get("price") is not None),
    }


def fresh_copy(path: Path, out_dir: Path) -> Path:
    """Copy with changed bytes but the same rows: zip comment for xlsx, trailing bytes for the rest."""
    target = out_dir / path.name
    shutil.copyfile(path, target)
    marker = uuid.uuid4().hex.encode()
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        with zipfile.ZipFile(target, "a") as archive:
            archive.comment = marker
    elif path.suffix.lower() in {".csv", ".txt"}:
        with open(target, "ab") as handle:
            handle.write(b"\n" * (1 + marker[0] % 16) + b" " * (1 + marker[1] % 64))  # blank tail, no extra rows
    else:  # .xls (OLE2) tolerates trailing padding; the random marker keeps every copy unique
        with open(target, "ab") as handle:
            handle.write(b"\x00" * 512 + marker)
    return target


def _price_list_rows(sheet, limit=120):
    """Clean table [Артикул, Номенклатура, Цена] from a real price list: the header row is the first one with
    a "Номенклатура"/"Наименование" cell, the price is the first numeric cell right of the name."""
    name_col = None
    rows = []
    for row in sheet.iter_rows(values_only=True):
        cells = list(row)
        if name_col is None:
            for index, cell in enumerate(cells):
                if isinstance(cell, str) and cell.strip().lower().startswith(("номенклатура", "наименование")):
                    name_col = index
            continue
        name = cells[name_col] if name_col < len(cells) else None
        if not isinstance(name, str) or not name.strip():
            continue
        price = next((c for c in cells[name_col + 1 :] if isinstance(c, (int, float)) and c > 0), None)
        if price is None:
            continue
        sku = next((str(c).strip() for c in cells[:name_col] if c not in (None, "")), "")
        rows.append([sku, " ".join(name.split()), f"{price:.2f}"])
        if len(rows) >= limit:
            break
    return [["Артикул", "Номенклатура", "Цена"], *rows] if rows else []


def build_derived(files: list[Path], out_dir: Path) -> list[Path]:
    """TXT (always) and DOCX (if python-docx exists) built from the first 120 rows of the smallest xlsx."""
    try:
        import openpyxl  # type: ignore
    except ImportError:
        print("openpyxl is missing, skipping --derived (run the script with the project venv python)")
        return []
    xlsx = sorted((f for f in files if f.suffix.lower() == ".xlsx"), key=lambda f: f.stat().st_size)
    for source in xlsx:
        rows = _price_list_rows(openpyxl.load_workbook(source, read_only=True, data_only=True).worksheets[0])
        if rows:
            break
    else:
        print("No price rows found in the xlsx files, skipping --derived")
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    txt = out_dir / "derived_price.txt"
    txt.write_text("\n".join("\t".join(r) for r in rows), encoding="utf-8")
    result = [txt]
    try:
        import docx  # type: ignore

        document = docx.Document()
        table = document.add_table(rows=len(rows), cols=3)
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                table.cell(i, j).text = cell
        path = out_dir / "derived_price.docx"
        document.save(path)
        result.append(path)
    except ImportError:
        print("python-docx is missing, DOCX skipped")
    return result


def run_file(base: str, token: str, path: Path, timeout: float, original_name: str) -> dict:
    entry = {"file": original_name, "size_kb": path.stat().st_size // 1024, "started_utc": _now_iso()}
    status, data, post_s = upload(base, token, path)
    entry.update(post_status=status, post_s=round(post_s, 2))
    if status != 202 or not isinstance(data, dict) or "id" not in data:
        entry["error"] = data
        return entry
    entry["session"] = data["id"]
    entry["post_state"] = data.get("status")
    entry.update(watch(base, token, data["id"], timeout))
    entry["finished_utc"] = _now_iso()
    if entry.get("final") == "ready":
        entry["fill"] = fill_stats(base, token, data["id"])
        status2, again, repeat_s = upload(base, token, path)
        entry["repeat_same_session"] = isinstance(again, dict) and again.get("id") == data["id"]
        entry["repeat_s"] = round(repeat_s, 2)
    return entry


def print_entry(entry: dict) -> None:
    print(f"\n== {entry['file']} ({entry['size_kb']} KB)  {entry['started_utc']}-{entry.get('finished_utc', '?')} UTC")
    print(f"   POST {entry['post_status']} in {entry['post_s']}s  session={entry.get('session')}")
    if "error" in entry:
        print("   error:", entry["error"])
        return
    print(
        f"   {entry.get('final')}: total {entry.get('total_s')}s rows={entry.get('rows_total')} "
        f"pending {entry.get('pending_s')}s first rows {entry.get('first_rows_s')}s "
        f"max pause {entry.get('max_pause_s')}s GET avg/max {entry.get('get_avg_s')}/{entry.get('get_max_s')}s"
    )
    if entry.get("error_message"):
        print("   error_message:", entry["error_message"])
    for step in entry.get("timeline", [])[:14]:
        print("     t=%ss %s %s/%s visible=%s" % tuple(step))
    if entry.get("fill"):
        print("   fill:", entry["fill"], "repeat same session:", entry.get("repeat_same_session"))


def error_scenarios(base: str, token: str, tmp: Path) -> list[dict]:
    cases = []

    def case(name, filename, content):
        path = tmp / filename
        path.write_bytes(content)
        status, data, _ = upload(base, token, path)
        cases.append({"case": name, "status": status, "body": data})
        print(f"   {name}: {status} {json.dumps(data, ensure_ascii=False)[:200]}")

    print("\n== error scenarios")
    case("random bytes as .xlsx", "broken.xlsx", os.urandom(2048))
    case("empty file", "empty.xlsx", b"")
    case("unsupported extension", "malware.exe", b"MZ" + b"\x00" * 64)
    try:
        import openpyxl  # type: ignore

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Название", "Цена"])
        for index in range(12001):
            sheet.append([f"Тестовый товар {index}", 100 + index % 50])
        big = tmp / "too_many_rows.xlsx"
        workbook.save(big)
        status, data, _ = upload(base, token, big)
        cases.append({"case": "12001 rows", "status": status, "body": data})
        print(f"   12001 rows: {status} {json.dumps(data, ensure_ascii=False)[:200]}")
    except ImportError:
        print("   openpyxl missing, 12001-rows case skipped")
    status, data = _request("POST", f"{base}/suppliers/products/import/analyze", "")
    cases.append({"case": "no token", "status": status, "body": data})
    print(f"   no token: {status}")
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("AUTOSNAB_API_BASE", DEFAULT_BASE))
    parser.add_argument("--dir", default=str(Path.home() / "Документы" / "тесты"))
    parser.add_argument("--only", default="", help="substring of the file name")
    parser.add_argument("--derived", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--errors", action="store_true")
    parser.add_argument("--legacy-probe", action="store_true")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--report", default="/tmp/autosnab_live_report.json", help="path of the JSON report")
    args = parser.parse_args()

    token = os.environ.get("AUTOSNAB_SUPPLIER_JWT", "").strip()
    if not token:
        print("Set AUTOSNAB_SUPPLIER_JWT", file=sys.stderr)
        return 2
    base = args.base_url.rstrip("/")
    directory = Path(args.dir).expanduser()
    if not directory.is_dir():  # the folder on this machine is named "тесты " with a trailing space
        candidates = [d for d in directory.parent.glob(directory.name + "*") if d.is_dir()]
        directory = candidates[0] if candidates else directory
    files = sorted(f for f in directory.iterdir() if f.suffix.lower() in SPREADSHEETS)
    if args.only:
        files = [f for f in files if args.only.lower() in f.name.lower()]
    tmp = Path(tempfile.mkdtemp(prefix="autosnab_live_"))
    if args.derived:
        files += build_derived(files or sorted(directory.glob("*.xlsx")), tmp / "derived")
    if not files:
        print(f"No files in {directory}", file=sys.stderr)
        return 2

    # analyze accepts only a supplier cabinet JWT. A login (core user) token is exchanged for one;
    # a token that already is a cabinet token (accepted by cabinet/me) is used as is.
    status, _ = _request("GET", f"{base}/suppliers/cabinet/me", token)
    if status != 200:
        status, session = _request("POST", f"{base}/suppliers/cabinet/session-from-login", token)
        if status != 200 or not isinstance(session, dict) or "access" not in session:
            print(f"Cannot get a supplier cabinet session: {status} {session}", file=sys.stderr)
            return 2
        token = session["access"]
        print(f"Cabinet session for supplier {session.get('supplier_name')}")

    jobs = []
    for path in files:
        send = fresh_copy(path, tmp) if args.fresh else path
        jobs.append((send, path.name))
    print(f"{len(jobs)} files, fresh={args.fresh}, parallel={args.parallel}, start {_now_iso()} UTC")

    run_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futures = [pool.submit(run_file, base, token, send, args.timeout, name) for send, name in jobs]
        report = []
        for future in futures:
            entry = future.result()
            report.append(entry)
            print_entry(entry)
    wall = time.monotonic() - run_started

    extras = {}
    if args.errors:
        extras["errors"] = error_scenarios(base, token, tmp)
    if args.legacy_probe:
        status, data = _request("GET", f"{base}/suppliers/uploads/{uuid.uuid4()}", token)
        extras["legacy_probe"] = {"status": status, "at_utc": _now_iso()}
        print(f"\n== legacy probe: GET /suppliers/uploads/<id> -> {status} at {_now_iso()} UTC")

    summary = {
        "wall_s": round(wall, 1),
        "parallel": args.parallel,
        "files_ready": sum(1 for e in report if e.get("final") == "ready"),
        "files": len(report),
        "worst_pause_s": max((e.get("max_pause_s") or 0) for e in report),
        "hang_threshold_s": HANG_THRESHOLD,
    }
    print(f"\nSUMMARY {json.dumps(summary, ensure_ascii=False)}")
    out = Path(args.report)
    out.write_text(json.dumps({"summary": summary, "files": report, **extras}, ensure_ascii=False, indent=2))
    print(f"Report: {out}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if summary["files_ready"] == summary["files"] else 1


if __name__ == "__main__":
    sys.exit(main())
