from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db.session import get_sessionmaker
from app.models import Event, EventDefinition, Person, PersonDistinctId, PropertyDefinition


async def _events(**where):
    async with get_sessionmaker()() as s:
        q = select(Event).order_by(Event.timestamp, Event.event)
        return (await s.execute(q)).scalars().all()


async def _count(model):
    async with get_sessionmaker()() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_single_event_ingestion_stores_all_field_groups(owner, ingest):
    body = {
        "event": "checkout_completed",
        "distinct_id": "user_123",
        "anonymous_id": "anon_456",
        "timestamp": "2026-09-29T12:30:00Z",
        "uuid": "evt-1",
        "properties": {
            "amount": 99.99,
            "currency": "USD",
            "plan": "pro",
            "device": "mobile",
            "$current_url": "https://shop.example.com/checkout?x=1",
            "$set": {"plan": "pro"},
        },
        "context": {
            "ip": "203.0.113.77",
            "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "country": "in",
        },
    }
    r = await ingest.client.post("/api/v1/events", json=body, headers={"Authorization": f"Bearer {owner['api_key']}"})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1
    (e,) = await _events()
    assert e.event == "checkout_completed" and e.distinct_id == "user_123" and e.anonymous_id == "anon_456"
    assert e.timestamp == datetime(2026, 9, 29, 12, 30, tzinfo=UTC)
    assert e.properties["amount"] == 99.99 and "$set" not in e.properties
    assert e.path == "/checkout" and e.hostname == "shop.example.com"
    assert (e.browser, e.os, e.device_type, e.country) == ("Chrome", "Linux", "desktop", "IN")
    assert str(e.ip) == "203.0.113.0"  # anonymised by default (last octet zeroed)
    async with get_sessionmaker()() as s:
        person = (await s.execute(select(Person))).scalars().one()
        assert person.properties == {"plan": "pro"} and person.id == e.person_id
        assert "amount" in {p.name for p in (await s.execute(select(PropertyDefinition))).scalars()}
        assert [d.name for d in (await s.execute(select(EventDefinition))).scalars()] == ["checkout_completed"]


async def test_batch_with_bad_events_is_partially_accepted(owner, ingest):
    events = [
        {"event": "ok", "distinct_id": "u1"},
        {"distinct_id": "u1"},  # missing event
        {"event": "x"},  # missing distinct id
        {"event": "x", "distinct_id": "undefined"},
        {"event": "x", "distinct_id": "u1", "timestamp": "not-a-date"},
        {"event": "x", "distinct_id": "u1", "properties": "nope"},
        "a string",
        None,
        {
            "event": "fine",
            "distinct_id": "u2",
            "properties": {"nested": {"a": [1, 2, {"b": None}]}, "n": float("nan") if False else 1},
        },
    ]
    r = await ingest(owner["api_key"], events)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["accepted"] == 2 and len(j["rejected"]) == 7
    assert {x["code"] for x in j["rejected"]} >= {
        "INVALID_EVENT",
        "MISSING_DISTINCT_ID",
        "INVALID_DISTINCT_ID",
        "INVALID_TIMESTAMP",
        "INVALID_PROPERTIES",
    }
    assert await _count(Event) == 2


