"""Deterministic fixtures for analytics tests. All timestamps are fixed historical UTC instants."""

from __future__ import annotations


def ev(event: str, user: str, ts: str, **props):  # type: ignore[no-untyped-def]
    d = {"event": event, "distinct_id": user, "timestamp": ts if ts.endswith("Z") else ts + "Z"}
    sets = props.pop("_set", None)
    if sets:
        props["$set"] = sets
    if props:
        d["properties"] = props
    return d


async def seed(ingest, key, events):  # type: ignore[no-untyped-def]
    for i in range(0, len(events), 400):
        r = await ingest(key, events[i:i + 400])
        assert r.status_code == 200, r.text
        assert r.json()["rejected"] == [], r.json()


async def analytics(client, path, project_id, **body):  # type: ignore[no-untyped-def]
    r = await client.post(f"/api/v1/analytics/{path}", json={"project_id": project_id, **body})
    assert r.status_code == 200, r.text
    return r.json()["result"]
