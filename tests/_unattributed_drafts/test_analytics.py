import pytest

from tests.helpers import RANGE, ev, query, seed


# ------------------------------------------------------------------------------------------------- trends
@pytest.fixture
async def trend_data(owner, ingest):
    events = [
        ev("a", "purchase", "2026-08-01T10:00:00Z", amount=10, plan="pro", **{"$set": {"tier": "gold"}}),
        ev("a", "purchase", "2026-08-01T11:00:00Z", amount=30, plan="pro"),
        ev("b", "purchase", "2026-08-01T12:00:00Z", amount=50, plan="free", **{"$set": {"tier": "silver"}}),
        ev("a", "purchase", "2026-08-03T09:00:00Z", amount=100, plan="pro"),
        ev("c", "purchase", "2026-08-03T09:30:00Z", amount="n/a", plan="pro"),  # non-numeric must not break sums
        ev("b", "login", "2026-08-02T08:00:00Z"),
        ev("b", "login", "2026-08-02T09:00:00Z"),
        ev("b", "login", "2026-08-02T10:00:00Z"),
    ]
    await seed(ingest, owner["api_key"], events)
    return owner


async def test_trend_total_and_unique_users_fill_missing_days(trend_data):
    base = {
        "type": "trend",
        "event": "purchase",
        "date_range": {"from": "2026-08-01", "to": "2026-08-04"},
        "interval": "day",
    }
    r = await query(trend_data, "trends", {**base, "metric": "total_events"})
    assert r["labels"] == ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]
    assert r["series"][0]["data"] == [3, 0, 2, 0] and r["series"][0]["total"] == 5
    r = await query(trend_data, "trends", {**base, "metric": "unique_users"})
    assert r["series"][0]["data"] == [2, 0, 2, 0] and r["series"][0]["total"] == 3  # total = distinct users overall
    r = await query(trend_data, "trends", {**base, "metric": "avg_events_per_user"})
    assert r["series"][0]["data"] == [1.5, 0, 1.0, 0]


async def test_trend_property_aggregations_ignore_non_numeric(trend_data):
    base = {"type": "trend", "event": "purchase", "date_range": RANGE, "interval": "week", "metric_property": "amount"}
    for metric, expected in {"sum": 190, "avg": 47.5, "min": 10, "max": 100, "median": 40}.items():
        r = await query(trend_data, "trends", {**base, "metric": metric})
        assert r["series"][0]["total"] == pytest.approx(expected), metric


async def test_trend_filters(trend_data):
    base = {"type": "trend", "event": "purchase", "date_range": RANGE, "interval": "day", "metric": "total_events"}

    async def total(*filters):
        return (await query(trend_data, "trends", {**base, "filters": list(filters)}))["series"][0]["total"]

    assert await total({"property": "plan", "operator": "equals", "value": "pro"}) == 4
    assert await total({"property": "plan", "operator": "not_equals", "value": "pro"}) == 1
    assert await total({"property": "amount", "operator": "gt", "value": 20}) == 3  # 30, 50, 100 ("n/a" excluded)
    assert await total({"property": "amount", "operator": "lte", "value": 30}) == 2
    assert await total({"property": "plan", "operator": "in", "value": ["free", "pro"]}) == 5
    assert await total({"property": "plan", "operator": "contains", "value": "PR"}) == 4
    assert await total({"property": "missing", "operator": "is_not_set"}) == 5
    assert await total({"property": "amount", "operator": "is_set"}) == 5
    assert (
        await total(
            {"type": "person", "property": "tier", "operator": "equals", "value": "gold"},
            {"property": "plan", "operator": "equals", "value": "pro"},
        )
        == 3
    )  # all of user a's events


