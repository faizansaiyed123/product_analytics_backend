import pytest

from tests.analytics_helpers import analytics, ev, seed

D = "2026-08-03T"
RANGE = {"from": "2026-08-03", "to": "2026-08-03"}


async def test_funnel_attribution_order_window_and_timing(owner, ingest):
    E = [
        ev("A", "u1", D + "10:00:00"), ev("B", "u1", D + "10:10:00"), ev("C", "u1", D + "10:20:00"),
        ev("A", "u2", D + "10:00:00"), ev("B", "u2", D + "10:30:00"),
        ev("A", "u3", D + "10:00:00"), ev("C", "u3", D + "10:05:00"),            # skipped B
        ev("B", "u4", D + "10:00:00"), ev("A", "u4", D + "10:30:00"),            # wrong order
        ev("A", "u5", D + "09:00:00"), ev("B", "u5", D + "11:30:00"),            # first attempt outside window
        ev("A", "u5", D + "12:00:00"), ev("B", "u5", D + "12:10:00"), ev("C", "u5", D + "12:20:00"),  # second attempt wins
        ev("A", "u6", D + "10:00:00"), ev("C", "u6", D + "10:10:00"), ev("B", "u6", D + "10:20:00"),  # C before B
        ev("other", "u7", D + "10:00:00"),
        ev("A", "u8", "2026-08-02T23:00:00"), ev("B", "u8", D + "00:10:00"),     # entry before the range
    ]
    await seed(ingest, owner["api_key"], E)
    r = await analytics(owner["client"], "funnels", owner["project_id"], date_range=RANGE,
                        steps=[{"event": "A"}, {"event": "B"}, {"event": "C"}], conversion_window={"value": 1, "unit": "hour"})
    s = r["steps"]
    assert [x["count"] for x in s] == [6, 4, 2]
    assert s[1]["conversion_rate"] == pytest.approx(4 / 6) and s[1]["dropoff_count"] == 2
    assert s[2]["conversion_rate"] == pytest.approx(0.5) and s[2]["overall_conversion_rate"] == pytest.approx(2 / 6)
    assert r["overall_conversion_rate"] == pytest.approx(2 / 6)
    assert s[1]["avg_time_to_convert_seconds"] == pytest.approx(1050) and s[1]["median_time_to_convert_seconds"] == pytest.approx(900)
    assert s[2]["avg_time_to_convert_seconds"] == pytest.approx(600)


async def test_funnel_same_event_twice_needs_two_occurrences(owner, ingest):
    await seed(ingest, owner["api_key"], [ev("view", "a", D + "10:00:00"), ev("view", "b", D + "10:00:00"), ev("view", "b", D + "10:05:00")])
    r = await analytics(owner["client"], "funnels", owner["project_id"], date_range=RANGE, steps=[{"event": "view"}, {"event": "view"}])
    assert [x["count"] for x in r["steps"]] == [2, 1]


async def test_funnel_step_filters_global_filters_and_breakdown(owner, ingest):
    E = [ev("A", "p1", D + "10:00:00", plan="pro"), ev("B", "p1", D + "10:01:00", ok=True),
         ev("A", "p2", D + "10:00:00", plan="pro"), ev("B", "p2", D + "10:01:00", ok=False),
         ev("A", "f1", D + "10:00:00", plan="free"), ev("B", "f1", D + "10:01:00", ok=True)]
    await seed(ingest, owner["api_key"], E)
    c, pid = owner["client"], owner["project_id"]
    r = await analytics(c, "funnels", pid, date_range=RANGE, steps=[{"event": "A"}, {"event": "B", "filters": [{"property": "ok", "operator": "equals", "value": True}]}])
    assert [x["count"] for x in r["steps"]] == [3, 2]
    r = await analytics(c, "funnels", pid, date_range=RANGE, steps=[{"event": "A"}, {"event": "B"}], breakdown={"type": "event", "property": "plan"})
    by = {b["value"]: [x["count"] for x in b["steps"]] for b in r["breakdowns"]}
    assert by == {"pro": [2, 2], "free": [1, 1]}


