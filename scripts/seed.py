"""Development seed: python -m scripts.seed [--users 300] [--days 45]

Generates realistic data by pushing events through the REAL ingestion pipeline (same code path as the API), so
identity merging, sessions and definitions are exercised. Creates a demo account only if DEMO_EMAIL and DEMO_PASSWORD
are set in the environment (never hard-coded). Safe to re-run: it reuses the demo org/project and adds more data."""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import hash_password
from app.core.timeutil import utcnow
from app.db.session import dispose_engines, get_sessionmaker
from app.models import ApiKey, Membership, Organization, Project, User
from app.schemas.projects import ProjectSettings
from app.services import ingestion
from app.services.auth import unique_slug
from app.services.projects import create_api_key

BROWSERS = [("Chrome", "Windows", "desktop", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            ("Safari", "iOS", "mobile", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"),
            ("Firefox", "Linux", "desktop", "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"),
            ("Chrome", "Android", "mobile", "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"),
            ("Safari", "Mac OS X", "desktop", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15")]
COUNTRIES = ["US", "IN", "GB", "DE", "BR", "CA", "FR", "AU"]
SOURCES = [("google", "organic"), ("twitter", "social"), ("newsletter", "email"), (None, None), (None, None), ("producthunt", "referral")]
PAGES = ["/", "/pricing", "/features", "/blog", "/docs", "/about"]
PLANS = [("starter", 19), ("pro", 49), ("team", 129)]


def _ev(name: str, distinct: str, ts: datetime, ctx: dict, props: dict | None = None, **extra) -> dict:  # type: ignore[type-arg]
    return {"event": name, "distinct_id": distinct, "timestamp": ts.isoformat(), "properties": props or {}, "context": ctx, **extra}


def build_events(rng: random.Random, users: int, days: int) -> list[dict]:  # type: ignore[type-arg]
    now = utcnow()
    out: list[dict] = []  # type: ignore[type-arg]
    for i in range(users):
        browser, os_, device, ua = rng.choice(BROWSERS)
        ctx = {"user_agent": ua, "country": rng.choices(COUNTRIES, weights=[30, 20, 10, 8, 8, 8, 8, 8])[0], "browser": browser, "os": os_, "device_type": device}
        src, medium = rng.choice(SOURCES)
        first_day = rng.randint(0, days - 1)
        anon = f"anon_{i:04d}"
        user_id = f"user_{i:04d}"
        will_signup, will_buy = rng.random() < 0.55, rng.random() < 0.2
        plan, price = rng.choice(PLANS)
        identified = False
        n_visits = rng.choices([1, 2, 3, 5, 8], weights=[35, 25, 20, 12, 8])[0] if will_signup else rng.choices([1, 2], weights=[80, 20])[0]
        day = first_day
        for v in range(n_visits):
            if day > days - 1 and v > 0:
                break
            start = (now - timedelta(days=days - day)).replace(hour=rng.randint(7, 22), minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0)
            if start > now:
                break
            t = start
            who = user_id if identified else anon
            utm = {"utm_source": src, "utm_medium": medium} if src and v == 0 else {}
            for p in range(rng.randint(1, 5)):
                path = "/" if p == 0 and v == 0 else rng.choice(PAGES)
                out.append(_ev("$pageview", who, t, ctx, {"$pathname": path, "$host": "demo.example.com", "$current_url": f"https://demo.example.com{path}",
                                                          "$title": path.strip("/").title() or "Home", **utm}))
                t += timedelta(seconds=rng.randint(10, 150))
            if v == 0 and will_signup:
                out.append(_ev("signup_started", who, t, ctx))
                t += timedelta(seconds=rng.randint(20, 120))
                if rng.random() < 0.85:
                    out.append(_ev("signup_completed", who, t, ctx, {"method": rng.choice(["email", "google", "github"])}))
                    out.append(_ev("$identify", user_id, t + timedelta(seconds=1), ctx, {"$set": {"email": f"{user_id}@example.com", "name": f"User {i}", "plan": "free", "company": rng.choice(["Acme", "Globex", "Initech", "Umbrella", "Hooli"])}}, anonymous_id=anon))
                    identified = True
                    who = user_id
                    t += timedelta(seconds=rng.randint(30, 300))
            if identified:
                for _ in range(rng.randint(0, 4)):
                    out.append(_ev(rng.choice(["button_clicked", "report_viewed", "dashboard_created", "invite_sent"]), who, t, ctx,
                                   {"button": rng.choice(["export", "share", "settings", "upgrade"])}))
                    t += timedelta(seconds=rng.randint(15, 240))
                if will_buy and v >= 1 and rng.random() < 0.7:
                    out.append(_ev("checkout_started", who, t, ctx, {"plan": plan}))
                    t += timedelta(seconds=rng.randint(30, 200))
                    out.append(_ev("purchase", who, t, ctx, {"plan": plan, "amount": float(price), "currency": "USD", "$set": {"plan": plan}}))
                    will_buy = False
            day += rng.choice([1, 1, 2, 3, 7])
    return out


async def run(users: int, days: int) -> None:
    s = get_settings()
    sm = get_sessionmaker()
    async with sm() as db:
        email = s.demo_email or "seed@example.com"
        user = (await db.execute(select(User).where(func.lower(User.email) == email.lower()))).scalar_one_or_none()
        if user is None:
            user = User(email=email.lower(), name="Demo User", password_hash=hash_password(s.demo_password or "seed-" + str(random.random())),
                        email_verified_at=utcnow())
            db.add(user)
            await db.flush()
        org = (await db.execute(select(Organization).join(Membership, Membership.org_id == Organization.id)
                                .where(Membership.user_id == user.id, Organization.name == "Demo Org"))).scalar_one_or_none()
        if org is None:
            org = Organization(name="Demo Org", slug=await unique_slug(db, "Demo Org"))
            db.add(org)
            await db.flush()
            db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
        project = (await db.execute(select(Project).where(Project.org_id == org.id, Project.name == "Demo Web App", Project.deleted_at.is_(None)))).scalar_one_or_none()
        raw_key = None
        if project is None:
            project = Project(org_id=org.id, name="Demo Web App", settings=ProjectSettings(timezone="UTC").model_dump())
            db.add(project)
            await db.flush()
            _, raw_key = await create_api_key(db, project.id, "Seed key", user.id)
        await db.commit()
        pid, settings = project.id, ProjectSettings.model_validate(project.settings)

    events = build_events(random.Random(42), users, days)
    events.sort(key=lambda e: e["timestamp"])
    meta = ingestion.RequestMeta(ip=None, user_agent=None, country_header=None, received_at=datetime.now(UTC))
    accepted = 0
    for i in range(0, len(events), 400):
        async with sm() as db:
            res = await ingestion.ingest_events(db, pid, settings, events[i:i + 400], meta)
            await db.commit()
            accepted += res.accepted
            assert not res.rejected, res.rejected[:3]
    print(f"Seeded {accepted} events for {users} visitors over {days} days.")
    print(f"Login: {email}" + ("  (password from DEMO_PASSWORD)" if s.demo_password else "  (no DEMO_PASSWORD set: use the register page, or set DEMO_EMAIL/DEMO_PASSWORD and re-run)"))
    async with sm() as db:
        n = (await db.execute(select(func.count()).select_from(ApiKey).where(ApiKey.project_id == pid))).scalar_one()
    print(f"Project: Demo Web App ({pid})" + (f"\nAPI key (shown once): {raw_key}" if raw_key else f"\nProject already has {n} API key(s); create a new one in Settings -> API Keys."))
    await dispose_engines()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=300)
    ap.add_argument("--days", type=int, default=45)
    a = ap.parse_args()
    asyncio.run(run(a.users, a.days))


if __name__ == "__main__":
    main()
