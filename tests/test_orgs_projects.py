import pytest

from tests.conftest import register


async def _add(owner_ctx, new_client, email, role):
    c = new_client()
    await register(c, email)
    r = await owner_ctx["client"].post(
        f"/api/v1/organizations/{owner_ctx['org_id']}/members", json={"email": email, "role": role}
    )
    assert r.status_code == 201, r.text
    return c


async def test_project_creation_returns_key_once_and_stores_hash(owner):
    c, pid = owner["client"], owner["project_id"]
    assert owner["api_key"].startswith("phc_")
    keys = (await c.get(f"/api/v1/projects/{pid}/api-keys")).json()
    assert len(keys) == 1 and "key" not in keys[0] and keys[0]["key_prefix"] == owner["api_key"][:10]
    from sqlalchemy import select

    from app.db.session import get_sessionmaker
    from app.models import ApiKey

    async with get_sessionmaker()() as s:
        row = (await s.execute(select(ApiKey))).scalars().one()
    assert owner["api_key"] not in (row.key_hash, row.key_prefix) and len(row.key_hash) == 64


async def test_api_key_lifecycle_revoke_and_rotate(owner, ingest):
    c, pid = owner["client"], owner["project_id"]
    ev = [{"event": "e", "distinct_id": "u1"}]
    assert (await ingest(owner["api_key"], ev)).status_code == 200

    new = (
        await c.post(
            f"/api/v1/projects/{pid}/api-keys/{(await c.get(f'/api/v1/projects/{pid}/api-keys')).json()[0]['id']}/rotate",
            json={"grace_period_hours": 0},
        )
    ).json()
    assert new["key"] != owner["api_key"]
    assert (await ingest(owner["api_key"], ev)).status_code == 401  # old key revoked immediately
    assert (await ingest(new["key"], ev)).status_code == 200
    keys = (await c.get(f"/api/v1/projects/{pid}/api-keys")).json()
    assert sum(1 for k in keys if k["revoked_at"]) == 1
    assert next(k for k in keys if k["id"] == new["id"])["last_used_at"] is not None

    assert (await c.delete(f"/api/v1/projects/{pid}/api-keys/{new['id']}")).status_code == 200
    assert (await ingest(new["key"], ev)).status_code == 401


async def test_rotation_with_grace_period_keeps_old_key_working(owner, ingest):
    c, pid = owner["client"], owner["project_id"]
    kid = (await c.get(f"/api/v1/projects/{pid}/api-keys")).json()[0]["id"]
    await c.post(f"/api/v1/projects/{pid}/api-keys/{kid}/rotate", json={"grace_period_hours": 24})
    assert (await ingest(owner["api_key"], [{"event": "e", "distinct_id": "u"}])).status_code == 200


async def test_role_permissions_are_enforced_server_side(owner, new_client):
    org, pid = owner["org_id"], owner["project_id"]
    admin = await _add(owner, new_client, "admin@example.com", "admin")
    member = await _add(owner, new_client, "member@example.com", "member")
    viewer = await _add(owner, new_client, "viewer@example.com", "viewer")

    # everyone in the org can read the project
    for c in (admin, member, viewer):
        assert (await c.get(f"/api/v1/projects/{pid}")).status_code == 200
    # API keys / settings are admin+
    assert (await admin.get(f"/api/v1/projects/{pid}/api-keys")).status_code == 200
    for c in (member, viewer):
        assert (await c.get(f"/api/v1/projects/{pid}/api-keys")).status_code == 403
        assert (await c.patch(f"/api/v1/projects/{pid}", json={"name": "x"})).status_code == 403
        assert (await c.post("/api/v1/projects", json={"org_id": org, "name": "P2"})).status_code == 403
    assert (await admin.patch(f"/api/v1/projects/{pid}", json={"name": "Renamed"})).status_code == 200
    # only owners delete projects/orgs
    assert (await admin.delete(f"/api/v1/projects/{pid}")).status_code == 403
    assert (await admin.delete(f"/api/v1/organizations/{org}")).status_code == 403
    # admins cannot grant admin/owner
    r = await admin.post(f"/api/v1/organizations/{org}/members", json={"email": "owner@example.com", "role": "admin"})
    assert r.status_code == 403


