# ruff: noqa
"""Frozen-task LLM comparison (see docs/wiki/logs/2026-10-15-llm-model-comparison.md). Local paths are hard-coded: run on the author machine."""

import sys, json, time, argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/home/itmedia_pc/projects/auto-snab-import-quality")
import django

django.setup()
from django.conf import settings
from openai import OpenAI
from api.llm.connector import _strict_json_schema
from file_imports import llm_candidates as L

PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-6-luna": (0.10, 0.50)}  # USD per 1M tokens (input, output)
OUT = Path("/home/itmedia_pc/projects/auto-snab-import-quality/var/llm-eval")
ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--effort", default="")
ap.add_argument("--repeat", type=int, default=1)
ap.add_argument("--chunks", type=int, default=0, help="limit number of chunks (pilot)")
ap.add_argument("--cap", type=float, default=4.0)
ap.add_argument(
    "--legacy-params", action="store_true", help="send temperature=0 and max_tokens like the project connector"
)
a = ap.parse_args()
key = (
    getattr(settings, "LLM_PRIMARY_API_KEY", "")
    or getattr(settings, "OPENAI_API_KEY", "")
    or getattr(settings, "AI_NORMALIZATION_API_KEY", "")
)
client = OpenAI(api_key=key, timeout=120, max_retries=0)
tasks = json.load(open(OUT / "tasks.json"))["tasks"]
chunks = [tasks[i : i + 12] for i in range(0, len(tasks), 12)]
if a.chunks:
    chunks = chunks[: a.chunks]
schema = _strict_json_schema(L.BatchCandidateResponse.model_json_schema())
response_format = {
    "type": "json_schema",
    "json_schema": {"name": "BatchCandidateResponse", "strict": True, "schema": schema},
}
spent_file = OUT / "spent.json"


def spent():
    return json.load(open(spent_file))["usd"] if spent_file.exists() else 0.0


def add_spent(x):
    json.dump({"usd": round(spent() + x, 6)}, open(spent_file, "w"))


def call(chunk):
    payload = {
        "rows": [
            {"row_number": t["row_number"], "requested_fields": t["requested"], "cells": t["cells"]} for t in chunk
        ]
    }
    kwargs = dict(
        model=a.model,
        response_format=response_format,
        messages=[
            {"role": "system", "content": L._PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    )
    budget = L._max_tokens(len(chunk))
    if a.legacy_params or a.model == "gpt-4o-mini":
        kwargs.update(temperature=0, max_tokens=budget)
    else:
        kwargs.update(max_completion_tokens=max(8000, budget * 6))
        if a.effort:
            kwargs["reasoning_effort"] = a.effort
    t0 = time.perf_counter()
    rec = {"rows": [t["row_number"] for t in chunk], "file": chunk[0]["file"]}
    try:
        c = client.chat.completions.create(**kwargs)
        u = c.usage
        rec.update(
            content=c.choices[0].message.content,
            finish=c.choices[0].finish_reason,
            prompt_tokens=u.prompt_tokens,
            completion_tokens=u.completion_tokens,
            reasoning_tokens=getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", None),
        )
        pi, po = PRICES[a.model]
        rec["usd"] = (u.prompt_tokens * pi + u.completion_tokens * po) / 1e6
    except Exception as e:
        rec.update(error=f"{type(e).__name__}: {str(e)[:300]}", usd=0.0)
    rec["seconds"] = round(time.perf_counter() - t0, 2)
    return rec


if spent() >= a.cap:
    sys.exit(f"budget cap reached: {spent()}")
with ThreadPoolExecutor(max_workers=4) as ex:
    results = list(ex.map(call, chunks))
add_spent(sum(r["usd"] for r in results))
name = f"run-{a.model}-{a.effort or 'default'}-{a.repeat}{'-legacy' if a.legacy_params else ''}.json"
json.dump(results, open(OUT / name, "w"), ensure_ascii=False, indent=1)
print(
    name,
    "| chunks",
    len(results),
    "| errors",
    sum(1 for r in results if "error" in r),
    "| usd",
    round(sum(r["usd"] for r in results), 4),
    "| total spent",
    round(spent(), 4),
)
for r in results[:3]:
    print(
        {k: r.get(k) for k in ("finish", "prompt_tokens", "completion_tokens", "reasoning_tokens", "seconds", "error")}
    )
