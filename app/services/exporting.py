"""CSV / JSON serialisation shared by streamed downloads and asynchronous export jobs."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from typing import Any

EVENT_COLUMNS = ["id", "timestamp", "event", "distinct_id", "person_id", "anonymous_id", "path", "hostname", "browser", "os",
                 "device_type", "country", "properties"]
USER_COLUMNS = ["id", "email", "is_identified", "first_seen", "last_seen", "properties"]


def _safe_cell(v: Any) -> Any:
    """Neutralise spreadsheet formula injection (cells starting with = + - @ are prefixed with a quote)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def flatten_event(e: dict[str, Any]) -> dict[str, Any]:
    d = e.get("device", {})
    return {**{k: e.get(k) for k in ("id", "timestamp", "event", "distinct_id", "person_id", "anonymous_id", "path", "hostname", "country")},
            "browser": d.get("browser"), "os": d.get("os"), "device_type": d.get("device_type"),
            "properties": json.dumps(e.get("properties", {}), separators=(",", ":"))}


def flatten_user(u: dict[str, Any]) -> dict[str, Any]:
    return {**{k: u.get(k) for k in ("id", "email", "is_identified", "first_seen", "last_seen")},
            "properties": json.dumps(u.get("properties", {}), separators=(",", ":"))}


async def to_csv(rows: AsyncIterator[dict[str, Any]], columns: list[str]) -> AsyncIterator[bytes]:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    yield b"\xef\xbb\xbf" + buf.getvalue().encode()
    buf.seek(0), buf.truncate()
    n = 0
    async for r in rows:
        w.writerow([_safe_cell(r.get(c)) for c in columns])
        n += 1
        if n % 500 == 0:
            yield buf.getvalue().encode()
            buf.seek(0), buf.truncate()
    if buf.tell():
        yield buf.getvalue().encode()


async def to_json(rows: AsyncIterator[dict[str, Any]]) -> AsyncIterator[bytes]:
    yield b"["
    first = True
    async for r in rows:
        yield (b"" if first else b",\n") + json.dumps(r, default=str).encode()
        first = False
    yield b"]\n"


def table_rows(kind: str, result: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Flatten an analytics result into a table for CSV export."""
    if kind == "trend":
        cols = ["series", "bucket", "value"]
        rows = [{"series": s["label"], "bucket": lab, "value": v} for s in result["series"] for lab, v in zip(result["labels"], s["data"], strict=False)]
        return cols, rows
    if kind == "funnel":
        cols = ["step", "event", "count", "conversion_rate", "dropoff_count", "overall_conversion_rate", "avg_time_to_convert_seconds"]
        return cols, [{"step": s["order"], **{k: s[k] for k in cols[1:] if k in s}} for s in result["steps"]]
    if kind == "retention":
        offs = result["offsets"]
        cols = ["cohort", "size"] + [f"offset_{o}" for o in offs]
        rows = [{"cohort": c["period"], "size": c["size"], **{f"offset_{x['offset']}": x["rate"] for x in c["cells"]}} for c in result["cohorts"]]
        return cols, rows
    if kind == "sessions":
        cols = ["bucket", "sessions", "avg_duration_seconds"]
        return cols, [{"bucket": a, "sessions": b, "avg_duration_seconds": c} for a, b, c in zip(result["labels"], result["sessions_series"], result["avg_duration_series"], strict=False)]
    if kind == "paths":
        cols = ["step", "source", "target", "count"]
        return cols, list(result["edges"])
    return ["json"], [{"json": json.dumps(result)}]
