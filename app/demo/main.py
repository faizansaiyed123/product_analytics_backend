from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
DAY = timedelta(days=1)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class DemoStore:
    def __init__(self) -> None:
        self.user = {
            "id": "user-demo-0001",
            "email": "demo@analytics.local",
            "name": "Demo Analyst",
            "email_verified": True,
            "created_at": iso(NOW - 180 * DAY),
        }
        self.org = {"id": "org-demo-0001", "name": "Acme Product Labs", "slug": "acme-product-labs"}
        self.project = {
            "id": "project-demo-0001",
            "org_id": self.org["id"],
            "name": "Acme Web App",
            "settings": {
                "timezone": "UTC",
                "session_timeout_minutes": 30,
                "retention_days": 365,
                "ip_mode": "anonymize",
                "rate_limit_per_second": 10,
                "property_denylist": ["password", "token"],
            },
            "role": "owner",
            "created_at": iso(NOW - 150 * DAY),
        }
        self.people = [
            {
                "id": f"person-demo-{i:04d}",
                "distinct_ids": [f"user_{i:03d}"],
                "email": f"user{i}@example.com",
                "name": f"Demo User {i}",
                "is_identified": True,
                "properties": {
                    "plan": "enterprise" if i % 4 == 0 else "pro" if i % 3 == 0 else "growth",
                    "country": ["US", "IN", "GB", "DE"][i % 4],
                },
                "first_seen": iso(NOW - (120 - i * 5) * DAY),
                "last_seen": iso(NOW - (i % 3) * DAY),
                "event_count": 80 + i * 17,
                "sessions": 8 + i,
                "country": ["US", "IN", "GB", "DE"][i % 4],
                "browser": "Chrome" if i % 2 else "Safari",
                "os": "macOS" if i % 3 else "Windows",
                "device_type": "mobile" if i % 4 == 0 else "desktop",
            }
            for i in range(1, 13)
        ]
        names = [
            "$pageview",
            "Signed Up",
            "Viewed Pricing",
            "Started Trial",
            "Created Project",
            "Invited Teammate",
            "Ran Report",
            "Exported Data",
        ]
        self.events = []
        for i in range(1, 73):
            person = self.people[(i - 1) % len(self.people)]
            event = names[(i - 1) % len(names)]
            stamp = NOW - ((i * 7) % 42) * DAY - timedelta(hours=(i % 7) * 3)
            path = ["/", "/pricing", "/docs", "/app"][i % 4] if event == "$pageview" else "/app/" + event.lower().replace(" ", "-")
            self.events.append(
                {
                    "id": f"event-demo-{i:04d}",
                    "event": event,
                    "timestamp": iso(stamp),
                    "received_at": iso(stamp + timedelta(seconds=1)),
                    "distinct_id": person["distinct_ids"][0],
                    "anonymous_id": f"anon-{i:04d}" if i % 5 == 0 else None,
                    "person_id": person["id"],
                    "insert_id": f"insert-{i:04d}",
                    "properties": {"plan": person["properties"]["plan"], "source": ["organic", "email", "paid"][i % 3]},
                    "path": path,
                    "hostname": "app.acme.example",
                    "country": person["country"],
                    "ip": None if i % 3 == 0 else "203.0.113.10",
                    "user_agent": "Mozilla/5.0 (Demo Browser)",
                    "device": {
                        "browser": person["browser"],
                        "browser_version": "157",
                        "os": person["os"],
                        "os_version": "15",
                        "device_type": person["device_type"],
                    },
                }
            )
        self.members = [
            {"user_id": self.user["id"], "email": self.user["email"], "name": self.user["name"], "role": "owner", "joined_at": self.user["created_at"]},
            {"user_id": "user-demo-0002", "email": "maya@acme.example", "name": "Maya Chen", "role": "admin", "joined_at": iso(NOW - 90 * DAY)},
            {"user_id": "user-demo-0003", "email": "daniel@acme.example", "name": "Daniel Wong", "role": "member", "joined_at": iso(NOW - 45 * DAY)},
        ]
        self.invitations = [{"id": "invite-demo-0001", "email": "new.engineer@example.com", "role": "member", "created_at": iso(NOW - 2 * DAY)}]
        self.api_keys = [
            {"id": "key-demo-0001", "name": "Production JS SDK", "key_prefix": "phc_demo_9f2a", "created_at": iso(NOW - 150 * DAY), "last_used_at": iso(NOW - timedelta(hours=2)), "revoked_at": None, "expires_at": None},
            {"id": "key-demo-0002", "name": "Staging", "key_prefix": "phc_demo_51c3", "created_at": iso(NOW - 32 * DAY), "last_used_at": iso(NOW - 9 * DAY), "revoked_at": None, "expires_at": iso(NOW + 60 * DAY)},
        ]
        self.reports = [
            {
                "id": "report-demo-0001",
                "project_id": self.project["id"],
                "name": "Activation trend",
                "description": "Trial activation over the last 30 days",
                "query": {"type": "trend", "series": [{"event": "Started Trial"}], "metric": "unique_users", "date_range": {"preset": "last_30_days"}, "interval": "day", "filters": [], "timezone": "UTC", "compare_previous": True},
                "visualization": {"chart": "line"},
                "created_by": self.user["id"],
                "created_by_name": self.user["name"],
                "created_at": iso(NOW - 20 * DAY),
                "updated_at": iso(NOW - 3 * DAY),
            }
        ]
        self.dashboards = [{
            "id": "dashboard-demo-0001",
            "project_id": self.project["id"],
            "name": "Executive overview",
            "description": "Core product health metrics",
            "config": {"widgets": [], "date_range": {"preset": "last_30_days"}},
            "created_at": iso(NOW - 25 * DAY),
            "updated_at": iso(NOW - 3 * DAY),
        }]
        self.cohorts = [{
            "id": "cohort-demo-0001",
            "project_id": self.project["id"],
            "name": "Trial users",
            "description": "Users who started a trial",
            "definition": {"match": "all", "criteria": [{"type": "performed_event", "event": "Started Trial", "operator": "at_least", "count": 1}]},
            "last_count": 74,
            "last_calculated_at": iso(NOW - timedelta(hours=2)),
            "created_at": iso(NOW - 15 * DAY),
            "updated_at": iso(NOW - 2 * DAY),
        }]
        self.alerts = [{
            "id": "alert-demo-0001",
            "project_id": self.project["id"],
            "name": "Activation drop",
            "enabled": True,
            "query": {"event": "Started Trial", "metric": "unique_users", "metric_property": None},
            "condition": {"operator": "decrease_pct", "value": 15},
            "period": "day",
            "webhook_url": None,
            "state": "healthy",
            "last_value": 82,
            "last_evaluated_at": iso(NOW - timedelta(minutes=45)),
            "last_triggered_at": None,
            "last_error": None,
        }]
        self.notifications = [{
            "id": "notification-demo-0001",
            "alert_id": self.alerts[0]["id"],
            "kind": "alert",
            "title": "Activation drop",
            "body": "Started Trial is within the expected range.",
            "created_at": iso(NOW - timedelta(hours=3)),
            "read_at": None,
        }]
        self.export_jobs = [{
            "id": "export-demo-0001",
            "project_id": self.project["id"],
            "kind": "events",
            "format": "csv",
            "status": "done",
            "row_count": len(self.events),
            "error": None,
            "created_at": iso(NOW - DAY),
            "finished_at": iso(NOW - timedelta(hours=23)),
        }]
        self.audit_logs = [
            {"id": "audit-demo-0001", "action": "project.create", "user_id": self.user["id"], "target_type": "project", "target_id": self.project["id"], "metadata": {"name": self.project["name"]}, "created_at": iso(NOW - 150 * DAY), "ip": "203.0.113.10"},
            {"id": "audit-demo-0002", "action": "api_key.rotate", "user_id": self.user["id"], "target_type": "api_key", "target_id": self.api_keys[0]["id"], "metadata": {}, "created_at": iso(NOW - 9 * DAY), "ip": "203.0.113.10"},
        ]
        self.logged_in = True

    def labels(self, n: int = 14) -> list[str]:
        return [(NOW - (n - i - 1) * DAY).date().isoformat() for i in range(n)]

    def envelope(self, result: dict, query_type: str) -> dict:
        return {"query_type": query_type, "result": result, "cached": False, "computed_at": iso(NOW), "duration_ms": 18}

    def query(self, q: dict) -> dict:
        qtype = q.get("type", "trend")
        labels = self.labels()
        if qtype == "trend":
            series = q.get("series") or [{"event": "$pageview"}]
            result = {
                "type": "trend",
                "kind": "distribution" if q.get("metric") == "frequency" else "time_series",
                "interval": q.get("interval", "day"),
                "labels": labels,
                "series": [
                    {"label": s.get("event", ""), "event": s.get("event"), "breakdown_value": None, "data": [18 + ((i * 7 + si * 11) % 28) for i in range(14)], "total": 410 + si * 56}
                    for si, s in enumerate(series)
                ],
                "date_from": labels[0],
                "date_to": labels[-1],
                "timezone": q.get("timezone") or "UTC",
                "previous": None,
            }
        elif qtype == "funnel":
            counts = [96, 77, 54, 39]
            steps = []
            for i, step in enumerate(q.get("steps") or []):
                count = counts[i] if i < len(counts) else max(10, counts[-1] - i * 7)
                previous = counts[i - 1] if i else count
                steps.append({
                    "order": i,
                    "event": step.get("event"),
                    "label": step.get("label") or step.get("event"),
                    "count": count,
                    "conversion_rate": 1 if i == 0 else count / previous,
                    "dropoff_count": 0 if i == 0 else previous - count,
                    "dropoff_rate": 0 if i == 0 else 1 - count / previous,
                    "overall_conversion_rate": count / counts[0],
                    "avg_time_to_convert_seconds": None if i == 0 else 1800 + i * 2200,
                    "median_time_to_convert_seconds": None if i == 0 else 1400 + i * 1900,
                })
            result = {"type": "funnel", "steps": steps, "overall_conversion_rate": (steps[-1]["count"] / steps[0]["count"]) if steps else 0, "breakdowns": None, "conversion_window_seconds": 1209600, "date_from": labels[0], "date_to": labels[-1], "timezone": q.get("timezone") or "UTC"}
        elif qtype == "retention":
            offsets = list(range(min(int(q.get("periods", 8)), 8)))
            cohorts = []
            for i in range(4):
                size = 40 - i * 4
                cohorts.append({"period": labels[4 + i], "size": size, "cells": [{"offset": off, "count": max(1, round(size * (0.72 ** off))), "rate": 1 if off == 0 else 0.72 ** off} for off in offsets]})
            result = {"type": "retention", "interval": q.get("interval", "day"), "offsets": offsets, "cohorts": cohorts, "average": [{"offset": o, "count": round(44 * (0.72 ** o)), "rate": 1 if o == 0 else 0.72 ** o} for o in offsets], "date_from": labels[0], "date_to": labels[-1], "timezone": q.get("timezone") or "UTC"}
        elif qtype == "paths":
            roots = ["/", "/pricing", "/docs"] if q.get("path_type") == "page" else ["$pageview", "Signed Up", "Viewed Pricing"]
            edges = []
            for step in range(1, min(q.get("depth", 4), 4) + 1):
                source = roots[(step - 1) % len(roots)]
                edges.extend([{"step": step, "source": source, "target": roots[step % len(roots)], "count": 90 - step * 13}, {"step": step, "source": source, "target": "$pageview", "count": 46 - step * 4}])
            result = {"type": "paths", "edges": edges, "sessions": 126}
        else:
            result = {
                "type": "sessions",
                "summary": {"sessions": 132, "avg_duration_seconds": 456, "avg_events_per_session": 6.4, "avg_pages_per_session": 3.1, "bounce_rate": 0.23, "unique_users": 94},
                "interval": q.get("interval", "day"),
                "labels": labels,
                "sessions_series": [7 + (i % 5) for i in range(14)],
                "avg_duration_series": [340 + (i % 6) * 22 for i in range(14)],
                "previous": {"sessions": 118, "avg_duration_seconds": 411, "avg_events_per_session": 5.8, "avg_pages_per_session": 2.8, "bounce_rate": 0.27, "unique_users": 87},
                "timezone": q.get("timezone") or "UTC",
            }
        return self.envelope(result, qtype)

    def overview(self) -> dict:
        labels = self.labels()
        return {
            "kpis": {"total_users": 148, "active_users": 94, "new_users": 26, "total_events": 12840, "sessions": 132, "events_per_user": 86.8, "avg_session_duration_seconds": 456},
            "previous_kpis": {"total_users": 131, "active_users": 86, "new_users": 21, "total_events": 11320, "sessions": 118, "events_per_user": 86.4, "avg_session_duration_seconds": 411},
            "interval": "day",
            "labels": labels,
            "event_volume": [730 + i * 28 for i in range(14)],
            "active_users_series": [49 + i * 2 for i in range(14)],
            "new_users_series": [5 + (i % 4) for i in range(14)],
            "user_growth_series": [2.1 + i * 0.15 for i in range(14)],
            "top_events": [{"event": e, "count": 210 - i * 23, "unique_users": 94 - i * 7} for i, e in enumerate(["$pageview", "Signed Up", "Viewed Pricing", "Started Trial", "Created Project", "Ran Report"])],
            "top_pages": [{"path": p, "views": 860 - i * 145, "unique_visitors": 310 - i * 42, "avg_views_per_user": 2.2 + i * 0.3} for i, p in enumerate(["/", "/pricing", "/docs", "/app/dashboard"])],
            "retention": self.query({"type": "retention", "periods": 8, "interval": "day"})["result"],
            "cached": False,
            "computed_at": iso(NOW),
        }

    def page(self, rows: list[dict], cursor: str | None = None, limit: int = 50) -> dict:
        start = 50 if cursor else 0
        batch = rows[start:start + limit]
        return {"items": batch, "next_cursor": "page-2" if start == 0 and len(rows) > limit else None}


