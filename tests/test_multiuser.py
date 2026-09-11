"""Authentication and two independent workspaces, with no live Clerk or HH calls."""

import base64
import asyncio
from contextlib import AsyncExitStack
import json
import re
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from src.config import Config
from src.db.models import PageCommitParams, Resume, SearchRun, VacancyCard
from src.web.app import create_app
from src.web.clerk_auth import AuthenticationError, ClerkAuth, Identity, frontend_host
from src.web.gateway import create_gateway, worker_headers
from src.web.workspaces import Workspace, WorkspacePool

ORIGIN = "http://localhost:18743"
PUBLISHABLE = "pk_test_" + base64.b64encode(b"test.clerk.accounts.dev$").decode()


def request_with(token):
    return Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})


@pytest.fixture
def clerk():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="test-key", alg="RS256")
    status = {"status": "active", "user_id": "user_A"}

    def respond(request):
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [jwk]})
        if request.url.path == "/v1/sessions/sess_A":
            return httpx.Response(200, json=status)
        if request.url.path == "/v1/users/user_A":
            if status.get("user_deleted"):
                return httpx.Response(404)
            return httpx.Response(200, json={"id": "user_A", "primary_email_address_id": "email_A",
                "banned": status.get("user_banned", False),
                "email_addresses": [{"id": "email_A", "email_address": "OWNER@example.com",
                                     "verification": {"status": "verified"}}]})
        return httpx.Response(404)

    auth = ClerkAuth(PUBLISHABLE, "test-secret", ORIGIN, client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    def token(**changes):
        claims = dict(sub="user_A", sid="sess_A", iss=auth.issuer, exp=int(time.time())+60,
                      nbf=int(time.time())-2, iat=int(time.time())-2, azp=ORIGIN)
        claims.update(changes)
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test-key"})
    return auth, token, status


async def test_clerk_signed_identity_and_verified_email(clerk):
    auth, token, _ = clerk
    identity = await auth.authenticate(request_with(token()))
    assert identity == Identity("user_A", "sess_A", "OWNER@example.com", ("owner@example.com",))
    assert frontend_host(PUBLISHABLE) == "test.clerk.accounts.dev"
    await auth.close()


@pytest.mark.parametrize("changes", [
    {"exp": 1}, {"nbf": int(time.time())+600}, {"azp": "https://attacker.example"},
    {"iss": "https://other.clerk.accounts.dev"}, {"sub": "../../owner"},
    {"sid": "sess_A/../user_A"}, {"sts": "pending"}, {"sub": "user_B"},
])
async def test_invalid_or_other_session_rejected(clerk, changes):
    auth, token, _ = clerk
    with pytest.raises(AuthenticationError):
        await auth.authenticate(request_with(token(**changes)))
    await auth.close()


async def test_signature_and_revocation(clerk):
    auth, token, status = clerk
    forged = jwt.encode({"sub": "user_A"}, "attacker-secret" * 3, algorithm="HS256")
    with pytest.raises(AuthenticationError):
        await auth.authenticate(request_with(forged))
    await auth.authenticate(request_with(token()))
    status["status"] = "revoked"
    auth.sessions.clear()
    with pytest.raises(AuthenticationError):
        await auth.active_session("user_A", "sess_A")
    await auth.close()


async def test_deleted_or_banned_users_cannot_keep_background_workers(clerk):
    auth, _, state = clerk
    assert await auth.user_enabled("user_A")
    state["user_banned"] = True
    assert not await auth.user_enabled("user_A")
    state["user_deleted"] = True
    assert not await auth.user_enabled("user_A")
    await auth.close()


def test_only_verified_owner_receives_legacy_files_once(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "app").mkdir(parents=True)
    (legacy / "app" / "career_agent.db").write_text("owner database")
    for provider in ("hh", "google"):
        (legacy / provider).mkdir()
        (legacy / provider / "session").write_text("owner " + provider)
    pool = WorkspacePool(root=tmp_path, host_root="/private", image_prefix="image", image_tag="sha-test",
                         gateway_container="gateway", public_url=ORIGIN, publishable_key=PUBLISHABLE,
                         owner_email="owner@example.com", docker_client=MagicMock())
    stranger = pool._register(Identity("user_B", "sess_B", "other@example.com", ("other@example.com",)))
    assert not (tmp_path / stranger.key / "app" / "career_agent.db").exists()
    with pytest.raises(ValueError):
        pool._register(Identity("user_Fake", "sess_Fake", "owner@example.com", ()))
    owner = pool._register(Identity("user_A", "sess_A", "owner@example.com", ("owner@example.com",)))
    assert (tmp_path / owner.key / "app" / "career_agent.db").read_text() == "owner database"
    assert (tmp_path / owner.key / "hh" / "session").read_text() == "owner hh"
    replacement = pool._register(Identity("user_C", "sess_C", "owner@example.com", ("owner@example.com",)))
    assert not (tmp_path / replacement.key / "app" / "career_agent.db").exists()
    token = owner.token
    pool.close()
    again = WorkspacePool(root=tmp_path, host_root="/private", image_prefix="image", image_tag="sha-test",
                          gateway_container="gateway", public_url=ORIGIN, publishable_key=PUBLISHABLE,
                          docker_client=MagicMock())
    assert again.entries["user_A"].token == token
    again.close()


def test_untrusted_proxy_headers_are_replaced():
    identity = Identity("user_A", "sess_A", "", ())
    workspace = Workspace("user_A", "key", "real-token", "google")
    headers = worker_headers({"x-workspace-user": "user_B", "x-workspace-token": "forged",
                              "x-workspace-session": "sess_B", "authorization": "Bearer private-jwt",
                              "cookie": "__session=private-jwt", "x-forwarded-host": "evil", "origin": ORIGIN}, identity, workspace)
    assert headers["x-workspace-user"] == "user_A"
    assert headers["x-workspace-token"] == "real-token"
    assert headers["x-workspace-session"] == "sess_A"
    assert not {"authorization", "cookie", "x-forwarded-host"} & headers.keys()


@pytest.fixture
async def workspaces(tmp_path):
    workers, entries = {}, {}
    async with AsyncExitStack() as stack:
        for letter in ("A", "B"):
            config = Config()
            config.db.sqlite_path = str(tmp_path / f"{letter}.db")
            config.web.workspace_token = letter * 48
            config.web.workspace_user_id = "user_" + letter
            config.web.public_url = ORIGIN
            app = create_app(config)
            await stack.enter_async_context(app.router.lifespan_context(app))
            workers[letter] = app
            entries["user_" + letter] = Workspace("user_" + letter, letter.lower(), letter * 48, "google_" + letter)

        class TestAuth:
            host = "test.clerk.accounts.dev"
            async def authenticate(self, connection):
                letter = connection.headers.get("authorization", "").removeprefix("Bearer ")
                if letter not in workers:
                    raise AuthenticationError()
                return Identity("user_" + letter, "sess_" + letter, letter + "@example.com", ())
            async def active_session(self, *args): pass
            async def close(self): pass

        class TestPool:
            async def ensure(self, identity): return entries[identity.user_id]
            async def restore(self): pass
            async def reap_idle(self): pass
            async def running(self): return []
            async def wake(self, token, provider): raise PermissionError()
            def close(self): pass

        class WorkerTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                letter = request.url.host.split("-")[1].upper()
                return await httpx.ASGITransport(app=workers[letter]).handle_async_request(request)

        gateway = create_gateway(auth=TestAuth(), pool=TestPool(), public_url=ORIGIN,
                                 publishable_key=PUBLISHABLE, transport=WorkerTransport())
        await stack.enter_async_context(gateway.router.lifespan_context(gateway))
        client = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url=ORIGIN))
        yield client, workers


