import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import jwt
import pytest
from config import Settings
from cryptography.hazmat.primitives.asymmetric import rsa
from graph import Graph
from server import create_app
from source import Change, Source
from starlette.testclient import TestClient


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        tenant_id="11111111-1111-4111-8111-111111111111",
        api_client_id="22222222-2222-4222-8222-222222222222",
        spa_client_id="33333333-3333-4333-8333-333333333333",
        ingest_client_id="44444444-4444-4444-8444-444444444444",
        ingest_client_secret="test-only",
        alice_oid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        bob_oid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        eve_oid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        public_url="https://demo.example",
        database=tmp_path / "tickets.db",
    )


@pytest.fixture
def headers(settings, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, token: SimpleNamespace(key=key.public_key()),
    )

    def sign(user="alice", **overrides):
        claims = {
            "oid": str(getattr(settings, f"{user}_oid")),
            "tid": str(settings.tenant_id),
            "iss": f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0",
            "aud": str(settings.api_client_id),
            "scp": "Tickets.Access",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=10),
            **overrides,
        }
        return {
            "Authorization": f"Bearer {jwt.encode(claims, key, algorithm='RS256')}",
            "Accept": "application/json, text/event-stream",
        }

    return sign


@pytest.fixture
def indexed(monkeypatch):
    submitted = []

    async def index(self, tickets):
        submitted.extend(tickets)

    monkeypatch.setattr(Graph, "index", index)
    return submitted


@pytest.fixture
def client(settings, indexed):
    with TestClient(create_app(settings)) as client:
        yield client


def call(client, headers, name, args):
    response = client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def change(version=1):
    return {"expected_version": version, "body": "VPN-refresh-proof", "status": "resolved"}


@pytest.mark.parametrize(
    "user,ids",
    [("alice", ["T100", "T200", "T400"]), ("bob", ["T100", "T200"]), ("eve", ["T100", "T300"])],
)
def test_mcp_read_acls(client, headers, user, ids):
    data = call(client, headers(user), "search_tickets", {"query": "service desk"})
    assert [t["id"] for t in data["structuredContent"]["tickets"]] == ids
    if user == "eve":
        assert call(client, headers(user), "fetch_ticket", {"ticket_id": "T200"})["isError"]


def test_mcp_write_persists_audit_and_submits_index(client, headers, settings, indexed):
    data = call(client, headers(), "update_ticket", {"ticket_id": "T200", **change()})
    assert not data["isError"]
    assert data["structuredContent"]["source_update"] == "committed"
    assert data["structuredContent"]["index_update"] == "submitted"
    saved = Source(settings).fetch(str(settings.alice_oid), "T200")
    assert saved.version == 2 and saved.body == "VPN-refresh-proof"
    assert indexed[0] == saved
    audit = client.get("/audit", headers=headers()).json()
    assert audit[0]["actor"] == str(settings.alice_oid)
    detail = json.loads(audit[0]["detail"])
    assert detail["before"]["version"] == 1 and detail["after"]["version"] == 2
    assert call(client, headers(), "update_ticket", {"ticket_id": "T200", **change()})["isError"]


@pytest.mark.parametrize("user", ["bob", "eve"])
def test_write_denied_by_source_not_ui(client, headers, settings, indexed, user):
    result = call(client, headers(user), "update_ticket", {"ticket_id": "T200", **change()})
    assert result["isError"]
    assert Source(settings).fetch(str(settings.alice_oid), "T200").version == 1
    assert not indexed
    events = client.get("/audit", headers=headers()).json()
    assert events[0]["action"] == "denied"
    assert events[0]["actor"] == str(getattr(settings, f"{user}_oid"))
    if user == "eve":
        assert client.get("/audit", headers=headers(user)).json() == []


def test_index_failure_reports_partial_commit(client, headers, settings, monkeypatch):
    async def unavailable(self, tickets):
        raise RuntimeError("Graph HTTP 429")

    monkeypatch.setattr(Graph, "index", unavailable)
    data = call(client, headers(), "update_ticket", {"ticket_id": "T200", **change()})
    assert data["isError"]
    assert data["structuredContent"]["source_update"] == "committed"
    assert data["structuredContent"]["index_update"] == "failed"
    assert "429" in data["structuredContent"]["error"]
    assert Source(settings).fetch(str(settings.alice_oid), "T200").version == 2


def test_auth_rejects_impersonation_and_bad_tokens(client, headers):
    assert client.get("/me", headers={"X-Demo-User": "alice"}).status_code == 401
    for overrides in [
        {"aud": "wrong"},
        {"tid": "wrong"},
        {"scp": ""},
        {"exp": datetime.now(UTC) - timedelta(minutes=5)},
        {"iss": "https://wrong"},
    ]:
        assert client.get("/me", headers=headers(**overrides)).status_code == 401
    assert client.get("/me", headers=headers("bob")).status_code == 200


def test_audit_failure_rolls_back_record(settings):
    source = Source(settings)
    with source.db() as db:
        db.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON audit
                      BEGIN SELECT RAISE(ABORT, 'audit failed'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        source.update(str(settings.alice_oid), "T200", Change.model_validate(change()))
    assert source.fetch(str(settings.alice_oid), "T200").version == 1


def test_tools_do_not_accept_actor_or_acl_arguments(client, headers):
    data = call(
        client,
        headers("bob"),
        "update_ticket",
        {"ticket_id": "T200", **change(), "actor": "alice", "readers": ["everyone"]},
    )
    assert data["isError"]
    assert client.put("/tickets/T200", headers=headers(), json=change()).status_code == 404


async def test_graph_index_preserves_user_acl(settings, monkeypatch):
    requests = []
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "indexer-token"})
        if request.method == "GET":
            return (
                httpx.Response(200, json={"status": "completed"})
                if "/operations/" in request.url.path
                else httpx.Response(404)
            )
        if request.method == "PATCH":
            return httpx.Response(
                202,
                headers={
                    "Location": "https://graph.microsoft.com/v1.0/external/connections/"
                    "helpdeskdemo/operations/schema"
                },
            )
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    ticket = Source(settings).fetch(str(settings.alice_oid), "T200")
    await Graph(settings).provision()
    await Graph(settings).index([ticket])
    item = next(r for r in requests if r.method == "PUT")
    registered = json.loads(next(r for r in requests if r.method == "PATCH").content)
    payload = json.loads(item.content)
    assert {p["name"] for p in registered["properties"]} == set(payload["properties"])
    assert payload["acl"] == [
        {"type": "user", "value": str(settings.alice_oid), "accessType": "grant"},
        {"type": "user", "value": str(settings.bob_oid), "accessType": "grant"},
    ]
    assert payload["properties"]["version"] == ticket.version
    assert payload["content"]["value"] == f"{ticket.title}\n{ticket.body}"
    assert item.headers["Authorization"] == "Bearer indexer-token"