async def test_funnel_merges_anonymous_and_identified_activity(owner, ingest):
    k = owner["api_key"]
    await seed(ingest, k, [ev("A", "anon-9", D + "10:00:00")])
    await seed(ingest, k, [{"event": "$identify", "distinct_id": "real-9", "anonymous_id": "anon-9", "timestamp": D + "10:01:00Z"},
                           ev("B", "real-9", D + "10:02:00")])
    r = await analytics(owner["client"], "funnels", owner["project_id"], date_range=RANGE, steps=[{"event": "A"}, {"event": "B"}])
    assert [x["count"] for x in r["steps"]] == [1, 1]  # one person, not two


async def test_funnel_validation(owner):
    r = await owner["client"].post("/api/v1/analytics/funnels", json={"project_id": owner["project_id"], "steps": [{"event": "A"}]})
    assert r.status_code == 422


async def test_retention_first_time_and_exact_offsets(owner, ingest):
    d = lambda day, h="10:00:00": f"2026-08-{day:02d}T{h}"  # noqa: E731
    E = [ev("app", "u1", d(3)), ev("app", "u1", d(4)), ev("app", "u1", d(10)),
         ev("app", "u2", d(3)), ev("app", "u2", d(4)),
         ev("app", "u3", d(3)),
         ev("app", "u4", d(4)), ev("app", "u4", d(5)),
         ev("app", "u5", "2026-07-01T10:00:00"), ev("app", "u5", d(3))]  # first seen before the range -> not a cohort member
    await seed(ingest, owner["api_key"], E)
    r = await analytics(owner["client"], "retention", owner["project_id"], interval="day", offsets=[0, 1, 7],
                        date_range={"from": "2026-08-03", "to": "2026-08-05"})
    cohorts = {c["period"]: c for c in r["cohorts"]}
    assert set(cohorts) == {"2026-08-03", "2026-08-04"}
    c3 = cohorts["2026-08-03"]
    assert c3["size"] == 3
    assert [x["count"] for x in c3["cells"]] == [3, 2, 1]
    assert c3["cells"][1]["rate"] == pytest.approx(2 / 3)
    c4 = cohorts["2026-08-04"]
    assert c4["size"] == 1 and [x["count"] for x in c4["cells"]] == [1, 1, 0]
    avg = {x["offset"]: x["rate"] for x in r["average"]}
    assert avg[0] == 1.0 and avg[1] == pytest.approx(3 / 4) and avg[7] == pytest.approx(1 / 4)