async def test_trend_breakdown_and_multiple_series_and_all_events(trend_data):
    r = await query(
        trend_data,
        "trends",
        {
            "type": "trend",
            "event": "purchase",
            "date_range": RANGE,
            "interval": "month",
            "metric": "total_events",
            "breakdown": {"type": "event", "property": "plan"},
        },
    )
    by = {s["breakdown_value"]: s["total"] for s in r["series"]}
    assert by == {"pro": 4, "free": 1}
    r = await query(
        trend_data,
        "trends",
        {
            "type": "trend",
            "event": "purchase",
            "date_range": RANGE,
            "interval": "month",
            "breakdown": {"type": "person", "property": "tier"},
        },
    )
    assert {s["breakdown_value"]: s["total"] for s in r["series"]} == {"gold": 3, "silver": 1, "(none)": 1}
    r = await query(
        trend_data,
        "trends",
        {
            "type": "trend",
            "series": [{"event": "purchase"}, {"event": "login"}, {"event": "*"}],
            "date_range": RANGE,
            "interval": "month",
        },
    )
    assert [s["total"] for s in r["series"]] == [5, 3, 8]


async def test_trend_frequency_distribution(trend_data):
    r = await query(
        trend_data, "trends", {"type": "trend", "event": "login", "metric": "frequency", "date_range": RANGE}
    )
    assert r["kind"] == "distribution" and r["labels"][:3] == ["1", "2", "3"]
    assert r["series"][0]["data"][2] == 1 and sum(r["series"][0]["data"]) == 1  # one user logged in exactly 3 times


async def test_trend_compare_previous_period(trend_data):
    r = await query(
        trend_data,
        "trends",
        {
            "type": "trend",
            "event": "purchase",
            "metric": "total_events",
            "interval": "day",
            "date_range": {"from": "2026-08-03", "to": "2026-08-04"},
            "compare_previous": True,
        },
    )
    assert r["series"][0]["total"] == 2 and r["previous"]["series"][0]["total"] == 3  # Aug 1-2 vs Aug 3-4
    assert r["previous"]["date_to"] == r["date_from"]


async def test_trend_timezone_bucketing(owner, ingest):
    await seed(ingest, owner["api_key"], [ev("u", "e", "2026-08-01T20:00:00Z"), ev("u", "e", "2026-08-02T01:00:00Z")])
    q = {"type": "trend", "event": "e", "interval": "day", "date_range": {"from": "2026-08-01", "to": "2026-08-02"}}
    utc = await query(owner, "trends", {**q, "timezone": "UTC"})
    ist = await query(owner, "trends", {**q, "timezone": "Asia/Kolkata"})  # UTC+5:30 -> both events fall on Aug 2
    assert utc["series"][0]["data"] == [1, 1]
    assert ist["series"][0]["data"] == [0, 2]


