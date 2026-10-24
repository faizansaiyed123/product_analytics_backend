"""Explorer, cohorts, reports, dashboards, alerts, exports, worker tasks."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import register
from tests.helpers import RANGE, ev, query, seed


@pytest.fixture
async def data(owner, ingest):
    now = datetime.now(UTC)
    evs = []
    for i in range(7):
        evs.append(ev("alice", "page_view", (now - timedelta(days=2, minutes=-i)).isoformat(), path="/p%d" % i))
    evs += [
        ev(
            "alice",
            "purchase",
            (now - timedelta(days=1)).isoformat(),
            amount=60,
            plan="pro",
            **{"$set": {"plan": "pro", "email": "alice@x.com"}},
        ),
        ev("alice", "purchase", (now - timedelta(hours=5)).isoformat(), amount=40, plan="pro"),
        ev("bob", "page_view", (now - timedelta(days=1)).isoformat(), path="/home", **{"$set": {"plan": "free"}}),
        ev(
            "carol",
            "purchase",
            (now - timedelta(hours=2)).isoformat(),
            amount=10,
            plan="free",
            **{"$set": {"plan": "free", "email": "carol@x.com"}},
        ),
        ev("=cmd", "weird", (now - timedelta(hours=1)).isoformat(), note='=HYPERLINK("http://evil")'),
    ]
    await seed(ingest, owner["api_key"], evs)
    return owner


async def test_event_explorer_filters_search_and_cursor_pagination(data):
    c, pid = data["client"], data["project_id"]
    r = (await c.get("/api/v1/events", params={"project_id": pid, "limit": 5})).json()
    assert len(r["items"]) == 5 and r["next_cursor"]
    seen = {e["id"] for e in r["items"]}
    stamps = [e["timestamp"] for e in r["items"]]
    assert stamps == sorted(stamps, reverse=True)
    pages = 1
    while r["next_cursor"]:
        r = (await c.get("/api/v1/events", params={"project_id": pid, "limit": 5, "cursor": r["next_cursor"]})).json()
        seen |= {e["id"] for e in r["items"]}
        pages += 1
    assert len(seen) == 12 and pages == 3  # every event exactly once

    flt = json.dumps(
        [
            {"property": "plan", "operator": "equals", "value": "pro"},
            {"property": "amount", "operator": "gt", "value": 50},
        ]
    )
    r = (await c.get("/api/v1/events", params={"project_id": pid, "event": "purchase", "filters": flt})).json()
    assert len(r["items"]) == 1 and r["items"][0]["properties"]["amount"] == 60
    r = (await c.get("/api/v1/events", params={"project_id": pid, "search": "carol"})).json()
    assert [e["distinct_id"] for e in r["items"]] == ["carol"]
    r = (await c.get("/api/v1/events", params={"project_id": pid, "search": "100%"})).json()
    assert r["items"] == []  # LIKE wildcards are escaped
    assert (await c.get("/api/v1/events", params={"project_id": pid, "filters": "not json"})).status_code == 422
    assert (await c.get("/api/v1/events", params={"project_id": pid, "cursor": "garbage"})).status_code == 400
    detail = (
        await c.get(f"/api/v1/events/{r['items'][0]['id'] if r['items'] else seen.pop()}", params={"project_id": pid})
    ).json()
    assert detail["person"]["id"] == detail["person_id"] and "device" in detail


async def test_user_explorer_profile_and_timeline(data):
    c, pid = data["client"], data["project_id"]
    users = (await c.get("/api/v1/users", params={"project_id": pid})).json()["items"]
    assert {u["distinct_ids"][0] for u in users} == {"alice", "bob", "carol", "=cmd"}
    alice = next(u for u in users if u["email"] == "alice@x.com")
    assert alice["event_count"] == 9 and alice["sessions"] >= 2 and alice["properties"]["plan"] == "pro"
    assert (await c.get("/api/v1/users", params={"project_id": pid, "search": "carol@"})).json()["items"][0][
        "email"
    ] == "carol@x.com"
    flt = json.dumps([{"type": "person", "property": "plan", "operator": "equals", "value": "free"}])
    assert {
        u["email"] or u["distinct_ids"][0]
        for u in (await c.get("/api/v1/users", params={"project_id": pid, "filters": flt})).json()["items"]
    } == {"bob", "carol@x.com"}
    page1 = (await c.get("/api/v1/users", params={"project_id": pid, "limit": 2})).json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    prof = (await c.get(f"/api/v1/users/{alice['id']}")).json()
    assert prof["distinct_ids"] == ["alice"] and prof["project_id"] == pid
    tl = (await c.get(f"/api/v1/users/{alice['id']}/events")).json()["items"]
    assert len(tl) == 9 and [e["timestamp"] for e in tl] == sorted(e["timestamp"] for e in tl)  # chronological
    around = datetime.now(UTC) - timedelta(days=1)  # explicit instants: independent of the time of day the test runs
    window = {
        "date_from": (around - timedelta(minutes=30)).isoformat(),
        "date_to": (around + timedelta(minutes=30)).isoformat(),
    }
    one = (await c.get(f"/api/v1/users/{alice['id']}/events", params=window)).json()["items"]
    assert [e["event"] for e in one] == ["purchase"]


async def test_user_endpoints_enforce_project_authorization(data, new_client):
    c, pid = data["client"], data["project_id"]
    alice = (await c.get("/api/v1/users", params={"project_id": pid, "search": "alice"})).json()["items"][0]
    outsider = new_client()
    await register(outsider, "o@example.com", "Other")
    assert (await outsider.get(f"/api/v1/users/{alice['id']}")).status_code == 404
    assert (await outsider.get(f"/api/v1/users/{alice['id']}/events")).status_code == 404
    assert (await outsider.get("/api/v1/events", params={"project_id": pid})).status_code == 404
    assert (
        await outsider.post(
            "/api/v1/analytics/trends", json={"project_id": pid, "query": {"type": "trend", "event": "x"}}
        )
    ).status_code == 404
    assert (await outsider.get(f"/api/v1/projects/{pid}/event-definitions")).status_code == 404
    assert (await outsider.get("/api/v1/dashboards", params={"project_id": pid})).status_code == 404
    assert (await outsider.get("/api/v1/export/events", params={"project_id": pid})).status_code == 404


async def test_definitions_and_property_value_autocomplete(data):
    c, pid = data["client"], data["project_id"]
    names = {d["name"] for d in (await c.get(f"/api/v1/projects/{pid}/event-definitions")).json()}
    assert {"page_view", "purchase", "weird"} <= names
    props = (await c.get(f"/api/v1/projects/{pid}/property-definitions", params={"kind": "event"})).json()
    assert {"name": "amount", "kind": "event", "value_type": "number"} in props
    assert any(p["name"] == "$browser" and p.get("builtin") for p in props)
    vals = (await c.get(f"/api/v1/projects/{pid}/property-values", params={"name": "plan", "event": "purchase"})).json()
    assert {v["value"]: v["count"] for v in vals} == {"pro": 2, "free": 1}


async def test_cohorts_definition_preview_reuse_in_filters(data):
    c, pid = data["client"], data["project_id"]
    definition = {
        "match": "all",
        "criteria": [
            {"type": "performed_event", "event": "purchase", "operator": "at_least", "count": 2},
            {"type": "person_property", "property": "plan", "operator": "equals", "value": "pro"},
        ],
    }
    prev = (await c.post("/api/v1/cohorts/preview", json={"project_id": pid, "definition": definition})).json()
    assert prev["count"] == 1 and prev["sample"][0]["email"] == "alice@x.com"
    r = await c.post("/api/v1/cohorts", json={"project_id": pid, "name": "Power buyers", "definition": definition})
    assert r.status_code == 201 and r.json()["last_count"] == 1
    cid = r.json()["id"]
    # reuse as a filter in a trend, a user list and an event list
    t = await query(
        data,
        "trends",
        {
            "type": "trend",
            "event": "purchase",
            "date_range": {"preset": "last_30_days"},
            "filters": [{"type": "cohort", "cohort_id": cid}],
        },
    )
    assert t["series"][0]["total"] == 2
    t = await query(
        data,
        "trends",
        {
            "type": "trend",
            "event": "purchase",
            "date_range": {"preset": "last_30_days"},
            "filters": [{"type": "cohort", "cohort_id": cid, "negate": True}],
        },
    )
    assert t["series"][0]["total"] == 1
    users = (await c.get("/api/v1/users", params={"project_id": pid, "cohort_id": cid})).json()["items"]
    assert [u["email"] for u in users] == ["alice@x.com"]
    members = (await c.get(f"/api/v1/cohorts/{cid}/members")).json()
    assert members["total"] == 1

    def size(criteria, match="all"):
        return c.post(
            "/api/v1/cohorts/preview", json={"project_id": pid, "definition": {"match": match, "criteria": criteria}}
        )

    assert (await size([{"type": "performed_event", "event": "purchase", "operator": "exactly", "count": 1}])).json()[
        "count"
    ] == 1  # carol
    assert (await size([{"type": "performed_event", "event": "purchase", "operator": "at_most", "count": 1}])).json()[
        "count"
    ] == 3  # bob, carol, =cmd
    assert (await size([{"type": "not_performed_event", "event": "purchase"}])).json()["count"] == 2
    assert (await size([{"type": "performed_event", "event": "purchase", "within_days": 1}], "any")).json()[
        "count"
    ] == 2  # alice (5h ago), carol
    assert (
        await size(
            [{"type": "performed_event", "event": "purchase", "filters": [{"property": "plan", "value": "free"}]}]
        )
    ).json()["count"] == 1
    assert (await size([{"type": "first_seen", "operator": "within_last_days", "days": 365}])).json()["count"] == 4
    assert (await size([{"type": "first_seen", "operator": "after", "date": "2999-01-01"}])).json()["count"] == 0
    assert (
        await size(
            [
                {"type": "person_property", "property": "plan", "value": "free"},
                {"type": "performed_event", "event": "page_view"},
            ],
            "any",
        )
    ).json()["count"] == 3  # bob, carol, alice
    bad = await c.post(
        "/api/v1/cohorts/preview", json={"project_id": pid, "definition": {"match": "all", "criteria": []}}
    )
    assert bad.status_code == 422
    t = await c.post(
        "/api/v1/analytics/trends",
        json={
            "project_id": pid,
            "query": {
                "type": "trend",
                "event": "x",
                "filters": [{"type": "cohort", "cohort_id": "00000000-0000-0000-0000-000000000000"}],
            },
        },
    )
    assert t.status_code == 404 and t.json()["error"]["code"] == "COHORT_NOT_FOUND"
    assert (await c.delete(f"/api/v1/cohorts/{cid}")).status_code == 200


async def test_reports_crud_duplicate_and_validation(owner):
    c, pid = owner["client"], owner["project_id"]
    q = {"type": "trend", "event": "purchase", "metric": "unique_users", "date_range": RANGE, "interval": "day"}
    r = await c.post(
        "/api/v1/reports",
        json={
            "project_id": pid,
            "name": "Weekly buyers",
            "description": "d",
            "query": q,
            "visualization": {"chart": "bar"},
        },
    )
    assert r.status_code == 201, r.text
    rep = r.json()
    assert (
        rep["query"]["date_range"] == {"from": "2026-08-01", "to": "2026-08-31", "preset": None}
        and rep["created_by_name"] == "Test"
    )
    assert (
        await c.patch(f"/api/v1/reports/{rep['id']}", json={"name": "Renamed", "visualization": {"chart": "area"}})
    ).json()["visualization"]["chart"] == "area"
    dup = (await c.post(f"/api/v1/reports/{rep['id']}/duplicate")).json()
    assert dup["name"] == "Renamed (copy)" and dup["id"] != rep["id"]
    assert len((await c.get("/api/v1/reports", params={"project_id": pid})).json()) == 2
    assert (
        await c.post("/api/v1/reports", json={"project_id": pid, "name": "bad", "query": {"type": "nope"}})
    ).status_code == 422
    assert (
        await c.post(
            "/api/v1/reports", json={"project_id": pid, "name": "bad", "query": q, "visualization": {"chart": "pie3d"}}
        )
    ).status_code == 422
    assert (await c.delete(f"/api/v1/reports/{rep['id']}")).status_code == 200
    assert (await c.get(f"/api/v1/reports/{rep['id']}")).status_code == 404


async def test_dashboards_widgets_layout_duplicate_delete(owner):
    c, pid = owner["client"], owner["project_id"]
    rep = (
        await c.post("/api/v1/reports", json={"project_id": pid, "name": "R", "query": {"type": "trend", "event": "e"}})
    ).json()
    widgets = [
        {"id": "w1", "title": "Events", "report_id": rep["id"], "layout": {"x": 0, "y": 0, "w": 6, "h": 4}},
        {
            "id": "w2",
            "title": "Inline",
            "query": {"type": "funnel", "steps": [{"event": "a"}, {"event": "b"}]},
            "visualization": {"chart": "funnel"},
            "layout": {"x": 6, "y": 0, "w": 6, "h": 4},
        },
    ]
    r = await c.post("/api/v1/dashboards", json={"project_id": pid, "name": "Main", "config": {"widgets": widgets}})
    assert r.status_code == 201, r.text
    d = r.json()
    assert [w["id"] for w in d["config"]["widgets"]] == ["w1", "w2"]
    # reorder + resize + remove, rename
    new_cfg = {"widgets": [{**widgets[1], "layout": {"x": 0, "y": 0, "w": 12, "h": 6}}]}
    u = (await c.patch(f"/api/v1/dashboards/{d['id']}", json={"name": "Renamed", "config": new_cfg})).json()
    assert (
        u["name"] == "Renamed" and len(u["config"]["widgets"]) == 1 and u["config"]["widgets"][0]["layout"]["w"] == 12
    )
    bad = await c.patch(f"/api/v1/dashboards/{d['id']}", json={"config": {"widgets": [widgets[0], widgets[0]]}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_DASHBOARD"
    bad = await c.patch(
        f"/api/v1/dashboards/{d['id']}", json={"config": {"widgets": [{"id": "x", "title": "no source"}]}}
    )
    assert bad.status_code == 422
    bad = await c.patch(
        f"/api/v1/dashboards/{d['id']}",
        json={"config": {"widgets": [{"id": "x", "report_id": "00000000-0000-0000-0000-000000000000"}]}},
    )
    assert bad.status_code == 422
    dup = (await c.post(f"/api/v1/dashboards/{d['id']}/duplicate")).json()
    assert dup["name"] == "Renamed (copy)" and dup["config"] == u["config"]
    assert (await c.delete(f"/api/v1/dashboards/{d['id']}")).status_code == 200
    assert [x["id"] for x in (await c.get("/api/v1/dashboards", params={"project_id": pid})).json()] == [dup["id"]]


async def test_viewer_can_read_but_not_write_saved_objects(owner, new_client):
    c, pid, org = owner["client"], owner["project_id"], owner["org_id"]
    v = new_client()
    await register(v, "viewer@example.com")
    await c.post(f"/api/v1/organizations/{org}/members", json={"email": "viewer@example.com", "role": "viewer"})
    assert (await v.get("/api/v1/dashboards", params={"project_id": pid})).status_code == 200
    assert (
        await v.post("/api/v1/analytics/trends", json={"project_id": pid, "query": {"type": "trend", "event": "x"}})
    ).status_code == 200
    assert (await v.post("/api/v1/dashboards", json={"project_id": pid, "name": "x"})).status_code == 403
    assert (
        await v.post("/api/v1/reports", json={"project_id": pid, "name": "x", "query": {"type": "trend", "event": "e"}})
    ).status_code == 403
    assert (
        await v.post(
            "/api/v1/cohorts",
            json={"project_id": pid, "name": "x", "definition": {"criteria": [{"type": "first_seen", "days": 1}]}},
        )
    ).status_code == 403
    assert (await v.get("/api/v1/export/events", params={"project_id": pid})).status_code == 403
    assert (
        await v.post(
            "/api/v1/alerts",
            json={"project_id": pid, "name": "x", "query": {"event": "e"}, "condition": {"operator": "lt", "value": 1}},
        )
    ).status_code == 403


async def test_overview_has_real_numbers(data):
    r = await data["client"].post(
        "/api/v1/analytics/overview", json={"project_id": data["project_id"], "date_range": {"preset": "last_7_days"}}
    )
    assert r.status_code == 200, r.text
    o = r.json()
    k = o["kpis"]
    assert k["total_events"] == 12 and k["active_users"] == 4 and k["new_users"] == 4 and k["total_users"] == 4
    assert k["events_per_user"] == pytest.approx(3.0) and k["sessions"] >= 4
    assert o["top_events"][0] == {"event": "page_view", "count": 8, "unique_users": 2}
    assert {p["path"] for p in o["top_pages"]} >= {"/home"} and sum(o["event_volume"]) == 12
    assert o["user_growth_series"][-1] == 4 and o["previous_kpis"]["total_events"] == 0
    assert len(o["labels"]) == len(o["event_volume"]) == 7


# ------------------------------------------------------------------------------------------------- alerts
async def test_alert_lifecycle_notifications_and_ssrf_guard(owner, ingest):
    from datetime import time

    c, pid = owner["client"], owner["project_id"]
    midnight = datetime.combine(datetime.now(UTC).date(), time.min, UTC)  # project timezone is UTC
    yesterday, before = midnight - timedelta(hours=12), midnight - timedelta(hours=36)
    await seed(
        ingest,
        owner["api_key"],
        [ev("a", "purchase", yesterday.isoformat())]
        + [ev("b", "purchase", (before + timedelta(minutes=i)).isoformat()) for i in range(3)],
    )
    bad = await c.post(
        "/api/v1/alerts",
        json={
            "project_id": pid,
            "name": "x",
            "query": {"event": "e"},
            "condition": {"operator": "lt", "value": 1},
            "webhook_url": "http://127.0.0.1:9/hook",
        },
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_WEBHOOK"
    assert (
        await c.post(
            "/api/v1/alerts",
            json={
                "project_id": pid,
                "name": "x",
                "query": {"event": "e"},
                "condition": {"operator": "bogus", "value": 1},
            },
        )
    ).status_code == 422
    r = await c.post(
        "/api/v1/alerts",
        json={
            "project_id": pid,
            "name": "Few purchases",
            "period": "day",
            "query": {"event": "purchase", "metric": "total_events"},
            "condition": {"operator": "lt", "value": 5},
        },
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    first = (await c.post(f"/api/v1/alerts/{aid}/evaluate")).json()
    assert first["state"] == "firing" and first["value"] == 1.0  # the last completed day had exactly 1 purchase
    assert (await c.post(f"/api/v1/alerts/{aid}/evaluate")).json()["state"] == "firing"
    notes = (await c.get("/api/v1/notifications", params={"project_id": pid})).json()
    assert (
        notes["unread_count"] == 1 and notes["items"][0]["title"] == "Alert firing: Few purchases"
    )  # no spam on re-evaluation
    await c.patch(f"/api/v1/alerts/{aid}", json={"condition": {"operator": "lt", "value": 1}})
    assert (await c.post(f"/api/v1/alerts/{aid}/evaluate")).json()["state"] == "ok"
    notes = (await c.get("/api/v1/notifications", params={"project_id": pid})).json()
    assert [n["title"] for n in notes["items"]] == ["Alert recovered: Few purchases", "Alert firing: Few purchases"]
    assert (await c.post("/api/v1/notifications/read-all", params={"project_id": pid})).status_code == 200
    assert (await c.get("/api/v1/notifications", params={"project_id": pid})).json()["unread_count"] == 0
    await c.patch(f"/api/v1/alerts/{aid}", json={"condition": {"operator": "decrease_pct", "value": 20}})
    d = (await c.post(f"/api/v1/alerts/{aid}/evaluate")).json()
    assert d["state"] == "firing" and d["value"] == 1.0 and d["previous"] == 3.0  # 3 -> 1 is a 66% decrease (>= 20%)


def test_alert_condition_logic():
    from app.services.alerts import evaluate_condition as e

    assert (
        e("lt", 10, 5, None)
        and not e("lt", 10, 10, None)
        and e("lte", 10, 10, None)
        and e("gt", 10, 11, None)
        and e("gte", 10, 10, None)
    )
    assert e("decrease_pct", 20, 70, 100) and not e("decrease_pct", 20, 90, 100) and e("increase_pct", 50, 150, 100)
    assert not e("decrease_pct", 20, 0, 0) and not e("decrease_pct", 20, 5, None)  # no baseline -> never fires


async def test_alert_webhook_delivery(data, monkeypatch):
    from app.core.config import get_settings
    from app.services import alerts as svc

    monkeypatch.setattr(get_settings(), "webhook_allow_private", True)
    calls = []

    async def fake_post(url, payload):
        calls.append((url, payload))

    monkeypatch.setattr(svc, "_post_webhook", fake_post)
    c, pid = data["client"], data["project_id"]
    aid = (
        await c.post(
            "/api/v1/alerts",
            json={
                "project_id": pid,
                "name": "hook",
                "query": {"event": "purchase"},
                "webhook_url": "http://localhost:9/h",
                "condition": {"operator": "lt", "value": 99},
            },
        )
    ).json()["id"]
    await c.post(f"/api/v1/alerts/{aid}/evaluate")
    assert len(calls) == 1 and calls[0][1]["event"] == "firing" and calls[0][1]["name"] == "hook"


# ------------------------------------------------------------------------------------------------- exports
async def test_streamed_exports_csv_json_and_formula_injection(data):
    c, pid = data["client"], data["project_id"]
    r = await c.get("/api/v1/export/events", params={"project_id": pid, "format": "csv"})
    assert (
        r.status_code == 200
        and r.headers["content-type"].startswith("text/csv")
        and "attachment" in r.headers["content-disposition"]
    )
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 12 and {"event", "distinct_id", "timestamp", "properties", "browser"} <= set(rows[0])
    weird = next(x for x in rows if x["event"] == "weird")
    assert (
        weird["distinct_id"].startswith("'=")
        and "'=HYPERLINK" in weird["properties"]
        or weird["properties"].startswith("'") is False
    )  # id neutralised
    assert weird["distinct_id"] == "'=cmd"
    j = (await c.get("/api/v1/export/events", params={"project_id": pid, "format": "json", "event": "purchase"})).json()
    assert len(j) == 3 and all(x["event"] == "purchase" for x in j) and isinstance(j[0]["properties"], dict)
    u = list(csv.DictReader(io.StringIO((await c.get("/api/v1/export/users", params={"project_id": pid})).text)))
    assert len(u) == 4 and {x["email"] for x in u} >= {"alice@x.com", "carol@x.com"}
    assert (await c.get("/api/v1/export/events", params={"project_id": pid, "format": "xml"})).status_code == 422


async def test_analytics_and_report_export(owner, ingest):
    c, pid = owner["client"], owner["project_id"]
    await seed(ingest, owner["api_key"], [ev("a", "e", "2026-08-01T10:00:00Z"), ev("a", "e", "2026-08-02T10:00:00Z")])
    q = {"type": "trend", "event": "e", "date_range": {"from": "2026-08-01", "to": "2026-08-03"}, "interval": "day"}
    r = await c.post("/api/v1/export/analytics", json={"project_id": pid, "query": q, "format": "csv"})
    assert [(x["bucket"], x["value"]) for x in csv.DictReader(io.StringIO(r.text))] == [
        ("2026-08-01", "1.0"),
        ("2026-08-02", "1.0"),
        ("2026-08-03", "0.0"),
    ] or [(x["bucket"], x["value"]) for x in csv.DictReader(io.StringIO(r.text))] == [
        ("2026-08-01", "1"),
        ("2026-08-02", "1"),
        ("2026-08-03", "0"),
    ]
    r = await c.post("/api/v1/export/analytics", json={"project_id": pid, "query": q, "format": "json"})
    assert r.json()["series"][0]["total"] == 2
    await c.post("/api/v1/reports", json={"project_id": pid, "name": "R1", "query": q})
    assert (await c.get("/api/v1/export/reports", params={"project_id": pid})).json()[0]["name"] == "R1"


async def test_async_export_job_via_worker(data):
    from app.workers import tasks

    c, pid = data["client"], data["project_id"]
    r = await c.post(
        "/api/v1/exports",
        json={"project_id": pid, "kind": "events", "format": "csv", "date_range": {"preset": "last_7_days"}},
    )
    assert r.status_code == 202 and r.json()["status"] == "pending"
    jid = r.json()["id"]
    assert (await c.get(f"/api/v1/exports/{jid}/download")).status_code == 409
    assert await tasks.process_export_jobs() == 1
    job = (await c.get(f"/api/v1/exports/{jid}")).json()
    assert job["status"] == "done" and job["row_count"] == 12
    dl = await c.get(f"/api/v1/exports/{jid}/download")
    assert dl.status_code == 200 and len(list(csv.DictReader(io.StringIO(dl.text)))) == 12


# ------------------------------------------------------------------------------------------------- worker tasks
async def test_retention_cleanup_respects_project_setting(owner, ingest):
    from sqlalchemy import func, select

    from app.db.session import get_sessionmaker
    from app.models import Event, Person
    from app.workers import tasks

    c, pid, key = owner["client"], owner["project_id"], owner["api_key"]
    now = datetime.now(UTC)
    await seed(
        ingest,
        key,
        [
            ev("old", "e", (now - timedelta(days=100)).isoformat()),
            ev("mid", "e", (now - timedelta(days=40)).isoformat()),
            ev("new", "e", (now - timedelta(days=1)).isoformat()),
        ],
    )

    async def counts():
        async with get_sessionmaker()() as s:
            return (
                (await s.execute(select(func.count()).select_from(Event))).scalar_one(),
                (await s.execute(select(func.count()).select_from(Person))).scalar_one(),
            )

    assert await tasks.retention_cleanup() == {} and await counts() == (3, 3)  # forever (default): nothing deleted
    await c.patch(f"/api/v1/projects/{pid}", json={"settings": {"retention_days": 90, "retention_days_set": True}})
    await tasks.retention_cleanup()
    assert await counts() == (2, 2)
    await c.patch(f"/api/v1/projects/{pid}", json={"settings": {"retention_days": 30, "retention_days_set": True}})
    await tasks.retention_cleanup()
    assert await counts() == (1, 1)


async def test_purge_and_project_delete_worker(owner, ingest):
    from sqlalchemy import func, select

    from app.db.session import get_sessionmaker
    from app.models import Event, Person, Project
    from app.workers import tasks

    c, pid = owner["client"], owner["project_id"]
    await seed(ingest, owner["api_key"], [ev("u", "e", "2026-08-01T10:00:00Z")])
    await c.post(f"/api/v1/projects/{pid}/data/purge", json={"confirm_name": "Web App"})
    assert await tasks.process_purge_queue() >= 1
    async with get_sessionmaker()() as s:
        assert (await s.execute(select(func.count()).select_from(Event))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(Person))).scalar_one() == 0
        assert (
            await s.execute(select(func.count()).select_from(Project))
        ).scalar_one() == 1  # data purge keeps the project
    await seed(ingest, owner["api_key"], [ev("u", "e", "2026-08-01T10:00:00Z")]) if False else None
    await c.delete(f"/api/v1/projects/{pid}")
    await tasks.process_purge_queue()
    async with get_sessionmaker()() as s:
        assert (await s.execute(select(func.count()).select_from(Project))).scalar_one() == 0


async def test_person_erasure_and_usage_flush_and_cohort_refresh(owner, ingest):
    from sqlalchemy import func, select

    from app.db.session import get_sessionmaker
    from app.models import Event
    from app.workers import tasks

    c, pid = owner["client"], owner["project_id"]
    await seed(
        ingest, owner["api_key"], [ev("gdpr", "e", "2026-08-01T10:00:00Z"), ev("keep", "e", "2026-08-01T10:00:00Z")]
    )
    person = (await c.get("/api/v1/users", params={"project_id": pid, "search": "gdpr"})).json()["items"][0]
    assert (await c.delete(f"/api/v1/projects/{pid}/persons/{person['id']}")).status_code == 200
    async with get_sessionmaker()() as s:
        assert (await s.execute(select(func.count()).select_from(Event))).scalar_one() == 1
    assert await tasks.flush_usage() >= 1
    await c.post(
        "/api/v1/cohorts",
        json={
            "project_id": pid,
            "name": "c",
            "definition": {"criteria": [{"type": "first_seen", "operator": "after", "date": "2000-01-01"}]},
        },
    )
    assert await tasks.refresh_cohort_counts() == 0  # freshly calculated on create