async def test_two_users_cannot_read_modify_or_export_each_others_data(workspaces):
    client, workers = workspaces
    for letter, app in workers.items():
        repo = app.state.repository
        await repo.upsert_resume(Resume(source_url="https://hh.ru/resume/same-id", title="Resume " + letter))
        await repo.create_search_run(SearchRun(id="same-run", task_id="same-run", search_url="https://hh.ru/search/vacancy"))
        await repo.commit_page_transaction(PageCommitParams(search_run_id="same-run", page_number=1, page_key="1",
            current_url="https://hh.ru/search/vacancy", canonical_url="https://hh.ru/search/vacancy",
            cards=[VacancyCard(source="hh", external_id="123", url="https://hh.ru/vacancy/123", title="Private vacancy " + letter)]))
    for letter, other in (("A", "B"), ("B", "A")):
        headers = {"authorization": "Bearer " + letter, "origin": ORIGIN,
                   "x-workspace-user": "user_" + other, "x-workspace-token": other * 48,
                   "x-workspace-session": "sess_" + other}
        for path in ("/vacancies", "/vacancies/export"):
            result = await client.get(path, headers=headers)
            assert result.status_code == 200, result.text
            assert "Private vacancy " + letter in result.text
            assert "Private vacancy " + other not in result.text
        page = await client.get("/actions", headers=headers)
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        result = await client.post("/actions/search-settings", headers=headers,
                                   data={"csrf_token": csrf, "search_query": "Only " + letter})
        assert result.status_code == 200
        assert workers[letter].state.settings.search_query == "Only " + letter
        assert workers[other].state.settings.search_query != "Only " + letter
        other_csrf = workers[other].state.auth.session(Request({"type": "http", "headers": [
            (b"x-workspace-token", (other*48).encode()), (b"x-workspace-user", ("user_"+other).encode()),
            (b"x-workspace-session", ("sess_"+other).encode())]})).csrf
        denied = await client.post("/actions/search-settings", headers=headers,
                                   data={"csrf_token": other_csrf, "search_query": "Bad"})
        assert denied.status_code == 403
    assert (await client.get("/vacancies", follow_redirects=False)).status_code == 303
    assert (await client.get("/internal/idle-status", headers={"authorization": "Bearer A"})).status_code == 404