async def test_query_validation_errors(owner):
    c = owner["client"]
    pid = owner["project_id"]
    r = await c.post("/api/v1/analytics/trends", json={"project_id": pid, "query": {"type": "trend"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = await c.post(
        "/api/v1/analytics/trends",
        json={"project_id": pid, "query": {"type": "trend", "event": "x", "date_range": {"from": "garbage"}}},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE_RANGE"
    r = await c.post(
        "/api/v1/analytics/trends",
        json={"project_id": pid, "query": {"type": "trend", "event": "x", "timezone": "Mars/Base"}},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_TIMEZONE"
    r = await c.post(
        "/api/v1/analytics/trends", json={"project_id": pid, "query": {"type": "trend", "event": "x", "metric": "sum"}}
    )
    assert r.status_code == 422
    r = await c.post(
        "/api/v1/analytics/trends",
        json={
            "project_id": pid,
            "query": {
                "type": "trend",
                "event": "x",
                "interval": "hour",
                "date_range": {"from": "2020-01-01", "to": "2026-01-01"},
            },
        },
    )
    assert r.status_code == 422  # range too large / too many intervals
    # SQL-injection-looking values are plain data
    r = await c.post(
        "/api/v1/analytics/trends",
        json={
            "project_id": pid,
            "query": {
                "type": "trend",
                "event": "x'; DROP TABLE events;--",
                "filters": [{"property": "a'b", "operator": "equals", "value": "'; DROP TABLE events;--"}],
            },
        },
    )
    assert r.status_code == 200
    assert (await c.get("/api/v1/projects")).status_code == 200


async def test_query_cache_and_refresh(trend_data, ingest):
    q = {"type": "trend", "event": "purchase", "date_range": RANGE, "interval": "month"}
    c, pid = trend_data["client"], trend_data["project_id"]
    first = (await c.post("/api/v1/analytics/trends", json={"project_id": pid, "query": q})).json()
    assert first["cached"] is False
    second = (await c.post("/api/v1/analytics/trends", json={"project_id": pid, "query": q})).json()
    assert second["cached"] is True and second["computed_at"] == first["computed_at"]
    await seed(ingest, trend_data["api_key"], [ev("z", "purchase", "2026-08-10T10:00:00Z", amount=1)])
    stale = (await c.post("/api/v1/analytics/trends", json={"project_id": pid, "query": q})).json()
    assert stale["result"]["series"][0]["total"] == 5  # documented TTL staleness
    fresh = (await c.post("/api/v1/analytics/trends", json={"project_id": pid, "query": q, "refresh": True})).json()
    assert fresh["result"]["series"][0]["total"] == 6 and fresh["cached"] is False


# ------------------------------------------------------------------------------------------------- funnels
@pytest.fixture
async def funnel_data(owner, ingest):
    events = [
        # A: complete, 600s then 3000s
        ev("A", "signup_started", "2026-08-01T10:00:00Z", plan="pro"),
        ev("A", "signup_completed", "2026-08-01T10:10:00Z"),
        ev("A", "purchase", "2026-08-01T11:00:00Z"),
        # B: stops after step 2 (1200s)
        ev("B", "signup_started", "2026-08-01T10:00:00Z", plan="free"),
        ev("B", "signup_completed", "2026-08-01T10:20:00Z"),
        # C: only step 1
        ev("C", "signup_started", "2026-08-02T10:00:00Z", plan="pro"),
        # D: purchase BEFORE signup: out of order, must not count past step 1
        ev("D", "purchase", "2026-08-01T08:00:00Z"),
        ev("D", "signup_started", "2026-08-01T09:00:00Z"),
        # E: completes after 3 days -> outside a 1-day window
        ev("E", "signup_started", "2026-08-01T10:00:00Z"),
        ev("E", "signup_completed", "2026-08-04T10:00:00Z"),
        # F: first attempt fails, second attempt succeeds (best chain wins, user counted once)
        ev("F", "signup_started", "2026-08-01T10:00:00Z"),
        ev("F", "signup_started", "2026-08-05T10:00:00Z"),
        ev("F", "signup_completed", "2026-08-05T10:05:00Z"),
        ev("F", "purchase", "2026-08-05T10:15:00Z"),
    ]
    await seed(ingest, owner["api_key"], events)
    return owner


STEPS = [{"event": "signup_started"}, {"event": "signup_completed"}, {"event": "purchase"}]


async def test_funnel_counts_order_window_and_best_attempt(funnel_data):
    r = await query(
        funnel_data,
        "funnels",
        {"type": "funnel", "steps": STEPS, "date_range": RANGE, "conversion_window": {"value": 1, "unit": "day"}},
    )
    steps = r["steps"]
    assert [s["count"] for s in steps] == [6, 3, 2]
    assert steps[1]["conversion_rate"] == pytest.approx(0.5) and steps[2]["conversion_rate"] == pytest.approx(2 / 3)
    assert steps[1]["dropoff_count"] == 3 and steps[2]["dropoff_count"] == 1
    assert steps[2]["overall_conversion_rate"] == pytest.approx(1 / 3) and r[
        "overall_conversion_rate"
    ] == pytest.approx(1 / 3)
    assert steps[1]["avg_time_to_convert_seconds"] == pytest.approx((600 + 1200 + 300) / 3)
    assert steps[1]["median_time_to_convert_seconds"] == pytest.approx(600)
    assert steps[2]["avg_time_to_convert_seconds"] == pytest.approx((3000 + 600) / 2)


async def test_funnel_longer_window_lets_slow_converters_through(funnel_data):
    r = await query(
        funnel_data,
        "funnels",
        {"type": "funnel", "steps": STEPS[:2], "date_range": RANGE, "conversion_window": {"value": 7, "unit": "day"}},
    )
    assert [s["count"] for s in r["steps"]] == [6, 4]  # E now converts too
    r = await query(
        funnel_data,
        "funnels",
        {
            "type": "funnel",
            "steps": STEPS[:2],
            "date_range": {"from": "2026-08-01", "to": "2026-08-01"},
            "conversion_window": {"value": 7, "unit": "day"},
        },
    )
    assert [s["count"] for s in r["steps"]] == [
        5,
        4,
    ]  # entries on Aug 1: A, B, D, E, F (C enters Aug 2); D never completes


async def test_funnel_entry_must_be_inside_date_range(funnel_data):
    r = await query(
        funnel_data,
        "funnels",
        {
            "type": "funnel",
            "steps": STEPS,
            "date_range": {"from": "2026-08-02", "to": "2026-08-31"},
            "conversion_window": {"value": 1, "unit": "day"},
        },
    )
    # entries from Aug 2: C (Aug 2) and F's SECOND attempt (Aug 5); everyone else entered on Aug 1 and is excluded
    assert [s["count"] for s in r["steps"]] == [2, 1, 1]


async def test_funnel_breakdown_and_filters(funnel_data):
    r = await query(
        funnel_data,
        "funnels",
        {
            "type": "funnel",
            "steps": STEPS[:2],
            "date_range": RANGE,
            "conversion_window": {"value": 1, "unit": "day"},
            "breakdown": {"type": "event", "property": "plan"},
        },
    )
    by = {b["value"]: [s["count"] for s in b["steps"]] for b in r["breakdowns"]}
    assert by["pro"] == [2, 1] and by["free"] == [1, 1] and by["(none)"] == [3, 1]
    r = await query(
        funnel_data,
        "funnels",
        {
            "type": "funnel",
            "steps": [
                {"event": "signup_started", "filters": [{"property": "plan", "operator": "equals", "value": "pro"}]},
                {"event": "signup_completed"},
            ],
            "date_range": RANGE,
            "conversion_window": {"value": 1, "unit": "day"},
        },
    )
    assert [s["count"] for s in r["steps"]] == [2, 1]


async def test_funnel_same_event_twice_needs_two_occurrences(owner, ingest):
    await seed(
        ingest,
        owner["api_key"],
        [
            ev("x", "view", "2026-08-01T10:00:00Z"),
            ev("y", "view", "2026-08-01T10:00:00Z"),
            ev("y", "view", "2026-08-01T10:05:00Z"),
        ],
    )
    r = await query(
        owner, "funnels", {"type": "funnel", "steps": [{"event": "view"}, {"event": "view"}], "date_range": RANGE}
    )
    assert [s["count"] for s in r["steps"]] == [2, 1]


async def test_funnel_uses_identity_merged_users(owner, ingest):
    key = owner["api_key"]
    await seed(
        ingest,
        key,
        [
            ev("anon-9", "signup_started", "2026-08-01T10:00:00Z"),
            {
                "event": "$identify",
                "distinct_id": "user-9",
                "anonymous_id": "anon-9",
                "timestamp": "2026-08-01T10:05:00Z",
            },
            ev("user-9", "purchase", "2026-08-01T10:30:00Z"),
        ],
    )
    r = await query(
        owner,
        "funnels",
        {"type": "funnel", "steps": [{"event": "signup_started"}, {"event": "purchase"}], "date_range": RANGE},
    )
    assert [s["count"] for s in r["steps"]] == [1, 1]  # anonymous + identified activity is ONE person


# ------------------------------------------------------------------------------------------------- retention
@pytest.fixture
async def retention_data(owner, ingest):
    await seed(
        ingest,
        owner["api_key"],
        [
            ev("U1", "app_open", "2026-08-01T10:00:00Z"),
            ev("U1", "app_open", "2026-08-02T10:00:00Z"),
            ev("U1", "app_open", "2026-08-08T10:00:00Z"),
            ev("U2", "app_open", "2026-08-01T11:00:00Z"),
            ev("U2", "app_open", "2026-08-02T11:00:00Z"),
            ev("U3", "app_open", "2026-08-02T12:00:00Z"),
            ev("U3", "app_open", "2026-08-03T12:00:00Z"),
            ev("U4", "app_open", "2026-08-01T13:00:00Z"),
        ],
    )
    return owner


async def test_day_retention_matrix(retention_data):
    r = await query(
        retention_data,
        "retention",
        {
            "type": "retention",
            "interval": "day",
            "offsets": [0, 1, 2, 7],
            "initial_event": {"event": "app_open"},
            "return_event": {"event": "app_open"},
            "date_range": {"from": "2026-08-01", "to": "2026-08-02"},
        },
    )
    c = {x["period"]: x for x in r["cohorts"]}
    assert set(c) == {"2026-08-01", "2026-08-02"}
    assert c["2026-08-01"]["size"] == 3 and c["2026-08-02"]["size"] == 1  # first-time cohorts: U1,U2,U4 / U3
    cells = {x["offset"]: (x["count"], x["rate"]) for x in c["2026-08-01"]["cells"]}
    assert (
        cells[0] == (3, 1.0)
        and cells[1] == (2, pytest.approx(2 / 3))
        and cells[2] == (0, 0.0)
        and cells[7] == (1, pytest.approx(1 / 3))
    )
    cells2 = {x["offset"]: x["count"] for x in c["2026-08-02"]["cells"]}
    assert cells2 == {0: 1, 1: 1, 2: 0, 7: 0}
    avg = {x["offset"]: x["rate"] for x in r["average"]}
    assert avg[1] == pytest.approx(3 / 4) and avg[0] == 1.0


async def test_retention_future_periods_are_null_not_zero(owner, ingest):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    await seed(ingest, owner["api_key"], [ev("N1", "open", (now - timedelta(hours=1)).isoformat())])
    r = await query(
        owner,
        "retention",
        {"type": "retention", "interval": "day", "offsets": [0, 1, 7], "date_range": {"preset": "last_7_days"}},
    )
    cells = {x["offset"]: x for x in r["cohorts"][-1]["cells"]}
    assert cells[0]["count"] == 1 and cells[1]["count"] is None and cells[7]["rate"] is None


async def test_retention_different_return_event_and_weekly_and_recurring(retention_data, ingest):
    await seed(
        ingest,
        retention_data["api_key"],
        [ev("U1", "purchase", "2026-08-09T10:00:00Z"), ev("U2", "purchase", "2026-08-02T10:00:00Z")],
    )
    r = await query(
        retention_data,
        "retention",
        {
            "type": "retention",
            "interval": "week",
            "offsets": [0, 1],
            "initial_event": {"event": "app_open"},
            "return_event": {"event": "purchase"},
            "date_range": {"from": "2026-07-27", "to": "2026-08-09"},
        },
    )
    # ISO weeks start Monday: Jul 27 (U1,U2,U4 first seen Aug 1 + U3 Aug 2 -> all in week of Jul 27)
    wk = {x["period"]: x for x in r["cohorts"]}["2026-07-27"]
    assert wk["size"] == 4
    assert {c["offset"]: c["count"] for c in wk["cells"]} == {0: 1, 1: 1}  # U2 purchased in week 0, U1 in week 1
    rec = await query(
        retention_data,
        "retention",
        {
            "type": "retention",
            "interval": "day",
            "offsets": [0, 1],
            "cohort_type": "recurring",
            "initial_event": {"event": "app_open"},
            "return_event": {"event": "app_open"},
            "date_range": {"from": "2026-08-02", "to": "2026-08-02"},
        },
    )
    assert rec["cohorts"][0]["size"] == 3  # U1, U2, U3 were active on Aug 2 (recurring cohort)


async def test_retention_user_filters(retention_data):
    r = await query(
        retention_data,
        "retention",
        {
            "type": "retention",
            "interval": "day",
            "offsets": [0],
            "date_range": {"from": "2026-08-01", "to": "2026-08-01"},
            "initial_event": {
                "event": "app_open",
                "filters": [{"property": "nope", "operator": "equals", "value": "x"}],
            },
        },
    )
    assert r["cohorts"] == []


# ------------------------------------------------------------------------------------------------- sessions & paths
async def test_sessions_use_inactivity_timeout(owner, ingest):
    await seed(
        ingest,
        owner["api_key"],
        [
            ev("S1", "$pageview", "2026-08-01T10:00:00Z", path="/a"),
            ev("S1", "$pageview", "2026-08-01T10:10:00Z", path="/b"),
            ev("S1", "click", "2026-08-01T10:25:00Z"),
            ev("S1", "$pageview", "2026-08-01T11:30:00Z", path="/a"),
            ev("S1", "$pageview", "2026-08-01T11:40:00Z", path="/c"),  # 65 min gap
            ev("S2", "$pageview", "2026-08-01T12:00:00Z", path="/a"),
        ],
    )
    r = await query(
        owner,
        "sessions",
        {"type": "sessions", "date_range": {"from": "2026-08-01", "to": "2026-08-01"}, "interval": "day"},
    )
    s = r["summary"]
    assert s["sessions"] == 3 and s["unique_users"] == 2
    assert s["avg_duration_seconds"] == pytest.approx((1500 + 600 + 0) / 3)
    assert s["avg_events_per_session"] == pytest.approx(2.0) and s["avg_pages_per_session"] == pytest.approx(
        (2 + 2 + 1) / 3
    )
    assert s["bounce_rate"] == pytest.approx(1 / 3)
    assert r["sessions_series"] == [3]
    # project-level timeout is configurable
    c, pid = owner["client"], owner["project_id"]
    await c.patch(f"/api/v1/projects/{pid}", json={"settings": {"session_timeout_minutes": 120}})
    r = await query(
        owner, "sessions", {"type": "sessions", "date_range": {"from": "2026-08-01", "to": "2026-08-01"}}, refresh=True
    )
    assert r["summary"]["sessions"] == 2
    lst = (
        await c.post(
            "/api/v1/analytics/sessions/list",
            json={"project_id": pid, "date_range": {"from": "2026-08-01", "to": "2026-08-01"}},
        )
    ).json()["items"]
    assert len(lst) == 2 and lst[0]["distinct_id"] == "S2" and lst[1]["events"] == 5 and lst[1]["entry_path"] == "/a"


async def test_session_started_before_range_is_not_double_counted(owner, ingest):
    await seed(
        ingest, owner["api_key"], [ev("L", "x", "2026-08-01T23:50:00Z"), ev("L", "x", "2026-08-02T00:10:00Z")]
    )  # one session across midnight
    r = await query(owner, "sessions", {"type": "sessions", "date_range": {"from": "2026-08-02", "to": "2026-08-02"}})
    assert r["summary"]["sessions"] == 0  # it belongs to Aug 1 (start day)
    r = await query(owner, "sessions", {"type": "sessions", "date_range": {"from": "2026-08-01", "to": "2026-08-01"}})
    assert r["summary"]["sessions"] == 1


async def test_paths(owner, ingest):
    evs = []
    for who, seq in {"P1": ["A", "B", "C"], "P2": ["A", "B", "D"], "P3": ["A", "C"]}.items():
        for i, name in enumerate(seq):
            evs.append(ev(who, name, f"2026-08-01T10:0{i}:00Z"))
    await seed(ingest, owner["api_key"], evs)
    r = await query(owner, "paths", {"type": "paths", "date_range": RANGE, "depth": 4})
    edges = {(e["step"], e["source"], e["target"]): e["count"] for e in r["edges"]}
    assert edges == {(1, "A", "B"): 2, (1, "A", "C"): 1, (2, "B", "C"): 1, (2, "B", "D"): 1} and r["sessions"] == 3
    r = await query(owner, "paths", {"type": "paths", "date_range": RANGE, "start_event": "B"})
    assert {(e["step"], e["source"], e["target"]): e["count"] for e in r["edges"]} == {
        (1, "B", "C"): 1,
        (1, "B", "D"): 1,
    }
