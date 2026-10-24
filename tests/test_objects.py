import json

import pytest

from tests.analytics_helpers import analytics, ev, seed
from tests.conftest import register

D = "2026-08-03T"
RANGE = {"from": "2026-08-03", "to": "2026-08-03"}


@pytest.fixture
async def proj(owner, ingest):
    E = [ev("login", "a", D + "10:00:00", _set={"plan": "pro"}), ev("login", "a", D + "11:00:00"), ev("login", "a", D + "12:00:00"),
         ev("purchase", "a", D + "12:30:00", amount=50),
         ev("login", "b", D + "10:00:00", _set={"plan": "pro"}), ev("purchase", "b", D + "10:10:00", amount=5),
         ev("login", "c", D + "10:00:00", _set={"plan": "free"}), ev("login", "c", D + "11:00:00"), ev("login", "c", D + "12:00:00"),
         ev("login", "c", D + "13:00:00"), ev("purchase", "c", D + "13:30:00", amount=500)]
    await seed(ingest, owner["api_key"], E)
    return owner


async def test_behavioural_cohort_and_reuse_as_filter(proj):
    c, pid = proj["client"], proj["project_id"]
    definition = {"match": "all", "criteria": [
        {"type": "performed_event", "event": "purchase"},
        {"type": "performed_event", "event": "login", "operator": "at_least", "count": 3},
        {"type": "person_property", "property": "plan", "operator": "equals", "value": "pro"}]}
    r = await c.post("/api/v1/cohorts", json={"project_id": pid, "name": "Engaged pro buyers", "definition": definition})
    assert r.status_code == 201, r.text
    cohort = r.json()
    assert cohort["last_count"] == 1  # only "a": purchased, 3 logins, pro
    members = (await c.get(f"/api/v1/cohorts/{cohort['id']}/members")).json()
    assert members["total"] == 1 and members["items"][0]["distinct_ids"] == ["a"]
    # reuse in an insight filter, and negated
    tr = await analytics(c, "trends", pid, event="login", date_range=RANGE, filters=[{"type": "cohort", "cohort_id": cohort["id"]}])
    assert tr["series"][0]["total"] == 3
    tr = await analytics(c, "trends", pid, event="login", date_range=RANGE, filters=[{"type": "cohort", "cohort_id": cohort["id"], "negate": True}])
    assert tr["series"][0]["total"] == 5
    users = (await c.get("/api/v1/users", params={"project_id": pid, "filters": json.dumps([{"type": "cohort", "cohort_id": cohort["id"]}])})).json()
    assert [u["distinct_ids"] for u in users["items"]] == [["a"]]


async def test_cohort_criteria_variants(proj):
    c, pid = proj["client"], proj["project_id"]

    async def count(criteria, match="all"):
        r = await c.post("/api/v1/cohorts/preview", json={"project_id": pid, "definition": {"match": match, "criteria": criteria}})
        assert r.status_code == 200, r.text
        return r.json()["count"]

    assert await count([{"type": "performed_event", "event": "purchase", "filters": [{"property": "amount", "operator": "gt", "value": 40}]}]) == 2
    assert await count([{"type": "not_performed_event", "event": "purchase"}]) == 0
    assert await count([{"type": "performed_event", "event": "login", "operator": "exactly", "count": 1}]) == 1
    assert await count([{"type": "performed_event", "event": "login", "operator": "at_most", "count": 1}]) == 1
    assert await count([{"type": "performed_event", "event": "login", "operator": "at_most", "count": 3}]) == 2
    assert await count([{"type": "person_property", "property": "plan", "value": "free"},
                        {"type": "performed_event", "event": "login", "count": 3}], match="any") == 2
    assert await count([{"type": "first_seen", "operator": "within_last_days", "days": 5}]) == 0
    assert await count([{"type": "first_seen", "operator": "before", "date": "2026-08-04"}]) == 3
    assert (await c.post("/api/v1/cohorts/preview", json={"project_id": pid, "definition": {"criteria": []}})).status_code == 422