async def test_all_invalid_returns_400_with_error_shape(owner, ingest):
    r = await ingest.client.post(
        "/api/v1/events", json={"distinct_id": "u"}, headers={"Authorization": f"Bearer {owner['api_key']}"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_EVENT" and r.json()["error"]["message"] == "Event name is required"


async def test_malformed_requests(owner, ingest):
    h = {"Authorization": f"Bearer {owner['api_key']}", "Content-Type": "application/json"}
    r = await ingest.client.post("/api/v1/events/batch", content=b"{not json", headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_JSON"
    assert (await ingest(owner["api_key"], [])).json()["error"]["code"] == "EMPTY_BATCH"
    r = await ingest(owner["api_key"], [{"event": "e", "distinct_id": "u"}] * 501)
    assert r.status_code == 413 and r.json()["error"]["code"] == "BATCH_TOO_LARGE"
    r = await ingest.client.post("/api/v1/events/batch", json={"events": "x"}, headers=h)
    assert r.status_code == 400


async def test_api_key_required_and_accepted_in_several_places(owner, ingest):
    c, key = ingest.client, owner["api_key"]
    ev = {"event": "e", "distinct_id": "u"}
    assert (await c.post("/api/v1/events", json=ev)).json()["error"]["code"] == "MISSING_API_KEY"
    assert (await c.post("/api/v1/events", json=ev, headers={"X-API-Key": "phc_nope"})).status_code == 401
    assert (await c.post("/api/v1/events", json=ev, headers={"X-API-Key": key})).status_code == 200
    assert (await c.post(f"/api/v1/events?api_key={key}", json=ev)).status_code == 200
    # sendBeacon style: text/plain body with the key inside
    r = await c.post(
        "/api/v1/events/batch",
        content=('{"api_key":"%s","events":[{"event":"b","distinct_id":"u"}]}' % key),
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 200 and r.json()["accepted"] == 1


async def test_idempotency_key_deduplicates(owner, ingest):
    ev = {"event": "purchase", "distinct_id": "u1", "uuid": "abc-123"}
    assert (await ingest(owner["api_key"], [ev])).json()["accepted"] == 1
    r = (await ingest(owner["api_key"], [ev, {**ev, "uuid": "abc-124"}])).json()
    assert r["accepted"] == 1 and r["duplicates"] == 1
    assert await _count(Event) == 2


async def test_anonymous_to_identified_merge(owner, ingest):
    key = owner["api_key"]
    await ingest(
        key,
        [
            {"event": "page_view", "distinct_id": "anon-1", "timestamp": "2026-09-01T10:00:00Z"},
            {"event": "page_view", "distinct_id": "anon-1", "timestamp": "2026-09-01T10:01:00Z"},
        ],
    )
    assert await _count(Person) == 1
    r = await ingest(
        key,
        [
            {
                "event": "$identify",
                "distinct_id": "user_1",
                "anonymous_id": "anon-1",
                "properties": {"$set": {"email": "u@example.com", "plan": "pro"}},
            }
        ],
    )
    assert r.status_code == 200
    assert await _count(Person) == 1  # anonymous person adopted, not duplicated
    await ingest(key, [{"event": "purchase", "distinct_id": "user_1"}])
    events = await _events()
    assert len({e.person_id for e in events}) == 1  # pre- and post-identify events are one user
    async with get_sessionmaker()() as s:
        p = (await s.execute(select(Person))).scalars().one()
        assert p.is_identified and p.properties["email"] == "u@example.com"
        ids = {m.distinct_id for m in (await s.execute(select(PersonDistinctId))).scalars()}
    assert ids == {"anon-1", "user_1"}


async def test_merge_when_both_ids_already_have_persons(owner, ingest):
    key = owner["api_key"]
    await ingest(
        key,
        [
            {"event": "a", "distinct_id": "anon-2", "properties": {"$set": {"seen": True, "shared": "anon"}}},
            {"event": "b", "distinct_id": "user_2", "properties": {"$set": {"shared": "user"}}},
        ],
    )
    assert await _count(Person) == 2
    await ingest(key, [{"event": "$identify", "distinct_id": "user_2", "anonymous_id": "anon-2"}])
    assert await _count(Person) == 1
    async with get_sessionmaker()() as s:
        p = (await s.execute(select(Person))).scalars().one()
        assert p.properties == {"seen": True, "shared": "user"}  # target wins on conflict, source fills gaps
    assert len({e.person_id for e in await _events()}) == 1  # history moved to the surviving person


async def test_two_identified_users_are_never_merged(owner, ingest):
    key = owner["api_key"]
    await ingest(key, [{"event": "$identify", "distinct_id": "alice"}, {"event": "$identify", "distinct_id": "bob"}])
    await ingest(key, [{"event": "$identify", "distinct_id": "alice", "anonymous_id": "bob"}])  # shared device scenario
    assert await _count(Person) == 2


async def test_alias_links_ids(owner, ingest):
    key = owner["api_key"]
    await ingest(key, [{"event": "x", "distinct_id": "old-id"}])
    await ingest(key, [{"event": "$alias", "distinct_id": "new-id", "properties": {"alias": "old-id"}}])
    assert await _count(Person) == 1


async def test_set_once_and_set_semantics(owner, ingest):
    key = owner["api_key"]
    await ingest(
        key,
        [
            {
                "event": "e",
                "distinct_id": "u",
                "timestamp": "2026-09-01T00:00:00Z",
                "properties": {"$set": {"plan": "free"}, "$set_once": {"first_referrer": "google"}},
            }
        ],
    )
    await ingest(
        key,
        [
            {
                "event": "e",
                "distinct_id": "u",
                "timestamp": "2026-09-02T00:00:00Z",
                "properties": {"$set": {"plan": "pro"}, "$set_once": {"first_referrer": "twitter"}},
            }
        ],
    )
    async with get_sessionmaker()() as s:
        p = (await s.execute(select(Person))).scalars().one()
    assert p.properties == {"plan": "pro", "first_referrer": "google"}


async def test_timestamps_future_clamped_sent_at_skew_and_late_events(owner, ingest):
    key = owner["api_key"]
    now = datetime.now(UTC)
    await ingest(
        key,
        [
            {"event": "future", "distinct_id": "u", "timestamp": (now + timedelta(days=3)).isoformat()},
            {"event": "late", "distinct_id": "u", "timestamp": "2020-01-01T00:00:00Z"},
            {"event": "epoch", "distinct_id": "u", "timestamp": 1_700_000_000},
        ],
    )
    ev = {e.event: e for e in await _events()}
    assert abs((ev["future"].timestamp - now).total_seconds()) < 60  # clamped to receive time
    assert ev["late"].timestamp.year == 2020  # late-arriving events keep their real time
    assert ev["epoch"].timestamp == datetime.fromtimestamp(1_700_000_000, UTC)
    # client clock is 1h slow: sent_at lets the server correct timestamps
    skewed = now - timedelta(hours=1)
    await ingest(
        key,
        [{"event": "skew", "distinct_id": "u2", "timestamp": (skewed - timedelta(minutes=5)).isoformat()}],
        sent_at=skewed.isoformat(),
    )
    skew = next(e for e in await _events() if e.event == "skew")
    assert abs((skew.timestamp - (now - timedelta(minutes=5))).total_seconds()) < 30


async def test_sensitive_properties_are_dropped_and_nul_bytes_stripped(owner, ingest):
    await ingest(
        owner["api_key"],
        [
            {
                "event": "signup",
                "distinct_id": "u",
                "properties": {
                    "password": "hunter2",
                    "Token": "abc",
                    "nested": {"secret": "x", "ok": 1},
                    "note": "a\u0000b",
                    "plan": "pro",
                },
            }
        ],
    )
    (e,) = await _events()
    assert e.properties == {"nested": {"ok": 1}, "note": "ab", "plan": "pro"}


async def test_ip_modes(owner, ingest):
    c, pid, key = owner["client"], owner["project_id"], owner["api_key"]
    ev = [{"event": "e", "distinct_id": "u", "context": {"ip": "198.51.100.23", "user_agent": "Mozilla/5.0"}}]
    await c.patch(f"/api/v1/projects/{pid}", json={"settings": {"ip_mode": "store"}})
    import asyncio

    from app.core.redis import get_redis

    await get_redis().flushdb()  # drop cached project settings (would expire on its own after 60s)
    await ingest(key, ev)
    await c.patch(f"/api/v1/projects/{pid}", json={"settings": {"ip_mode": "discard"}})
    await get_redis().flushdb()
    await ingest(key, [{**ev[0], "distinct_id": "u2"}])
    e = {e.distinct_id: e for e in await _events()}
    assert str(e["u"].ip) == "198.51.100.23"
    assert e["u2"].ip is None and e["u2"].user_agent is None
    _ = asyncio


async def test_retention_rejects_events_older_than_window(owner, ingest):
    c, pid, key = owner["client"], owner["project_id"], owner["api_key"]
    await c.patch(f"/api/v1/projects/{pid}", json={"settings": {"retention_days": 7, "retention_days_set": True}})
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    r = await ingest(
        key, [{"event": "old", "distinct_id": "u", "timestamp": old}, {"event": "new", "distinct_id": "u"}]
    )
    j = r.json()
    assert j["accepted"] == 1 and j["rejected"][0]["code"] == "OUTSIDE_RETENTION"


async def test_body_size_limit(owner, ingest, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_body_bytes", 5_000)
    big = [{"event": "e", "distinct_id": "u", "properties": {"blob": "x" * 6000}}]
    r = await ingest(owner["api_key"], big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_rate_limit_returns_429_with_headers_and_is_per_project(owner, ingest):
    c, pid, key = owner["client"], owner["project_id"], owner["api_key"]
    await c.patch(f"/api/v1/projects/{pid}", json={"settings": {"rate_limit_per_second": 3}})
    from app.core.redis import get_redis

    await get_redis().flushdb()
    responses = []
    for _ in range(8):
        responses.append(await ingest(key, [{"event": "e", "distinct_id": "u"}]))
    codes = [response.status_code for response in responses]
    assert codes.count(200) >= 3 and 429 in codes

    # A later request can succeed when the limiter's one-second window refills.
    # Assert headers on the actual rate-limited response, not whichever response ran last.
    limited = next(response for response in responses if response.status_code == 429)
    assert limited.headers["retry-after"]
    assert limited.headers["x-ratelimit-limit"] == "3"
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


async def test_usage_counters_are_reported(owner, ingest):
    await ingest(owner["api_key"], [{"event": "e", "distinct_id": "u"}, {"distinct_id": "bad"}])
    u = (await owner["client"].get(f"/api/v1/projects/{owner['project_id']}/usage")).json()
    assert u["totals"]["events_accepted"] == 1 and u["totals"]["events_rejected"] == 1 and u["totals"]["requests"] == 1


async def test_cors_public_ingestion_vs_dashboard(owner, ingest):
    c = ingest.client
    r = await c.options(
        "/api/v1/events/batch", headers={"Origin": "https://customer.example", "Access-Control-Request-Method": "POST"}
    )
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in r.headers
    r = await c.options(
        "/api/v1/projects", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert r.status_code == 403
    r = await c.get("/health", headers={"Origin": "http://localhost:5173"})
    assert (
        r.headers["access-control-allow-origin"] == "http://localhost:5173"
        and r.headers["access-control-allow-credentials"] == "true"
    )


async def test_concurrent_ingestion_of_same_new_user_creates_one_person(owner, ingest):
    import asyncio

    key = owner["api_key"]
    results = await asyncio.gather(*[ingest(key, [{"event": "e", "distinct_id": "racer"}]) for _ in range(8)])
    assert all(r.status_code == 200 for r in results)
    assert await _count(Person) == 1 and await _count(PersonDistinctId) == 1 and await _count(Event) == 8


async def test_small_sent_at_offsets_are_latency_not_skew(owner, ingest):
    """Regression: network latency must not shift timestamps (it reordered events across batches and broke funnels)."""
    now = datetime.now(UTC)
    ts = (now - timedelta(seconds=10)).isoformat()
    await ingest(owner["api_key"], [{"event": "e", "distinct_id": "u", "timestamp": ts}], sent_at=(now - timedelta(seconds=3)).isoformat())
    (e,) = await _events()
    assert e.timestamp == datetime.fromisoformat(ts)  # untouched: 3 s offset is below the 30 s dead zone
