from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://analytics:analytics@localhost:5432/analytics_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ["JWT_SECRET"] = "test-jwt-secret-test-jwt-secret-1234"
os.environ["API_KEY_SECRET"] = "test-api-key-secret-test-api-key-1234"
os.environ["DEFAULT_RATE_LIMIT_PER_SECOND"] = "100"

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.models  # noqa: E402,F401
from app.core.redis import close_redis, get_redis  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import dispose_engines, get_sessionmaker  # noqa: E402
from app.main import create_app  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Build the test schema with the real Alembic migrations (this also verifies them)."""
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, check=True, env=os.environ.copy())


@pytest_asyncio.fixture(autouse=True)
async def clean_state() -> AsyncIterator[None]:
    from app.services import ingestion

    ingestion._seen.clear()
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with get_sessionmaker()() as s:
        await s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await s.commit()
    await get_redis().flushdb()
    yield
    await close_redis()
    await dispose_engines()


@pytest.fixture
def app():  # type: ignore[no-untyped-def]
    return create_app()


def _csrf_hook(client_holder: dict) -> Callable:
    async def hook(request: httpx.Request) -> None:
        client = client_holder["c"]
        token = client.cookies.get("pa_csrf")
        if token and "x-csrf-token" not in request.headers:
            request.headers["x-csrf-token"] = token

    return hook


@pytest_asyncio.fixture
async def new_client(app) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:  # type: ignore[no-untyped-def]
    created: list[httpx.AsyncClient] = []

    def factory() -> httpx.AsyncClient:
        holder: dict = {}
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            event_hooks={"request": [_csrf_hook(holder)]},
        )
        holder["c"] = c
        created.append(c)
        return c

    yield factory
    for c in created:
        await c.aclose()


@pytest_asyncio.fixture
async def client(new_client) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return new_client()


PASSWORD = "Sup3rSecretPass"


async def register(c: httpx.AsyncClient, email: str, org: str | None = None, name: str = "Test") -> dict:
    r = await c.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, "name": name, "organization_name": org}
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest_asyncio.fixture
async def owner(client):  # type: ignore[no-untyped-def]
    """A logged-in owner with one org and one project (+ its raw API key)."""
    me = await register(client, "owner@example.com", "Acme")
    org_id = me["organizations"][0]["org_id"]
    r = await client.post("/api/v1/projects", json={"org_id": org_id, "name": "Web App"})
    assert r.status_code == 201, r.text
    proj = r.json()
    return {"client": client, "org_id": org_id, "project_id": proj["id"], "api_key": proj["api_key"]["key"], "me": me}


@pytest_asyncio.fixture
async def ingest(app):  # type: ignore[no-untyped-def]
    """Ingestion client helper (no cookies, API key auth)."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:

        async def send(api_key: str, events: list[dict], **extra) -> httpx.Response:  # type: ignore[no-untyped-def]
            return await c.post(
                "/api/v1/events/batch", json={"events": events, **extra}, headers={"Authorization": f"Bearer {api_key}"}
            )

        send.client = c  # type: ignore[attr-defined]
        yield send
