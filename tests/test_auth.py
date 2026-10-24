import pytest

from tests.conftest import PASSWORD, register


async def test_register_login_logout_flow(client, new_client):
    me = await register(client, "Alice@Example.com", "Acme Inc")
    assert me["user"]["email"] == "alice@example.com"  # normalised
    assert me["organizations"][0]["role"] == "owner"
    assert client.cookies.get("pa_session") and client.cookies.get("pa_csrf")

    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 200 and r.json()["csrf_token"] == client.cookies.get("pa_csrf")

    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 401  # session revoked server-side

    c2 = new_client()
    r = await c2.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert r.status_code == 200
    assert (await c2.get("/api/v1/auth/me")).status_code == 200


async def test_session_cookie_is_httponly_and_samesite(client):
    r = await client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": PASSWORD})
    cookies = {h.split("=")[0]: h for h in r.headers.get_list("set-cookie")}
    assert "HttpOnly" in cookies["pa_session"] and "SameSite=lax" in cookies["pa_session"]
    assert "HttpOnly" not in cookies["pa_csrf"]  # readable by JS for the double-submit header


async def test_duplicate_email_and_weak_password(client):
    await register(client, "dup@example.com")
    r = await client.post("/api/v1/auth/register", json={"email": "DUP@example.com", "password": PASSWORD})
    assert r.status_code == 409 and r.json()["error"]["code"] == "EMAIL_TAKEN"
    r = await client.post("/api/v1/auth/register", json={"email": "x@example.com", "password": "short"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_login_errors_do_not_reveal_which_part_was_wrong(client, new_client):
    await register(client, "real@example.com")
    c = new_client()
    r1 = await c.post("/api/v1/auth/login", json={"email": "real@example.com", "password": "wrongPassw0rd"})
    r2 = await c.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrongPassw0rd"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["error"]["message"] == r2.json()["error"]["message"]


async def test_csrf_required_for_unsafe_methods(client):
    await register(client, "csrf@example.com", "Org")
    r = await client.post("/api/v1/organizations", json={"name": "X"}, headers={"x-csrf-token": "bad"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"
    r = await client.post("/api/v1/organizations", json={"name": "X"})  # hook adds the valid token
    assert r.status_code == 201


async def test_unauthenticated_access_is_rejected(client):
    for path in ("/api/v1/projects", "/api/v1/organizations", "/api/v1/auth/me"):
        r = await client.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


async def test_login_is_rate_limited(client, new_client):
    await register(client, "rl@example.com")
    c = new_client()
    codes = []
    for _ in range(45):  # > 2 x limit so a fixed-window boundary cannot hide the 429
        r = await c.post("/api/v1/auth/login", json={"email": "rl@example.com", "password": "wrongPassw0rd"})
        codes.append(r.status_code)
    assert 429 in codes and "Retry-After" in r.headers


async def test_password_reset_flow(client, new_client, monkeypatch):
    from sqlalchemy import select

    from app.db.session import get_sessionmaker
    from app.models import EmailOutbox

    await register(client, "reset@example.com")
    r = await client.post("/api/v1/auth/password-reset/request", json={"email": "reset@example.com"})
    assert r.status_code == 202
    unknown = await client.post("/api/v1/auth/password-reset/request", json={"email": "ghost@example.com"})
    assert unknown.status_code == 202 and unknown.json() == r.json()  # no account enumeration

    async with get_sessionmaker()() as s:
        bodies = [
            m.body for m in (await s.execute(select(EmailOutbox).where(EmailOutbox.subject.like("Reset%")))).scalars()
        ]
    assert len(bodies) == 1
    token = bodies[0].split("token=")[1].split()[0]

    c2 = new_client()
    r = await c2.post("/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "BrandNewPass99"})
    assert r.status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 401  # old sessions revoked
    r = await c2.post("/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "AnotherPass123"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_TOKEN"  # single use
    r = await c2.post("/api/v1/auth/login", json={"email": "reset@example.com", "password": "BrandNewPass99"})
    assert r.status_code == 200


async def test_email_verification_flow(client):
    from sqlalchemy import select

    from app.db.session import get_sessionmaker
    from app.models import EmailOutbox

    me = await register(client, "verify@example.com")
    assert me["user"]["email_verified"] is False
    async with get_sessionmaker()() as s:
        body = (await s.execute(select(EmailOutbox).where(EmailOutbox.subject.like("Verify%")))).scalars().first().body
    token = body.split("token=")[1].split()[0]
    assert (await client.post("/api/v1/auth/verify-email/confirm", json={"token": token})).status_code == 200
    assert (await client.get("/api/v1/auth/me")).json()["user"]["email_verified"] is True


@pytest.mark.parametrize("path", ["/health", "/ready"])
async def test_health_endpoints(client, path):
    r = await client.get(path)
    assert r.status_code == 200
    assert r.headers["x-request-id"]
    assert r.headers["x-content-type-options"] == "nosniff"