async def test_reports_crud_duplicate_and_ownership(proj, new_client):
    c, pid = proj["client"], proj["project_id"]
    q = {"type": "trend", "event": "purchase", "metric": "unique_users", "date_range": RANGE, "interval": "day",
         "filters": [{"property": "plan", "operator": "equals", "value": "pro"}]}
    r = await c.post("/api/v1/reports", json={"project_id": pid, "name": "Buyers", "description": "d", "query": q, "visualization": {"chart": "bar"}})
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["created_by_name"] and rep["query"]["series"][0]["event"] == "purchase" and rep["visualization"] == {"chart": "bar"}
    assert (await c.patch(f"/api/v1/reports/{rep['id']}", json={"name": "Renamed"})).json()["name"] == "Renamed"
    dup = (await c.post(f"/api/v1/reports/{rep['id']}/duplicate")).json()
    assert dup["name"] == "Renamed (copy)" and dup["id"] != rep["id"]
    assert len((await c.get("/api/v1/reports", params={"project_id": pid})).json()) == 2
    # a saved query can be re-run through the generic endpoint
    run = await c.post("/api/v1/analytics/query", json={"project_id": pid, "query": rep["query"]})
    assert run.status_code == 200 and run.json()["query_type"] == "trend"
    # invalid saved query rejected
    assert (await c.post("/api/v1/reports", json={"project_id": pid, "name": "x", "query": {"type": "nope"}})).status_code == 422
    out = new_client()
    await register(out, "stranger@example.com", "Z")
    assert (await out.get(f"/api/v1/reports/{rep['id']}")).status_code == 404
    assert (await out.delete(f"/api/v1/reports/{rep['id']}")).status_code == 404
    assert (await c.delete(f"/api/v1/reports/{rep['id']}")).status_code == 200


