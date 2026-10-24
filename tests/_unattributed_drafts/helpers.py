from __future__ import annotations

from typing import Any

RANGE = {"from": "2026-08-01", "to": "2026-08-31"}


def ev(distinct_id: str, event: str, ts: str, **props: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"event": event, "distinct_id": distinct_id, "timestamp": ts}
    if props:
        out["properties"] = props
    return out


async def seed(ingest, key: str, events: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    for i in range(0, len(events), 400):
        r = await ingest(key, events[i : i + 400])
        assert r.status_code == 200, r.text
        assert not r.json()["rejected"], r.json()


async def query(ctx: dict, kind: str, q: dict, **extra: Any) -> dict[str, Any]:
    r = await ctx["client"].post(
        f"/api/v1/analytics/{kind}", json={"project_id": ctx["project_id"], "query": q, **extra}
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]