async def test_workers_reject_direct_requests_and_keep_tasks_separate(workspaces):
    _, workers = workspaces
    for letter, app in workers.items():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            assert (await client.get("/vacancies")).status_code == 401
            assert (await client.get("/remote-static/core/rfb.js")).status_code == 401
            assert (await client.get("/healthz")).status_code == 200
    queue_a = workers["A"].state.tasks.subscribe()
    queue_b = workers["B"].state.tasks.subscribe()
    workers["A"].state.tasks.publish({"private": "A"})
    assert queue_a.get_nowait() == {"private": "A"}
    assert queue_b.empty()
    assert workers["A"].state.screens is not workers["B"].state.screens


async def test_slow_workspace_does_not_block_another_user(tmp_path, monkeypatch):
    pool = WorkspacePool(root=tmp_path, host_root="/private", image_prefix="image", image_tag="sha-test",
                         gateway_container="gateway", public_url=ORIGIN, publishable_key=PUBLISHABLE,
                         docker_client=MagicMock())
    started, finish = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(pool, "_start", lambda *args: None)
    async def ready(item, service):
        if item.user_id == "user_A":
            started.set()
            await finish.wait()
    monkeypatch.setattr(pool, "_ready", ready)
    first = asyncio.create_task(pool.ensure(Identity("user_A", "sess_A", "", ())))
    try:
        await asyncio.wait_for(started.wait(), 2)
        second = await asyncio.wait_for(pool.ensure(Identity("user_B", "sess_B", "", ())), 2)
        assert second.user_id == "user_B"
        assert not first.done()
    finally:
        finish.set()
        await first
        pool.close()


async def test_private_revocation_only_closes_that_sessions_screens(workspaces):
    gateway, workers = workspaces
    app = workers["A"]
    lease = app.state.screens.grant("hh", "sess_A")
    other = workers["B"].state.screens.grant("hh", "sess_B")
    denied = await gateway.post("/internal/revoke-session", json={"session_id": "sess_B"},
                                headers={"authorization": "Bearer A", "origin": ORIGIN})
    assert denied.status_code == 404
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        denied = await client.post("/internal/revoke-session", json={"session_id": "sess_A"})
        assert denied.status_code == 401
        done = await client.post("/internal/revoke-session", json={"session_id": "sess_A"},
                                 headers={"x-workspace-token": "A"*48})
        assert done.status_code == 200
        assert lease.revoked.is_set()
        assert not other.revoked.is_set()
        assert workers["B"].state.screens.active("hh")
        await app.state.settings.save({"schedule.enabled": "1"}, keys={"schedule.enabled"})
        status = await client.get("/internal/idle-status", headers={"x-workspace-token": "A"*48})
        assert status.json()["scheduled"] is True


@pytest.mark.parametrize("status", ["running", "exited"])
def test_recreated_gateway_rejoins_existing_worker_network(tmp_path, status):
    engine = MagicMock()
    image = "image-app:sha-test"
    engine.images.get.return_value.id = "sha256:current"
    worker = SimpleNamespace(attrs={"Config": {"Image": image}, "Image": "sha256:current"},
                             status=status, reload=MagicMock(), start=MagicMock())
    gateway = SimpleNamespace(id="new-gateway-id")
    engine.containers.get.side_effect = lambda name: gateway if name == "gateway" else worker
    network = engine.networks.get.return_value
    network.attrs = {"Containers": {"old-gateway-id": {}, "worker-id": {}}}
    pool = WorkspacePool(root=tmp_path, host_root="/private", image_prefix="image", image_tag="sha-test",
                         gateway_container="gateway", public_url=ORIGIN, publishable_key=PUBLISHABLE,
                         docker_client=engine)
    item = pool._register(Identity("user_A", "sess_A", "", ()))
    pool._start(item, "app")
    network.connect.assert_called_once_with(gateway, aliases=["workspace-gateway"])
    engine.containers.create.assert_not_called()
    assert worker.start.call_count == (0 if status == "running" else 1)
    pool.close()