async def test_retention_future_periods_are_null_not_zero(owner, ingest):
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    await seed(ingest, owner["api_key"], [ev("app", "n1", (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"))])
    r = await analytics(owner["client"], "retention", owner["project_id"], interval="day", offsets=[0, 1, 5],
                        date_range={"from": (now - timedelta(days=3)).strftime("%Y-%m-%d")})
    cells = r["cohorts"][0]["cells"]
    assert cells[0]["count"] == 1 and cells[2]["count"] is None and cells[2]["rate"] is None


async def test_retention_initial_vs_return_event_weekly_and_recurring(owner, ingest):
    E = [ev("signup", "w1", "2026-07-27T10:00:00"), ev("login", "w1", "2026-08-04T10:00:00"),
         ev("signup", "w2", "2026-07-28T10:00:00"), ev("login", "w2", "2026-07-29T10:00:00"),
         ev("signup", "w3", "2026-07-29T10:00:00")]
    await seed(ingest, owner["api_key"], E)
    c, pid = owner["client"], owner["project_id"]
    r = await analytics(c, "retention", pid, interval="week", offsets=[0, 1], initial_event={"event": "signup"},
                        return_event={"event": "login"}, date_range={"from": "2026-07-27", "to": "2026-08-02"})
    (coh,) = r["cohorts"]
    assert coh["period"] == "2026-07-27" and coh["size"] == 3
    assert [x["count"] for x in coh["cells"]] == [1, 1]  # only w2 logged in during week 0; only w1 in week 1
    rec = await analytics(c, "retention", pid, interval="day", offsets=[0, 1], cohort_type="recurring",
                          initial_event={"event": "signup"}, return_event={"event": "login"}, date_range={"from": "2026-07-28", "to": "2026-07-28"})
    assert rec["cohorts"][0]["size"] == 1 and [x["count"] for x in rec["cohorts"][0]["cells"]] == [0, 1]


async def test_sessions_inactivity_timeout_and_late_events(owner, ingest):
    k, c, pid = owner["api_key"], owner["client"], owner["project_id"]
    pv = lambda u, t, p="/": ev("$pageview", u, D + t, **{"$pathname": p})  # noqa: E731
    await seed(ingest, k, [pv("s1", "10:00:00", "/home"), pv("s1", "10:10:00", "/pricing"), ev("click", "s1", D + "10:50:00"),
                           pv("s2", "11:00:00", "/home")])
    r = await analytics(c, "sessions", pid, date_range=RANGE)
    sm = r["summary"]
    assert sm["sessions"] == 3 and sm["unique_users"] == 2
    assert sm["avg_events_per_session"] == pytest.approx(4 / 3)
    assert sm["avg_duration_seconds"] == pytest.approx((600 + 0 + 0) / 3)
    assert sm["avg_pages_per_session"] == pytest.approx(1.0) and sm["bounce_rate"] == pytest.approx(2 / 3)
    # a late-arriving event bridges the gap: 10:10 -> 10:30 -> 10:50 are all within 30 minutes
    await seed(ingest, k, [ev("late", "s1", D + "10:30:00")])
    r = await analytics(c, "sessions", pid, date_range=RANGE, refresh=True)
    assert r["summary"]["sessions"] == 2
    lst = (await c.post("/api/v1/analytics/sessions/list", json={"project_id": pid, "date_range": RANGE})).json()["items"]
    s1 = next(s for s in lst if s["distinct_id"] == "s1")
    assert s1["events"] == 4 and s1["duration_seconds"] == 3000 and s1["entry_path"] == "/home" and s1["exit_path"] == "/pricing"


async def test_sessions_use_configurable_timeout(owner, ingest):
    c, pid = owner["client"], owner["project_id"]
    await seed(ingest, owner["api_key"], [ev("e", "t1", D + "10:00:00"), ev("e", "t1", D + "10:20:00")])
    await c.patch(f"/api/v1/projects/{pid}", json={"settings": {"session_timeout_minutes": 10}})
    r = await analytics(c, "sessions", pid, date_range=RANGE)
    assert r["summary"]["sessions"] == 2


async def test_paths(owner, ingest):
    E = []
    for u in ("p1", "p2"):
        E += [ev("home", u, D + "10:00:00"), ev("pricing", u, D + "10:01:00"), ev("signup", u, D + "10:02:00")]
    E += [ev("home", "p3", D + "10:00:00"), ev("docs", "p3", D + "10:01:00")]
    await seed(ingest, owner["api_key"], E)
    r = await analytics(owner["client"], "paths", owner["project_id"], date_range=RANGE, depth=3)
    edges = {(e["step"], e["source"], e["target"]): e["count"] for e in r["edges"]}
    assert edges[(1, "home", "pricing")] == 2 and edges[(1, "home", "docs")] == 1 and edges[(2, "pricing", "signup")] == 2
    r = await analytics(owner["client"], "paths", owner["project_id"], date_range=RANGE, start_event="pricing", depth=3)
    assert {(e["step"], e["source"], e["target"]): e["count"] for e in r["edges"]} == {(1, "pricing", "signup"): 2}
