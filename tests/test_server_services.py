"""Boundaries that differ from the desktop runtime: private API and persistence."""

import asyncio
import hashlib
import sqlite3
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from scripts.configure_server import copy_database
from src.aistudio.remote import RemoteAIStudioRuntime
from src.aistudio.runtime import AIStudioRuntime, RuntimeErrorWithHint
from src.aistudio.service import RuntimeSettings, create_service
from src.config import Config
from src.browser.connection import connect_browser
from types import SimpleNamespace
from src.config import BrowserConfig


def test_database_copy_preserves_history_and_disables_unverified_accounts(tmp_path):
    source, target = tmp_path / "local.db", tmp_path / "server.db"
    with sqlite3.connect(source) as database:
        database.executescript("""
            CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE vacancies(id INTEGER PRIMARY KEY, title TEXT);
            INSERT INTO vacancies VALUES (1, 'Existing vacancy');
            INSERT INTO app_settings VALUES ('schedule.enabled', 'true');
        """)
    before = hashlib.sha256(source.read_bytes()).digest()
    copy_database(source, target)
    assert hashlib.sha256(source.read_bytes()).digest() == before
    with sqlite3.connect(target) as database:
        settings = dict(database.execute("SELECT key,value FROM app_settings"))
        assert database.execute("SELECT title FROM vacancies").fetchone()[0] == "Existing vacancy"
        assert settings["schedule.enabled"] == "false"
        assert settings["hh.login_required"] == "true"
    with pytest.raises(ValueError, match="уже существует"):
        copy_database(source, target)


@pytest.mark.asyncio
async def test_hh_connects_only_to_camoufox():
    firefox = SimpleNamespace(connect=AsyncMock(return_value="browser"))
    config = BrowserConfig(endpoint="ws://hh-browser:3000/hh", timeout_ms=12345)
    assert await connect_browser(SimpleNamespace(firefox=firefox), config) == "browser"
    firefox.connect.assert_awaited_once_with(config.endpoint, timeout=12345)


def remote_runtime():
    settings = RuntimeSettings()
    settings.config = Config()
    settings.config.accounts.google_url = "http://private-google:7860"
    settings.config.accounts.google_key = "private-key-do-not-send-to-the-ui"
    settings.values["llm.model"] = "chosen-model"
    return RemoteAIStudioRuntime(settings, Mock())


@pytest.mark.asyncio
@pytest.mark.parametrize("code, message", [("region_unsupported", "Region not supported"), ("", "Не удалось открыть")])
async def test_google_startup_failure_is_reported_without_waiting_for_timeout(code, message):
    runtime = AIStudioRuntime(RuntimeSettings(), Mock())
    runtime._server = Mock()
    runtime._server.poll.return_value = None
    runtime._read_status = Mock(return_value={"connected": False, "error": True, "error_code": code})
    with pytest.raises(RuntimeErrorWithHint, match=message):
        await asyncio.wait_for(runtime._wait_for_server(), timeout=.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason, succeeds", [("length", True), ("stop", False)])
async def test_google_probe_retries_only_an_exhausted_answer_budget(monkeypatch, finish_reason, succeeds):
    import json
    from src.aistudio import runtime as runtime_module
    runtime = AIStudioRuntime(RuntimeSettings(), Mock())
    runtime._endpoint = "http://private-test"
    limits = []

    def respond(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemini-flash-latest"}]})
        limits.append(json.loads(request.content)["max_tokens"])
        content = "OK" if len(limits) == 2 else ""
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]})

    client = httpx.AsyncClient
    monkeypatch.setattr(runtime_module.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    if succeeds:
        assert await runtime._probe() == "gemini-flash-latest"
        assert limits == [64, 1024]
    else:
        with pytest.raises(RuntimeErrorWithHint, match="пустой ответ"):
            await runtime._probe()
        assert limits == [64]


@pytest.mark.asyncio
async def test_remote_connect_only_enables_llm_after_service_verified_model():
    runtime = remote_runtime()
    runtime._request = AsyncMock(side_effect=[{}, {"state": "ready", "model": "chosen-model", "models": []}])
    try:
        await runtime._connect(login=True)
        assert runtime.settings.managed_llm["api_key"] == runtime._key
        assert runtime.settings.get("llm.enabled") == "1"
        assert runtime._request.call_args_list[0].kwargs["json"] == {"login": True, "model": "chosen-model"}
        assert runtime._key not in str(runtime.snapshot())
    finally:
        runtime._request = AsyncMock(return_value={})
        await runtime.stop()


@pytest.mark.asyncio
async def test_remote_failure_does_not_save_a_connected_account():
    runtime = remote_runtime()
    runtime._request = AsyncMock(side_effect=RuntimeErrorWithHint("service unavailable"))
    await runtime._connect(login=False)
    assert runtime.state == "error"
    assert runtime.settings.managed_llm is None
    assert not runtime.settings.get("llm.enabled", False)


@pytest.mark.asyncio
async def test_remote_watcher_resumes_selected_service_after_restart():
    runtime = remote_runtime()
    runtime.settings.values.update({"llm.connection": "aistudio", "llm.enabled": True})
    runtime._request = AsyncMock(return_value={"state": "idle"})
    restarted = asyncio.Event()
    runtime.begin = Mock(side_effect=restarted.set)
    watcher = asyncio.create_task(runtime._watch())
    try:
        await asyncio.wait_for(restarted.wait(), 7)
        runtime.begin.assert_called_once_with()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


@pytest.mark.parametrize("url", ["https://hh.ru/check-captcha", "https://hh.ru/account/login", "https://hh.kz/403"])
@pytest.mark.asyncio
async def test_hh_guard_surfaces_manual_intervention(url):
    from src.collector.page_guard import HHPageGuard
    from src.browser.intervention import HHInterventionRequired
    with pytest.raises(HHInterventionRequired):
        await HHPageGuard().check_page_state(Mock(url=url))


@pytest.mark.asyncio
async def test_google_streams_are_serialized_and_cannot_be_stopped_mid_request():
    first_started, release_first = asyncio.Event(), asyncio.Event()
    requests = []

    async def upstream(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        assert b"Authorization: Bearer engine-key" in headers
        requests.append(headers)
        if len(requests) == 1:
            first_started.set()
            await release_first.wait()
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    runtime = Mock(available=True, busy=False, models=[], _key="engine-key")
    runtime._endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    runtime.snapshot.return_value = {"state": "ready"}
    runtime.stop = AsyncMock()
    key = "private-service-key-123456789"
    app = create_service(key=key, runtime=runtime)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://service") as client:
            assert (await client.get("/control/status")).status_code == 401
            assert (await client.get("/health")).status_code == 200
            client.headers["Authorization"] = f"Bearer {key}"
            assert (await client.get("/v1/anything-else")).status_code == 404
            first = asyncio.create_task(client.post("/v1/chat/completions", json={"stream": True}))
            await asyncio.wait_for(first_started.wait(), 3)
            second = asyncio.create_task(client.get("/v1/models"))
            assert (await client.post("/control/stop")).status_code == 409
            assert (await client.post("/control/connect", json={"login": True})).status_code == 409
            await asyncio.sleep(0.02)
            assert len(requests) == 1
            release_first.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert all(result.json() == {"ok": True} for result in results)
            assert len(requests) == 2
            assert (await client.post("/control/stop")).status_code == 200
            runtime.stop.assert_awaited_once()
    finally:
        release_first.set()
        server.close()
        await server.wait_closed()