async def test_non_members_cannot_see_projects_or_orgs(owner, new_client):
    outsider = new_client()
    await register(outsider, "outsider@example.com", "Other")
    pid, org = owner["project_id"], owner["org_id"]
    assert (await outsider.get(f"/api/v1/projects/{pid}")).status_code == 404  # existence not leaked
    assert (await outsider.get(f"/api/v1/organizations/{org}/members")).status_code == 404
    assert (await outsider.get(f"/api/v1/projects/{pid}/api-keys")).status_code == 404
    assert (await outsider.get("/api/v1/projects")).json() == []
    r = await outsider.post("/api/v1/projects", json={"org_id": org, "name": "hax"})
    assert r.status_code == 404


async def test_last_owner_cannot_be_removed_or_demoted(owner):
    c, org = owner["client"], owner["org_id"]
    uid = owner["me"]["user"]["id"]
    assert (await c.patch(f"/api/v1/organizations/{org}/members/{uid}", json={"role": "member"})).status_code == 409
    assert (await c.delete(f"/api/v1/organizations/{org}/members/{uid}")).status_code == 409


async def test_invitation_applied_on_registration(owner, new_client):
    c, org = owner["client"], owner["org_id"]
    r = await c.post(f"/api/v1/organizations/{org}/members", json={"email": "newbie@example.com", "role": "viewer"})
    assert r.json()["status"] == "invited"
    assert len((await c.get(f"/api/v1/organizations/{org}/invitations")).json()) == 1
    newbie = new_client()
    me = await register(newbie, "newbie@example.com")
    assert [(o["org_id"], o["role"]) for o in me["organizations"]] == [(org, "viewer")]
    assert (await c.get(f"/api/v1/organizations/{org}/invitations")).json() == []


async def test_project_settings_validation_and_patch(owner):
    c, pid = owner["client"], owner["project_id"]
    r = await c.patch(
        f"/api/v1/projects/{pid}",
        json={
            "settings": {
                "retention_days": 30,
                "retention_days_set": True,
                "ip_mode": "discard",
                "timezone": "Asia/Kolkata",
            }
        },
    )
    assert r.status_code == 200
    s = r.json()["settings"]
    assert s["retention_days"] == 30 and s["ip_mode"] == "discard" and s["timezone"] == "Asia/Kolkata"
    assert s["session_timeout_minutes"] == 30  # untouched
    bad = await c.patch(
        f"/api/v1/projects/{pid}", json={"settings": {"timezone": "Mars/Base", "retention_days_set": False}}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_SETTINGS"
    bad = await c.patch(
        f"/api/v1/projects/{pid}", json={"settings": {"retention_days": 13, "retention_days_set": True}}
    )
    assert bad.status_code == 422
    r = await c.patch(
        f"/api/v1/projects/{pid}", json={"settings": {"retention_days": None, "retention_days_set": True}}
    )
    assert r.json()["settings"]["retention_days"] is None


async def test_audit_log_records_sensitive_actions(owner):
    c, org, pid = owner["client"], owner["org_id"], owner["project_id"]
    created = (await c.post(f"/api/v1/projects/{pid}/api-keys", json={"name": "CI"})).json()
    logs = (await c.get(f"/api/v1/organizations/{org}/audit-logs")).json()
    actions = {entry["action"] for entry in logs}
    assert {"project.create", "api_key.create", "organization.create"} <= actions
    assert created["key"] not in str(logs) and owner["api_key"] not in str(logs)  # raw keys never land in the audit log


async def test_project_soft_delete_stops_ingestion(owner, ingest):
    c, pid = owner["client"], owner["project_id"]
    assert (await c.delete(f"/api/v1/projects/{pid}")).status_code == 200
    assert (await c.get(f"/api/v1/projects/{pid}")).status_code == 404
    assert (await ingest(owner["api_key"], [{"event": "e", "distinct_id": "u"}])).status_code == 401


@pytest.mark.parametrize("confirm,status", [("wrong", 422), ("Web App", 202)])
async def test_purge_requires_project_name_confirmation(owner, confirm, status):
    r = await owner["client"].post(f"/api/v1/projects/{owner['project_id']}/data/purge", json={"confirm_name": confirm})
    assert r.status_code == status
