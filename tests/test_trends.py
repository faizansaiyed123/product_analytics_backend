import pytest

from tests.analytics_helpers import analytics, ev, seed

RANGE = {"from": "2026-08-03", "to": "2026-08-09"}


@pytest.fixture
async def data(owner, ingest):
    events = [
        # Mon 08-03: 3 purchases by u1 (2) and u2 (1)
        ev("purchase", "u1", "2026-08-03T10:00:00", amount=10, plan="pro", _set={"plan": "pro", "country_group": "A"}),
        ev("purchase", "u1", "2026-08-03T11:00:00", amount=30, plan="pro"),
        ev("purchase", "u2", "2026-08-03T12:00:00", amount=20, plan="free", _set={"plan": "free"}),
        ev("login", "u2", "2026-08-03T12:05:00"),
        # Tue 08-04
        ev("purchase", "u1", "2026-08-04T09:00:00", amount="not-a-number", plan="pro"),
        ev("purchase", "u3", "2026-08-04T09:30:00", amount=100, plan="pro", _set={"plan": "pro"}),
        # Wed 08-05: nothing. Thu 08-06
        ev("purchase", "u2", "2026-08-06T23:30:00", amount=5, plan="free"),
        # outside the range
        ev("purchase", "u1", "2026-08-12T10:00:00", amount=999, plan="pro"),
        ev("purchase", "u1", "2026-07-31T10:00:00", amount=1, plan="pro"),
    ]
    await seed(ingest, owner["api_key"], events)
    return owner


async def test_total_events_per_day_fills_gaps(data):
    r = await analytics(data["client"], "trends", data["project_id"], event="purchase", metric="total_events", date_range=RANGE, interval="day")
    assert r["labels"] == [f"2026-08-0{d}" for d in range(3, 10)]
    assert r["series"][0]["data"] == [3, 2, 0, 1, 0, 0, 0]
    assert r["series"][0]["total"] == 6


async def test_unique_users_and_avg_per_user(data):
    c, pid = data["client"], data["project_id"]
    r = await analytics(c, "trends", pid, event="purchase", metric="unique_users", date_range=RANGE)
    assert r["series"][0]["data"][:4] == [2, 2, 0, 1] and r["series"][0]["total"] == 3  # total is distinct over the range
    r = await analytics(c, "trends", pid, event="purchase", metric="avg_events_per_user", date_range=RANGE)
    assert r["series"][0]["data"][:2] == [1.5, 1.0]


async def test_value_aggregations_ignore_non_numeric(data):
    c, pid = data["client"], data["project_id"]
    expected = {"sum": 165, "avg": 33, "min": 5, "max": 100, "median": 20}
    for metric, value in expected.items():
        r = await analytics(c, "trends", pid, event="purchase", metric=metric, metric_property="amount", date_range=RANGE)
        assert r["series"][0]["total"] == pytest.approx(value), metric
    r = await analytics(c, "trends", pid, event="purchase", metric="sum", metric_property="amount", date_range=RANGE)
    assert r["series"][0]["data"][:4] == [60, 100, 0, 5]


async def test_property_filters(data):
    c, pid = data["client"], data["project_id"]

    async def total(filters):
        return (await analytics(c, "trends", pid, event="purchase", date_range=RANGE, filters=filters))["series"][0]["total"]

    assert await total([{"property": "plan", "operator": "equals", "value": "pro"}]) == 4
    assert await total([{"property": "plan", "operator": "not_equals", "value": "pro"}]) == 2
    assert await total([{"property": "amount", "operator": "gt", "value": 15}]) == 3
    assert await total([{"property": "amount", "operator": "lte", "value": 20}]) == 3
    assert await total([{"property": "amount", "operator": "is_set"}]) == 6
    assert await total([{"property": "nope", "operator": "is_not_set"}]) == 6
    assert await total([{"property": "plan", "operator": "in", "value": ["free", "enterprise"]}]) == 2
    assert await total([{"property": "plan", "operator": "contains", "value": "PR"}]) == 4
    assert await total([{"property": "plan", "operator": "equals", "value": "pro"},
                        {"property": "amount", "operator": "gt", "value": 15}]) == 2  # AND semantics
    # person property filter
    assert await total([{"type": "person", "property": "plan", "operator": "equals", "value": "free"}]) == 2


async def test_like_wildcards_are_escaped(data):
    r = await analytics(data["client"], "trends", data["project_id"], event="purchase", date_range=RANGE,
                        filters=[{"property": "plan", "operator": "contains", "value": "%"}])
    assert r["series"][0]["total"] == 0


