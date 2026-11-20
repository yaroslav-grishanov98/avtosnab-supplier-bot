"""Aggregate quality diagnostics; never log source values or product names."""

import logging
from collections import Counter

logger = logging.getLogger("autosnab.import.canonical")


def log_canonical_rows(rows):
    snapshots = [row["_parsed"] for row in rows if row.get("_parsed")]
    if not snapshots:
        return
    sources = Counter(f["source"] for row in snapshots for f in row["fields"].values())
    statuses = Counter(f["status"] for row in snapshots for f in row["fields"].values())
    logger.info(
        "canonical.parse rows_total=%s rows_review_required=%s field_sources=%s field_statuses=%s parser_version=%s",
        len(snapshots),
        sum(bool(row["parse_issues"]) for row in snapshots),
        dict(sources),
        dict(statuses),
        snapshots[0]["parser_version"],
    )
