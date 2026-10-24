"""Event ingestion: validation/normalisation, identity resolution, bulk insert.

Rules (see docs/events.md):
* Each event is validated independently; a bad event never fails the batch.
* A distinct_id maps to exactly one person per project. `$identify` (distinct_id + anonymous_id) and
  `$alias` link ids; anonymous persons are merged into identified ones, two identified persons never merge.
* Events are idempotent when the client sends a unique `uuid`/`insert_id`.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from prometheus_client import Counter
from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import uuid7
from app.core.timeutil import utcnow
from app.models import Event, EventDefinition, Person, PersonDistinctId, PropertyDefinition
from app.schemas.projects import ProjectSettings
from app.services.useragent import parse_user_agent

logger = logging.getLogger("app.ingestion")

EVENTS_ACCEPTED = Counter("events_accepted_total", "Events accepted")
EVENTS_REJECTED = Counter("events_rejected_total", "Events rejected", ["code"])

FORBIDDEN_DISTINCT_IDS = {
    "",
    "null",
    "undefined",
    "none",
    "nan",
    "anonymous",
    "guest",
    "[object object]",
    "0",
    "false",
    "true",
}
MAX_FUTURE_SKEW = timedelta(minutes=5)
SKEW_DEADZONE = timedelta(seconds=30)
MAX_DEPTH = 8


class InvalidEvent(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Retry(Exception):
    """A concurrent identity merge removed a person we were about to use; redo the batch."""


@dataclass
class Rejection:
    index: int
    code: str
    message: str


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: list[Rejection] = field(default_factory=list)


@dataclass
class RequestMeta:
    ip: str | None
    user_agent: str | None
    country_header: str | None
    received_at: datetime


@dataclass
class Normalized:
    index: int
    id: UUID
    insert_id: str | None
    event: str
    distinct_id: str
    anonymous_id: str | None
    timestamp: datetime
    properties: dict[str, Any]
    set_props: dict[str, Any]
    set_once: dict[str, Any]
    alias: str | None
    path: str | None
    hostname: str | None
    device: dict[str, str | None]
    country: str | None
    ip: str | None
    user_agent: str | None


# ---------------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------------
def _clean(value: Any, deny: set[str], depth: int = 0) -> Any:
    max_len = get_settings().max_property_value_bytes
    if depth > MAX_DEPTH:
        raise InvalidEvent("PROPERTIES_TOO_DEEP", f"Properties may be nested at most {MAX_DEPTH} levels")
    if isinstance(value, str):
        return value.replace("\x00", "")[:max_len]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_clean(v, deny, depth + 1) for v in value[:200]]
    if isinstance(value, dict):
        return {
            str(k).replace("\x00", "")[:200]: _clean(v, deny, depth + 1)
            for k, v in list(value.items())[:200]
            if str(k).lower() not in deny
        }
    return str(value)[:max_len]


def _parse_timestamp(raw: Any) -> datetime:
    try:
        if isinstance(raw, bool):
            raise ValueError
        if isinstance(raw, (int, float)):
            seconds = raw / 1000 if raw > 1e11 else raw
            return datetime.fromtimestamp(seconds, UTC)
        if isinstance(raw, str):
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        pass
    raise InvalidEvent("INVALID_TIMESTAMP", "timestamp must be an ISO 8601 string or a unix epoch number")


def _norm_id(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise InvalidEvent("INVALID_DISTINCT_ID", "distinct_id must be a string")
    s = str(raw).strip().replace("\x00", "")
    return s or None


def apply_ip_mode(ip: str | None, mode: str) -> str | None:
    if not ip or mode == "discard":
        return None
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if mode == "anonymize":
        if addr.version == 4:
            return str(ipaddress.ip_address(int(addr) & 0xFFFFFF00))
        return str(ipaddress.ip_address(int(addr) & (((1 << 48) - 1) << 80)))
    return str(addr)


def normalize_event(
    raw: Any, index: int, settings: ProjectSettings, meta: RequestMeta, offset: timedelta
) -> Normalized:
    cfg = get_settings()
    if not isinstance(raw, dict):
        raise InvalidEvent("INVALID_EVENT", "Each event must be a JSON object")
    name = raw.get("event")
    if not isinstance(name, str) or not name.strip():
        raise InvalidEvent("INVALID_EVENT", "Event name is required")
    name = name.strip().replace("\x00", "")
    if len(name) > cfg.max_event_name_length:
        raise InvalidEvent("INVALID_EVENT", f"Event name must be at most {cfg.max_event_name_length} characters")

    distinct = _norm_id(raw.get("distinct_id"))
    anon = _norm_id(raw.get("anonymous_id"))
    distinct = distinct or anon
    if not distinct:
        raise InvalidEvent("MISSING_DISTINCT_ID", "distinct_id (or anonymous_id) is required")
    for value in (distinct, anon):
        if value and (value.lower() in FORBIDDEN_DISTINCT_IDS or len(value) > 400):
            raise InvalidEvent("INVALID_DISTINCT_ID", f"distinct_id {value[:40]!r} is not allowed")

    props_raw = raw.get("properties") or {}
    if not isinstance(props_raw, dict):
        raise InvalidEvent("INVALID_PROPERTIES", "properties must be an object")
    if len(props_raw) > cfg.max_properties_per_event:
        raise InvalidEvent("TOO_MANY_PROPERTIES", f"At most {cfg.max_properties_per_event} properties are allowed")
    deny = set(settings.property_denylist)
    props = _clean(props_raw, deny)
    set_props = props.pop("$set", None)
    set_once = props.pop("$set_once", None)
    set_props = set_props if isinstance(set_props, dict) else {}
    set_once = set_once if isinstance(set_once, dict) else {}
    alias = props.get("alias") if name == "$alias" else None
    alias = _norm_id(alias) if alias is not None else None
    if name == "$identify" and not anon:
        anon = _norm_id(props.get("$anon_distinct_id"))

    ctx = raw.get("context") or {}
    if not isinstance(ctx, dict):
        raise InvalidEvent("INVALID_CONTEXT", "context must be an object")

    # timestamp: client clock corrected by sent_at offset; future timestamps are clamped
    ts = _parse_timestamp(raw["timestamp"]) + offset if raw.get("timestamp") is not None else meta.received_at
    if ts > meta.received_at + MAX_FUTURE_SKEW:
        ts = meta.received_at
    if settings.retention_days and ts < meta.received_at - timedelta(days=settings.retention_days):
        raise InvalidEvent(
            "OUTSIDE_RETENTION", f"Event is older than the project's {settings.retention_days}-day retention"
        )

    insert_id = None
    for key in ("uuid", "insert_id", "event_id"):
        if raw.get(key) is not None:
            insert_id = str(raw[key])[:200]
            break

    # page info
    url = props.get("$current_url") or props.get("url")
    parsed = urlparse(url) if isinstance(url, str) else None
    path = props.get("$pathname") or props.get("path") or (parsed.path if parsed and parsed.path else None)
    host = props.get("$host") or props.get("hostname") or (parsed.hostname if parsed else None)
    path = str(path)[:2000] if path is not None else None
    host = str(host)[:255] if host is not None else None

    # device
    ua = ctx.get("user_agent") if isinstance(ctx.get("user_agent"), str) else None
    ua = ua or meta.user_agent
    device = dict(parse_user_agent(ua or ""))
    for key in ("browser", "browser_version", "os", "os_version", "device_type"):
        if isinstance(ctx.get(key), str) and ctx[key]:
            device[key] = ctx[key][:80]

    country = ctx.get("country") if isinstance(ctx.get("country"), str) else meta.country_header
    country = country.strip().upper() if country else None
    country = country if country and len(country) == 2 and country.isalpha() and country not in ("XX", "T1") else None

    ip = apply_ip_mode(ctx["ip"] if isinstance(ctx.get("ip"), str) else meta.ip, settings.ip_mode)
    stored_ua = ua[:500] if ua and settings.ip_mode != "discard" else None

    return Normalized(
        index=index,
        id=uuid7(),
        insert_id=insert_id,
        event=name,
        distinct_id=distinct,
        anonymous_id=anon,
        timestamp=ts,
        properties=props,
        set_props=set_props,
        set_once=set_once,
        alias=alias,
        path=path,
        hostname=host,
        device=device,
        country=country,
        ip=ip,
        user_agent=stored_ua,
    )


# ---------------------------------------------------------------------------------------------------
# identity resolution
# ---------------------------------------------------------------------------------------------------
async def _load_mappings(db: AsyncSession, project_id: UUID, ids: set[str]) -> dict[str, UUID]:
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(PersonDistinctId.distinct_id, PersonDistinctId.person_id).where(
                PersonDistinctId.project_id == project_id, PersonDistinctId.distinct_id.in_(list(ids))
            )
        )
    ).all()
    return {d: p for d, p in rows}


async def resolve_persons(db: AsyncSession, project_id: UUID, ids: dict[str, datetime]) -> dict[str, UUID]:
    """Return distinct_id -> person_id, creating persons for unknown ids (race-safe)."""
    mapping = await _load_mappings(db, project_id, set(ids))
    missing = [d for d in ids if d not in mapping]
    if not missing:
        return mapping
    now = utcnow()
    new_persons = {d: uuid7() for d in missing}
    await db.execute(
        pg_insert(Person).values(
            [
                {
                    "id": pid,
                    "project_id": project_id,
                    "properties": {},
                    "is_identified": False,
                    "first_seen": ids[d],
                    "last_seen": ids[d],
                    "created_at": now,
                    "updated_at": now,
                }
                for d, pid in new_persons.items()
            ]
        )
    )
    inserted = (
        (
            await db.execute(
                pg_insert(PersonDistinctId)
                .values(
                    [{"project_id": project_id, "distinct_id": d, "person_id": pid} for d, pid in new_persons.items()]
                )
                .on_conflict_do_nothing(index_elements=["project_id", "distinct_id"])
                .returning(PersonDistinctId.distinct_id)
            )
        )
        .scalars()
        .all()
    )
    lost = [d for d in missing if d not in set(inserted)]
    if lost:  # another request created these ids first: use theirs, discard our orphan persons
        await db.execute(delete(Person).where(Person.id.in_([new_persons[d] for d in lost])))
        mapping.update(await _load_mappings(db, project_id, set(lost)))
    for d in inserted:
        mapping[d] = new_persons[d]
    return mapping


async def merge_persons(db: AsyncSession, project_id: UUID, source: UUID, target: UUID) -> None:
    """Merge `source` into `target`: ids, events and properties move; the source person is deleted."""
    src = (await db.execute(select(Person).where(Person.id == source).with_for_update())).scalar_one_or_none()
    tgt = (await db.execute(select(Person).where(Person.id == target).with_for_update())).scalar_one_or_none()
    if src is None or tgt is None:
        raise _Retry()
    await db.execute(update(PersonDistinctId).where(PersonDistinctId.person_id == source).values(person_id=target))
    await db.execute(
        update(Event).where(Event.project_id == project_id, Event.person_id == source).values(person_id=target)
    )
    tgt.properties = {**(src.properties or {}), **(tgt.properties or {})}  # target wins on conflicts
    tgt.first_seen = min(tgt.first_seen, src.first_seen)
    tgt.last_seen = max(tgt.last_seen, src.last_seen)
    tgt.is_identified = True
    await db.delete(src)
    await db.flush()


async def process_identity_events(
    db: AsyncSession, project_id: UUID, events: list[Normalized], mapping: dict[str, UUID]
) -> set[UUID]:
    """Handle $identify/$alias in order. Returns the set of person ids marked identified."""
    identified: set[UUID] = set()
    link_events = [e for e in events if e.event in ("$identify", "$alias")]
    if not link_events:
        return identified
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"identity:{project_id}"})
    for e in link_events:
        other = e.anonymous_id if e.event == "$identify" else e.alias
        d = e.distinct_id
        if d not in mapping:
            continue
        pd_id = mapping[d]
        if other and other != d:
            if other not in mapping:
                mapping.update(await _load_mappings(db, project_id, {other}))
            if other not in mapping:  # unknown anonymous id: attach it to the identified person
                await db.execute(
                    pg_insert(PersonDistinctId)
                    .values(project_id=project_id, distinct_id=other, person_id=pd_id)
                    .on_conflict_do_nothing(index_elements=["project_id", "distinct_id"])
                )
                mapping[other] = pd_id
            po_id = mapping[other]
            if po_id != pd_id:
                flags = dict(
                    (
                        await db.execute(select(Person.id, Person.is_identified).where(Person.id.in_([pd_id, po_id])))
                    ).all()
                )
                if len(flags) < 2:
                    raise _Retry()
                if flags[pd_id] and flags[po_id]:
                    logger.info(
                        "identity conflict: refusing to merge two identified persons",
                        extra={"project_id": str(project_id)},
                    )
                else:
                    source, target = (po_id, pd_id) if not flags[po_id] else (pd_id, po_id)
                    await merge_persons(db, project_id, source, target)
                    for k, v in list(mapping.items()):
                        if v == source:
                            mapping[k] = target
        identified.add(mapping[d])
    return identified


# ---------------------------------------------------------------------------------------------------
# definitions cache (avoid an upsert per event)
# ---------------------------------------------------------------------------------------------------
_seen: dict[tuple, float] = {}
_SEEN_TTL = 300.0


def _fresh(key: tuple) -> bool:
    now = time.monotonic()
    t = _seen.get(key)
    if t is not None and now - t < _SEEN_TTL:
        return True
    if len(_seen) > 50_000:
        _seen.clear()
    _seen[key] = now
    return False


def _value_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "string"


async def _record_definitions(db: AsyncSession, project_id: UUID, events: list[Normalized]) -> None:
    now = utcnow()
    ev_defs: dict[str, tuple[datetime, datetime]] = {}
    prop_defs: dict[tuple[str, str], str] = {}
    for e in events:
        lo, hi = ev_defs.get(e.event, (e.timestamp, e.timestamp))
        ev_defs[e.event] = (min(lo, e.timestamp), max(hi, e.timestamp))
        for k, v in list(e.properties.items())[:100]:
            if not k.startswith("$") or k in ("$current_url", "$pathname", "$host", "$referrer", "$title"):
                prop_defs.setdefault(("event", k), _value_type(v))
        for k, v in list(e.set_props.items())[:100]:
            prop_defs.setdefault(("person", k), _value_type(v))
    rows = [
        {"project_id": project_id, "name": n, "first_seen": lo, "last_seen": hi}
        for n, (lo, hi) in ev_defs.items()
        if not _fresh(("e", project_id, n))
    ]
    if rows:
        stmt = pg_insert(EventDefinition).values(rows)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=["project_id", "name"],
                set_={
                    "first_seen": text("LEAST(event_definitions.first_seen, EXCLUDED.first_seen)"),
                    "last_seen": text("GREATEST(event_definitions.last_seen, EXCLUDED.last_seen)"),
                },
            )
        )
    prows = [
        {"project_id": project_id, "kind": kind, "name": n, "value_type": t, "first_seen": now}
        for (kind, n), t in prop_defs.items()
        if not _fresh(("p", project_id, kind, n))
    ]
    if prows:
        await db.execute(pg_insert(PropertyDefinition).values(prows).on_conflict_do_nothing())


# ---------------------------------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------------------------------
_PERSON_UPDATE = text(
    """
    UPDATE persons SET
      properties = (CAST(:set_once AS jsonb) || properties) || CAST(:set AS jsonb),
      first_seen = LEAST(first_seen, :first_seen),
      last_seen = GREATEST(last_seen, :last_seen),
      is_identified = is_identified OR :identified,
      updated_at = now()
    WHERE id = :pid
    """
).bindparams(bindparam("first_seen", type_=Person.first_seen.type), bindparam("last_seen", type_=Person.last_seen.type))


async def _ingest_once(
    db: AsyncSession, project_id: UUID, settings: ProjectSettings, valid: list[Normalized]
) -> tuple[int, int]:
    id_times: dict[str, datetime] = {}
    for e in valid:
        for d in (e.distinct_id, e.anonymous_id if e.event == "$identify" else None, e.alias):
            if d:
                id_times[d] = min(id_times.get(d, e.timestamp), e.timestamp)
    mapping = await resolve_persons(db, project_id, id_times)
    identified = await process_identity_events(db, project_id, valid, mapping)

    per_person: dict[UUID, dict[str, Any]] = {}
    for e in valid:
        pid = mapping[e.distinct_id]
        agg = per_person.setdefault(pid, {"set": {}, "set_once": {}, "first": e.timestamp, "last": e.timestamp})
        agg["set"].update(e.set_props)
        for k, v in e.set_once.items():
            agg["set_once"].setdefault(k, v)
        agg["first"], agg["last"] = min(agg["first"], e.timestamp), max(agg["last"], e.timestamp)

    # Lock the persons we are about to update (in id order, so concurrent batches cannot deadlock) which also
    # guarantees a concurrent merge cannot delete a person our events are about to reference.
    ids = sorted(per_person)
    found = (
        (
            await db.execute(
                text("SELECT id FROM persons WHERE id = ANY(:ids) ORDER BY id FOR UPDATE").bindparams(
                    bindparam("ids", value=ids)
                )
            )
        )
        .scalars()
        .all()
    )
    if len(found) != len(ids):
        raise _Retry()

    await db.execute(
        _PERSON_UPDATE,
        [
            {
                "pid": pid,
                "set": json.dumps(a["set"]),
                "set_once": json.dumps(a["set_once"]),
                "first_seen": a["first"],
                "last_seen": a["last"],
                "identified": pid in identified,
            }
            for pid, a in per_person.items()
        ],
    )

    rows = [
        {
            "id": e.id,
            "project_id": project_id,
            "event": e.event,
            "insert_id": e.insert_id,
            "distinct_id": e.distinct_id,
            "anonymous_id": e.anonymous_id,
            "person_id": mapping[e.distinct_id],
            "timestamp": e.timestamp,
            "properties": e.properties,
            "path": e.path,
            "hostname": e.hostname,
            "browser": e.device["browser"],
            "browser_version": e.device["browser_version"],
            "os": e.device["os"],
            "os_version": e.device["os_version"],
            "device_type": e.device["device_type"],
            "country": e.country,
            "ip": e.ip,
            "user_agent": e.user_agent,
        }
        for e in valid
    ]
    inserted = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i : i + 500]
        res = await db.execute(
            pg_insert(Event)
            .values(chunk)
            .on_conflict_do_nothing(
                index_elements=["project_id", "insert_id"], index_where=text("insert_id IS NOT NULL")
            )
            .returning(Event.id)
        )
        inserted += len(res.all())
    await _record_definitions(db, project_id, valid)
    return inserted, len(rows) - inserted


async def ingest_events(
    db: AsyncSession,
    project_id: UUID,
    settings: ProjectSettings,
    raw_events: list[Any],
    meta: RequestMeta,
    sent_at: datetime | None = None,
) -> IngestResult:
    result = IngestResult()
    offset = (meta.received_at - sent_at) if sent_at else timedelta(0)
    # sent_at corrects a genuinely wrong client clock. Offsets below SKEW_DEADZONE are just network latency (and would add
    # per-request jitter that can reorder events across batches), so they are ignored; absurd offsets are ignored too.
    if abs(offset) < SKEW_DEADZONE or abs(offset) > timedelta(days=2):
        offset = timedelta(0)
    valid: list[Normalized] = []
    for i, raw in enumerate(raw_events):
        try:
            valid.append(normalize_event(raw, i, settings, meta, offset))
        except InvalidEvent as exc:
            result.rejected.append(Rejection(i, exc.code, exc.message))
            EVENTS_REJECTED.labels(exc.code).inc()
        except Exception:  # defensive: never let one event take down the batch
            logger.exception("unexpected error normalising event")
            result.rejected.append(Rejection(i, "INVALID_EVENT", "Event could not be processed"))
            EVENTS_REJECTED.labels("INVALID_EVENT").inc()
    if not valid:
        return result
    for attempt in range(4):
        try:
            inserted, dupes = await _ingest_once(db, project_id, settings, valid)
            break
        except _Retry:
            await db.rollback()
            if attempt == 3:
                raise
    result.accepted, result.duplicates = inserted, dupes
    EVENTS_ACCEPTED.inc(inserted)
    return result