async def test_breakdown_by_event_and_person_property(data):
    c, pid = data["client"], data["project_id"]
    r = await analytics(c, "trends", pid, event="purchase", date_range=RANGE, breakdown={"type": "event", "property": "plan"})
    by = {s["breakdown_value"]: s["total"] for s in r["series"]}
    assert by == {"pro": 4, "free": 2}
    r = await analytics(c, "trends", pid, event="purchase", date_range=RANGE, metric="unique_users",
                        breakdown={"type": "person", "property": "plan"})
    assert {s["breakdown_value"]: s["total"] for s in r["series"]} == {"pro": 2, "free": 1}


async def test_timezone_changes_bucket_boundaries(data):
    c, pid = data["client"], data["project_id"]
    utc = await analytics(c, "trends", pid, event="purchase", date_range=RANGE, timezone="UTC")
    ist = await analytics(c, "trends", pid, event="purchase", date_range=RANGE, timezone="Asia/Kolkata")
    assert utc["series"][0]["data"][3] == 1 and utc["series"][0]["data"][4] == 0
    # 23:30 UTC on Thu is 05:00 IST on Fri
    assert ist["series"][0]["data"][3] == 0 and ist["series"][0]["data"][4] == 1
    assert ist["timezone"] == "Asia/Kolkata"


async def test_week_month_hour_intervals_and_multiple_series(data):
    c, pid = data["client"], data["project_id"]
    w = await analytics(c, "trends", pid, event="purchase", date_range={"from": "2026-07-27", "to": "2026-08-16"}, interval="week")
    assert w["labels"] == ["2026-07-27", "2026-08-03", "2026-08-10"]
    assert w["series"][0]["data"] == [1, 6, 1]
    m = await analytics(c, "trends", pid, event="purchase", date_range={"from": "2026-07-01", "to": "2026-08-31"}, interval="month")
    assert m["labels"] == ["2026-07-01", "2026-08-01"] and m["series"][0]["data"] == [1, 7]
    h = await analytics(c, "trends", pid, event="purchase", date_range={"from": "2026-08-03", "to": "2026-08-03"}, interval="hour")
    assert len(h["labels"]) == 24 and h["series"][0]["data"][10] == 1 and h["series"][0]["data"][11] == 1
    s = await analytics(c, "trends", pid, series=[{"event": "purchase"}, {"event": "login"}, {"event": "*"}], date_range=RANGE)
    assert [x["total"] for x in s["series"]] == [6, 1, 7]


async def test_frequency_distribution(data):
    r = await analytics(data["client"], "trends", data["project_id"], event="purchase", metric="frequency", date_range=RANGE)
    assert r["kind"] == "distribution" and r["labels"][:2] == ["1", "2"]
    assert r["series"][0]["data"][:3] == [1, 1, 1]  # u3 once, u2 twice, u1 three times


async def test_compare_previous_period(data):
    r = await analytics(data["client"], "trends", data["project_id"], event="purchase", date_range=RANGE, compare_previous=True)
    assert r["previous"]["series"][0]["total"] == 1  # 07-27..08-02 contains only the 07-31 purchase
    assert r["previous"]["labels"][0] == "2026-07-27"


async def test_invalid_queries_return_422(data):
    c, pid = data["client"], data["project_id"]
    for body in [
        {"event": "x", "metric": "sum"},  # needs metric_property
        {"event": "x", "filters": [{"property": "a", "operator": "gt", "value": "abc"}]},
        {"event": "x", "date_range": {"from": "2026-09-01", "to": "2026-08-01"}},
        {"event": "x", "date_range": {"from": "garbage"}},
        {"event": "x", "timezone": "Mars/Phobos"},
        {},
    ]:
        r = await c.post("/api/v1/analytics/trends", json={"project_id": pid, **body})
        assert r.status_code == 422, (body, r.text)
        assert r.json()["error"]["code"] in ("VALIDATION_ERROR", "INVALID_DATE_RANGE", "INVALID_TIMEZONE")


async def test_sql_injection_attempts_are_inert(data):
    c, pid = data["client"], data["project_id"]
    evil = "x'); DROP TABLE events; --"
    r = await analytics(c, "trends", pid, event=evil, date_range=RANGE,
                        filters=[{"property": evil, "operator": "equals", "value": evil}],
                        breakdown={"type": "event", "property": evil})
    assert r["series"] == [] or r["series"][0]["total"] == 0
    assert (await analytics(c, "trends", pid, event="purchase", date_range=RANGE))["series"][0]["total"] == 6


async def test_query_results_are_cached_and_refreshable(data):
    c, pid = data["client"], data["project_id"]
    body = {"project_id": pid, "event": "purchase", "date_range": RANGE}
    first = (await c.post("/api/v1/analytics/trends", json=body)).json()
    second = (await c.post("/api/v1/analytics/trends", json=body)).json()
    fresh = (await c.post("/api/v1/analytics/trends", json={**body, "refresh": True})).json()
    assert first["cached"] is False and second["cached"] is True and fresh["cached"] is False
    assert first["result"] == second["result"]