async def test_dashboard_widgets_lifecycle(proj):
    c, pid = proj["client"], proj["project_id"]
    rep = (await c.post("/api/v1/reports", json={"project_id": pid, "name": "R", "query": {"type": "trend", "event": "login", "date_range": RANGE}})).json()
    d = (await c.post("/api/v1/dashboards", json={"project_id": pid, "name": "Main"})).json()
    assert d["config"]["widgets"] == []
    widgets = [
        {"id": "w1", "title": "Logins", "report_id": rep["id"], "visualization": {"chart": "line"}, "layout": {"x": 0, "y": 0, "w": 6, "h": 4}},
        {"id": "w2", "title": "Inline funnel", "query": {"type": "funnel", "steps": [{"event": "login"}, {"event": "purchase"}], "date_range": RANGE},
         "layout": {"x": 6, "y": 0, "w": 6, "h": 4}},
    ]
    r = await c.patch(f"/api/v1/dashboards/{d['id']}", json={"config": {"widgets": widgets}})
    assert r.status_code == 200, r.text
    assert [w["id"] for w in r.json()["config"]["widgets"]] == ["w1", "w2"]
    # resize + reorder + remove persisted as structured JSON
    widgets[0]["layout"] = {"x": 0, "y": 4, "w": 12, "h": 6}
    r = await c.patch(f"/api/v1/dashboards/{d['id']}", json={"config": {"widgets": [widgets[0]]}, "name": "Renamed"})
    got = (await c.get(f"/api/v1/dashboards/{d['id']}")).json()
    assert got["name"] == "Renamed" and got["config"]["widgets"][0]["layout"] == {"x": 0, "y": 4, "w": 12, "h": 6} and len(got["config"]["widgets"]) == 1
    dup = (await c.post(f"/api/v1/dashboards/{d['id']}/duplicate")).json()
    assert dup["name"] == "Renamed (copy)" and dup["config"]["widgets"][0]["id"] != "w1"
    # validation
    bad = await c.patch(f"/api/v1/dashboards/{d['id']}", json={"config": {"widgets": [{"id": "x", "report_id": "00000000-0000-0000-0000-000000000000"}]}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_DASHBOARD"
    bad = await c.patch(f"/api/v1/dashboards/{d['id']}", json={"config": {"widgets": [{"id": "x", "title": "no query"}]}})
    assert bad.status_code == 422
    assert (await c.delete(f"/api/v1/dashboards/{d['id']}")).status_code == 200


async def test_viewer_cannot_write_saved_objects(proj, new_client):
    c, pid, org = proj["client"], proj["project_id"], proj["org_id"]
    v = new_client()
    await register(v, "viewer2@example.com")
    await c.post(f"/api/v1/organizations/{org}/members", json={"email": "viewer2@example.com", "role": "viewer"})
    assert (await v.get("/api/v1/dashboards", params={"project_id": pid})).status_code == 200
    assert (await v.post("/api/v1/dashboards", json={"project_id": pid, "name": "x"})).status_code == 403
    assert (await v.post("/api/v1/reports", json={"project_id": pid, "name": "x", "query": {"type": "trend", "event": "a"}})).status_code == 403
    assert (await v.get("/api/v1/export/events", params={"project_id": pid})).status_code == 403
    assert (await v.post("/api/v1/analytics/trends", json={"project_id": pid, "event": "login", "date_range": RANGE})).status_code == 200


async def test_alerts_threshold_and_percent_change_with_notifications(proj, ingest):
    c, pid = proj["client"], proj["project_id"]
    a = (await c.post("/api/v1/alerts", json={"project_id": pid, "name": "Few signups", "query": {"event": "signup", "metric": "unique_users"},
                                               "condition": {"operator": "lt", "value": 5}, "period": "day"})).json()
    r = await c.post(f"/api/v1/alerts/{a['id']}/evaluate")
    assert r.status_code == 200 and r.json()["state"] == "firing" and r.json()["last_value"] == 0
    n = (await c.get("/api/v1/notifications", params={"project_id": pid})).json()
    assert n["unread"] == 1 and "Few signups" in n["items"][0]["title"]
    await c.post(f"/api/v1/alerts/{a['id']}/evaluate")  # still firing: no duplicate notification
    assert len((await c.get("/api/v1/notifications", params={"project_id": pid})).json()["items"]) == 1
    await c.post("/api/v1/notifications/read", params={"project_id": pid})
    assert (await c.get("/api/v1/notifications", params={"project_id": pid})).json()["unread"] == 0
    assert (await c.patch(f"/api/v1/alerts/{a['id']}", json={"condition": {"operator": "gte", "value": 1}})).status_code == 200
    assert (await c.post(f"/api/v1/alerts/{a['id']}/evaluate")).json()["state"] == "ok"  # recovered
    # percent decrease: pure logic
    from app.services.alerts import is_firing, period_bounds
    assert is_firing("decrease_pct", 20, 70, 100) and not is_firing("decrease_pct", 20, 90, 100)
    assert not is_firing("decrease_pct", 20, 0, 0) and is_firing("increase_pct", 50, 160, 100)
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo
    cs, ce, ps, pe = period_bounds("day", ZoneInfo("Asia/Kolkata"), datetime(2026, 8, 5, 3, 0, tzinfo=UTC))
    assert (cs.isoformat(), ce.isoformat()) == ("2026-08-03T18:30:00+00:00", "2026-08-04T18:30:00+00:00") and pe == cs


async def test_alert_webhook_ssrf_protection(proj):
    c, pid = proj["client"], proj["project_id"]
    base = {"project_id": pid, "name": "w", "query": {"event": "x"}, "condition": {"operator": "lt", "value": 1}}
    for url in ("http://127.0.0.1:8080/hook", "http://localhost/hook", "http://169.254.169.254/latest", "ftp://example.com/x", "javascript:alert(1)"):
        r = await c.post("/api/v1/alerts", json={**base, "webhook_url": url})
        assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_WEBHOOK", url


async def test_streamed_exports_csv_json_and_formula_injection(proj, ingest):
    c, pid = proj["client"], proj["project_id"]
    await seed(ingest, proj["api_key"], [ev("=HYPERLINK(\"http://evil\")", "evil", D + "14:00:00")])
    r = await c.get("/api/v1/export/events", params={"project_id": pid, "format": "csv", "preset": None, "date_from": "2026-08-03", "date_to": "2026-08-03"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = r.text.lstrip("\ufeff").strip().splitlines()
    assert lines[0].startswith("id,timestamp,event,distinct_id") and len(lines) == 1 + 12
    assert "'=HYPERLINK" in r.text and ",=HYPERLINK" not in r.text
    r = await c.get("/api/v1/export/events", params={"project_id": pid, "format": "json", "event": "purchase", "date_from": "2026-08-03", "date_to": "2026-08-03"})
    data = r.json()
    assert len(data) == 3 and {e["event"] for e in data} == {"purchase"}
    users = (await c.get("/api/v1/export/users", params={"project_id": pid, "format": "csv"})).text
    assert len(users.strip().splitlines()) == 1 + 4
    reports = await c.get("/api/v1/export/reports", params={"project_id": pid, "format": "json"})
    assert reports.status_code == 200 and reports.json() == []


async def test_analytics_result_export_and_async_job(proj):
    from app.workers.tasks import process_exports

    c, pid = proj["client"], proj["project_id"]
    q = {"type": "trend", "event": "login", "date_range": RANGE, "interval": "hour"}
    r = await c.post("/api/v1/export/analytics", json={"project_id": pid, "query": q, "format": "csv"})
    assert r.status_code == 200 and r.text.lstrip("\ufeff").splitlines()[0] == "series,bucket,value"
    assert len(r.text.strip().splitlines()) == 1 + 24
    job = (await c.post("/api/v1/export/jobs", json={"project_id": pid, "kind": "events", "format": "csv",
                                                       "params": {"date_range": RANGE, "events": ["login"]}})).json()
    assert job["status"] == "pending"
    assert (await c.get(f"/api/v1/export/jobs/{job['id']}/download")).status_code == 409
    await process_exports()
    done = (await c.get(f"/api/v1/export/jobs/{job['id']}")).json()
    assert done["status"] == "done" and done["row_count"] == 8
    dl = await c.get(f"/api/v1/export/jobs/{job['id']}/download")
    assert dl.status_code == 200 and len(dl.text.strip().splitlines()) == 1 + 8


async def test_explorer_events_cursor_pagination_filters_and_detail(proj):
    c, pid = proj["client"], proj["project_id"]
    base = {"project_id": pid, "date_from": "2026-08-03", "date_to": "2026-08-03"}
    seen, cursor = [], None
    while True:
        page = (await c.get("/api/v1/events", params={**base, "limit": 4, **({"cursor": cursor} if cursor else {})})).json()
        seen += [e["id"] for e in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 11 and len(set(seen)) == 11  # no gaps, no duplicates across pages
    flt = json.dumps([{"property": "amount", "operator": "gt", "value": 40}])
    r = (await c.get("/api/v1/events", params={**base, "event": "purchase", "filters": flt})).json()
    assert {e["properties"]["amount"] for e in r["items"]} == {50, 500}
    assert (await c.get("/api/v1/events", params={**base, "search": "purch"})).json()["items"].__len__() == 3
    assert (await c.get("/api/v1/events", params={**base, "distinct_id": "b"})).json()["items"].__len__() == 2
    assert (await c.get("/api/v1/events", params={**base, "filters": "not json"})).status_code == 422
    one = (await c.get(f"/api/v1/events/{seen[0]}", params={"project_id": pid})).json()
    assert one["person"]["properties"] and "device" in one and one["id"] == seen[0]


async def test_user_explorer_and_timeline(proj):
    c, pid = proj["client"], proj["project_id"]
    users = (await c.get("/api/v1/users", params={"project_id": pid})).json()["items"]
    a = next(u for u in users if u["distinct_ids"] == ["a"])
    assert a["event_count"] == 4 and a["sessions"] == 3 and a["properties"]["plan"] == "pro"
    assert [u["distinct_ids"][0] for u in (await c.get("/api/v1/users", params={"project_id": pid, "search": "c"})).json()["items"]] == ["c"]
    p1 = (await c.get("/api/v1/users", params={"project_id": pid, "limit": 2})).json()
    p2 = (await c.get("/api/v1/users", params={"project_id": pid, "limit": 2, "cursor": p1["next_cursor"]})).json()
    assert len(p1["items"]) == 2 and len(p2["items"]) == 1 and p2["next_cursor"] is None
    detail = (await c.get(f"/api/v1/users/{a['id']}")).json()
    assert detail["first_seen"].startswith("2026-08-03T10:00")
    tl = (await c.get(f"/api/v1/users/{a['id']}/events", params={"order": "asc"})).json()["items"]
    assert [e["event"] for e in tl] == ["login", "login", "login", "purchase"]
    fl = (await c.get(f"/api/v1/users/{a['id']}/events", params={"date_from": "2026-08-04", "date_to": "2026-08-05"})).json()["items"]
    assert fl == []


async def test_meta_definitions_and_overview(proj):
    c, pid = proj["client"], proj["project_id"]
    assert {e["name"] for e in (await c.get("/api/v1/meta/event-definitions", params={"project_id": pid})).json()} == {"login", "purchase"}
    props = (await c.get("/api/v1/meta/property-definitions", params={"project_id": pid, "kind": "person"})).json()
    assert {"name": "plan", "kind": "person", "value_type": "string"} in props
    vals = (await c.get("/api/v1/meta/property-values", params={"project_id": pid, "name": "plan", "kind": "person"})).json()
    assert {v["value"]: v["count"] for v in vals} == {"pro": 2, "free": 1}
    ov = (await c.post("/api/v1/analytics/overview", json={"project_id": pid, "date_range": RANGE, "interval": "day"})).json()
    k = ov["kpis"]
    assert (k["total_events"], k["active_users"], k["new_users"], k["total_users"]) == (11, 3, 3, 3)
    assert k["events_per_user"] == pytest.approx(11 / 3) and k["sessions"] >= 3
    assert ov["top_events"][0] == {"event": "login", "count": 8, "unique_users": 3}
    assert ov["event_volume"] == [11.0] and ov["user_growth_series"] == [3.0] and ov["retention"]["offsets"] == list(range(8))
    assert ov["previous_kpis"]["total_events"] == 0