store = DemoStore()

app = FastAPI(title="Product Analytics Demo API", version="1.0-demo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
@app.get("/health/live")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "demo"}


@app.get("/health/ready")
def ready() -> dict[str, str]:
    return {"status": "ready", "mode": "demo"}


def current_me() -> dict:
    return {
        "user": store.user,
        "organizations": [{"org_id": store.org["id"], "org_name": store.org["name"], "org_slug": store.org["slug"], "role": "owner"}],
        "csrf_token": "demo-csrf",
    }


def csv_bytes(rows: list[dict]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]) if rows else ["id"])
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k) if not isinstance(row.get(k), (dict, list)) else str(row.get(k)) for k in writer.fieldnames})
    return output.getvalue().encode()


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PATCH", "DELETE", "PUT", "OPTIONS"])
async def demo_api(path: str, request: Request):
    method = request.method
    parts = [p for p in path.split("/") if p]
    body = {}
    if method in {"POST", "PATCH", "PUT"}:
        try:
            body = await request.json()
        except Exception:
            body = {}
    params = dict(request.query_params)
    root = parts[0] if parts else ""

    if root == "auth":
        if parts[1:] == ["me"] or parts[1:] == ["login"] or parts[1:] == ["register"]:
            if parts[1] == "logout":
                store.logged_in = False
                return {"ok": True}
            if parts[1:] == ["register"]:
                store.logged_in = True
                if body.get("name"):
                    store.user["name"] = body["name"]
                if body.get("email"):
                    store.user["email"] = body["email"]
                return current_me()
            store.logged_in = True
            return current_me()
        if parts[1:] in (["logout"], ["change-password"], ["password-reset", "request"], ["password-reset", "confirm"], ["verify-email", "confirm"], ["verify-email", "request"]):
            return {"ok": True}

    if root == "organizations":
        if len(parts) == 1 and method == "GET":
            return [{"id": store.org["id"], "name": store.org["name"], "slug": store.org["slug"], "role": "owner"}]
        if len(parts) >= 3 and parts[2] == "members":
            if len(parts) == 3 and method == "GET":
                return store.members
            if len(parts) == 3 and method == "POST":
                member = {"user_id": str(uuid4()), "email": body.get("email", "new@example.com"), "name": body.get("email", "New Member").split("@")[0], "role": body.get("role", "member"), "joined_at": iso(NOW)}
                store.members.append(member)
                return {"status": "added", "member": member}
            if len(parts) == 4 and method == "PATCH":
                member = next((m for m in store.members if m["user_id"] == parts[3]), store.members[-1])
                member["role"] = body.get("role", member["role"])
                return member
            if len(parts) == 4 and method == "DELETE":
                store.members = [m for m in store.members if m["user_id"] != parts[3]]
                return {"ok": True}
        if len(parts) >= 3 and parts[2] == "invitations":
            if len(parts) == 3 and method == "GET":
                return store.invitations
            if len(parts) == 4 and method == "DELETE":
                store.invitations = [i for i in store.invitations if i["id"] != parts[3]]
                return {"ok": True}
        if len(parts) == 3 and parts[2] == "audit-logs":
            return store.audit_logs
        if len(parts) == 1 and method == "POST":
            return {"id": str(uuid4()), "name": body.get("name", "New Organization"), "slug": "new-org", "role": "owner"}

    if root == "projects":
        if len(parts) == 1:
            if method == "GET":
                return [store.project]
            if method == "POST":
                created = {**store.project, "id": str(uuid4()), "name": body.get("name", "New Project")}
                return {**created, "api_key": {"id": str(uuid4()), "name": "Default", "key_prefix": "phc_demo_new", "key": "phc_demo_new_secret", "created_at": iso(NOW), "last_used_at": None, "revoked_at": None, "expires_at": None}}
        pid = parts[1]
        if len(parts) == 2 and method == "GET":
            return store.project
        if len(parts) == 2 and method == "PATCH":
            store.project["name"] = body.get("name", store.project["name"])
            if body.get("settings"):
                store.project["settings"].update(body["settings"])
            return store.project
        if len(parts) == 2 and method == "DELETE":
            return {"ok": True}
        if len(parts) >= 3 and parts[2] == "api-keys":
            if len(parts) == 3 and method == "GET":
                return store.api_keys
            if len(parts) == 3 and method == "POST":
                key = {"id": str(uuid4()), "name": body.get("name", "New key"), "key_prefix": "phc_demo_new", "created_at": iso(NOW), "last_used_at": None, "revoked_at": None, "expires_at": None}
                store.api_keys.append(key)
                return {**key, "key": "phc_demo_new_secret"}
            if len(parts) == 5 and parts[4] == "rotate" and method == "POST":
                key = {"id": str(uuid4()), "name": "Rotated key", "key_prefix": "phc_demo_rotated", "created_at": iso(NOW), "last_used_at": None, "revoked_at": None, "expires_at": None}
                store.api_keys.append(key)
                return {**key, "key": "phc_demo_rotated_secret"}
            if len(parts) == 4 and method == "DELETE":
                key = next((k for k in store.api_keys if k["id"] == parts[3]), store.api_keys[0])
                key["revoked_at"] = iso(NOW)
                return key
        if len(parts) == 3 and parts[2] == "usage":
            days = int(params.get("days", "14"))
            rows = []
            for i in range(min(days, 30)):
                rows.append({"date": (NOW - (min(days, 30) - i - 1) * DAY).date().isoformat(), "requests": 120 + i * 11, "events_accepted": 740 + i * 24, "events_rejected": 4 + i % 3, "events_duplicate": 18 + i % 5, "rate_limited": 1 if i % 4 == 0 else 0})
            totals = {k: sum(r[k] for r in rows) for k in ["requests", "events_accepted", "events_rejected", "events_duplicate", "rate_limited"]}
            return {"days": rows, "totals": totals, "rate_limit_per_second": store.project["settings"]["rate_limit_per_second"]}
        if len(parts) == 4 and parts[2:4] == ["data", "purge"]:
            return {"ok": True}
        if len(parts) == 4 and parts[2] == "persons" and method == "DELETE":
            store.people = [p for p in store.people if p["id"] != parts[3]]
            store.events = [e for e in store.events if e["person_id"] != parts[3]]
            return {"ok": True}

    if root == "analytics":
        if parts[1:] == ["overview"]:
            return store.overview()
        if parts[1:] == ["query"]:
            return store.query(body.get("query", {}))
        if parts[1:] == ["sessions", "list"]:
            offset = int(body.get("offset", 0))
            rows = [{"person_id": p["id"], "distinct_id": p["distinct_ids"][0], "start": iso(NOW - timedelta(hours=i + 1)), "end": iso(NOW - timedelta(hours=i + 1) + timedelta(minutes=30)), "duration_seconds": 1800, "events": 6 + i, "pages": 3, "entry_path": "/", "exit_path": "/app"} for i, p in enumerate(store.people[offset:offset + 6])]
            return {"items": rows, "next_offset": offset + len(rows) if offset + len(rows) < len(store.people) else None}

    if root == "events":
        if len(parts) == 1:
            cursor = params.get("cursor")
            return store.page(store.events, cursor=cursor, limit=int(params.get("limit", "50")))
        event = next((e for e in store.events if e["id"] == parts[1]), store.events[0])
        return event

    if root == "users":
        if len(parts) == 1:
            return store.page(store.people, cursor=params.get("cursor"), limit=int(params.get("limit", "50")))
        person = next((p for p in store.people if p["id"] == parts[1]), store.people[0])
        if len(parts) == 3 and parts[2] == "events":
            return {"items": [e for e in store.events if e["person_id"] == person["id"]], "next_cursor": None}
        return person

    if root == "meta":
        if parts[1:] == ["event-definitions"]:
            return [{"name": n, "first_seen": iso(NOW - 90 * DAY), "last_seen": iso(NOW - DAY)} for n in ["$pageview", "Signed Up", "Viewed Pricing", "Started Trial", "Created Project", "Invited Teammate", "Ran Report"]]
        if parts[1:] == ["property-definitions"]:
            return [
                {"name": "plan", "kind": "person", "value_type": "string"},
                {"name": "country", "kind": "person", "value_type": "string", "builtin": True},
                {"name": "$current_url", "kind": "event", "value_type": "string", "builtin": True},
                {"name": "source", "kind": "event", "value_type": "string"},
                {"name": "value", "kind": "event", "value_type": "number"},
            ]
        if parts[1:] == ["property-values"]:
            return [{"value": "enterprise", "count": 18}, {"value": "pro", "count": 42}, {"value": "growth", "count": 64}]

    if root in {"cohorts", "reports", "dashboards", "alerts"}:
        collection = getattr(store, root)
        if len(parts) == 1:
            if method == "GET":
                return collection
            created = {
                **body,
                "id": str(uuid4()),
                "project_id": body.get("project_id", store.project["id"]),
                "created_at": iso(NOW),
                "updated_at": iso(NOW),
            }
            if root == "reports":
                created["created_by"] = store.user["id"]
                created["created_by_name"] = store.user["name"]
            if root == "cohorts":
                created.update({"last_count": 24, "last_calculated_at": iso(NOW)})
            if root == "alerts":
                created.update({"enabled": body.get("enabled", True), "state": "healthy", "last_value": None, "last_evaluated_at": None, "last_triggered_at": None, "last_error": None, "webhook_url": body.get("webhook_url")})
            collection.insert(0, created)
            return created
        if len(parts) == 2 and parts[1] == "preview" and root == "cohorts" and method == "POST":
            return {"count": 27}
        item = next((x for x in collection if x["id"] == parts[1]), None)
        if len(parts) == 3 and parts[2] == "members" and root == "cohorts":
            return {"items": store.people[:8], "total": 8}
        if len(parts) == 3 and parts[2] == "refresh" and root == "cohorts" and method == "POST":
            if item:
                item["last_count"] = 27
                item["last_calculated_at"] = iso(NOW)
            return item
        if len(parts) == 3 and parts[2] in {"duplicate", "evaluate"} and method == "POST":
            if not item:
                return {"ok": True}
            if parts[2] == "duplicate":
                copy = {**item, "id": str(uuid4()), "name": item["name"] + " (copy)", "created_at": iso(NOW), "updated_at": iso(NOW)}
                collection.insert(0, copy)
                return copy
            item.update({"last_evaluated_at": iso(NOW), "last_value": 84, "state": "healthy"})
            return item
        if item is not None and method == "GET":
            return item
        if item is not None and method == "PATCH":
            item.update(body)
            item["updated_at"] = iso(NOW)
            return item
        if item is not None and method == "DELETE":
            collection.remove(item)
            return {"ok": True}

    if root == "notifications":
        if method == "GET":
            unread = sum(1 for n in store.notifications if not n["read_at"])
            return {"items": store.notifications, "unread": unread}
        for n in store.notifications:
            n["read_at"] = iso(NOW)
        return {"ok": True}

    if root == "export":
        if len(parts) == 2 and parts[1] == "events" and method == "GET":
            return Response(content=csv_bytes(store.events[:50]), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="events.csv"'})
        if len(parts) == 2 and parts[1] == "users" and method == "GET":
            return Response(content=csv_bytes(store.people), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="users.csv"'})
        if len(parts) == 2 and parts[1] == "reports" and method == "GET":
            return Response(content=csv_bytes(store.reports), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="reports.csv"'})
        if len(parts) == 2 and parts[1] == "analytics" and method == "POST":
            env = store.query(body.get("query", {}))
            return Response(content=str(env["result"]).encode(), media_type="application/json")
        if len(parts) >= 2 and parts[1] == "jobs":
            if len(parts) == 2 and method == "GET":
                return store.export_jobs
            if len(parts) == 2 and method == "POST":
                job = {"id": str(uuid4()), "project_id": body.get("project_id", store.project["id"]), "kind": body.get("kind", "events"), "format": body.get("format", "csv"), "status": "done", "row_count": len(store.events), "error": None, "created_at": iso(NOW), "finished_at": iso(NOW)}
                store.export_jobs.insert(0, job)
                return job
            if len(parts) == 3 and method == "GET":
                return next((j for j in store.export_jobs if j["id"] == parts[2]), store.export_jobs[0])

    return {"ok": True, "mode": "demo", "path": path}
