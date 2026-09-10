"""Ownership, persistence and authentication boundaries of the unified HH runtime."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.browser import docker_runtime
from src.browser.docker_runtime import DockerBrowserRuntime, OWNER_LABEL, prepare_browser
from src.browser.session import SharedBrowserSession, save_storage_state
from src.config import BrowserConfig, Config


def test_native_runtime_owns_one_container_and_pins_only_loopback_ports(tmp_path, monkeypatch):
    config = Config()
    config.db.sqlite_path = str(tmp_path / "database/career_agent.db")
    runtime = DockerBrowserRuntime(config)
    runtime.docker = "docker"
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        value = "linux" if args[0] == "info" else "our-id" if args[0] == "run" else ""
        return SimpleNamespace(returncode=0, stdout=value, stderr="")

    healthy = {"Id": "our-id", "State": {"Running": True, "Health": {"Status": "healthy"}},
               "NetworkSettings": {"Ports": {"3000/tcp": [{"HostPort": "53121"}], "5900/tcp": [{"HostPort": "53122"}]}}}
    monkeypatch.setattr(runtime, "command", command)
    monkeypatch.setattr(runtime, "inspect", Mock(side_effect=[None, healthy, healthy]))
    monkeypatch.setattr(docker_runtime, "free_browser_ports", lambda: (53121, 53122))
    try:
        assert runtime.start() is runtime
        assert config.browser.endpoint == "ws://127.0.0.1:53121/hh"
        assert config.accounts.hh_vnc_host == "127.0.0.1"
        assert config.accounts.hh_vnc_port == 53122
        assert Path(config.browser.storage_state_path).parent == tmp_path / "database"
        run = next(call for call in calls if call[0] == "run")
        assert "127.0.0.1:53121:3000" in run and "127.0.0.1:53122:5900" in run
        assert f"{OWNER_LABEL}={runtime.identity}" in run
        assert "--memory" in run and "1024m" in run
        assert "--restart" not in run  # native exit owns lifecycle, not the Docker daemon
        duplicate = DockerBrowserRuntime(config)
        duplicate.docker = "docker"
        duplicate.command = Mock(side_effect=AssertionError("must fail before touching Docker"))
        with pytest.raises(RuntimeError, match="уже открыта"):
            duplicate.start()
    finally:
        runtime.stop()
    assert calls[-1] == ("stop", "--time", "20", "our-id")


def test_foreign_container_is_never_stopped(tmp_path, monkeypatch):
    config = Config()
    config.db.sqlite_path = str(tmp_path / "app.db")
    runtime = DockerBrowserRuntime(config)
    runtime._container = "foreign-id"
    command = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps([
        {"Id": "foreign-id", "Config": {"Labels": {OWNER_LABEL: "someone-else"}}}
    ])))
    monkeypatch.setattr(runtime, "command", command)
    with pytest.raises(RuntimeError, match="чужим контейнером"):
        runtime.stop()
    assert command.call_count == 1
    assert command.call_args.args[:2] == ("container", "inspect")


def test_compose_endpoint_never_starts_local_docker(monkeypatch):
    config = Config()
    config.browser.endpoint = "ws://hh-browser:3000/hh"
    monkeypatch.setattr(DockerBrowserRuntime, "start", Mock(side_effect=AssertionError("Compose owns startup")))
    assert prepare_browser(config) is None


async def test_idle_web_session_keeps_context_and_snapshots_after_last_lane(tmp_path, monkeypatch):
    shared = SharedBrowserSession()
    shared.keep_alive = True
    context = SimpleNamespace(storage_state=AsyncMock(return_value={"cookies": [], "origins": []}))
    browser = SimpleNamespace(is_connected=lambda: True, close=AsyncMock())

    async def connect(config):
        shared._config, shared._browser, shared._context = config, browser, context

    monkeypatch.setattr(shared, "_connect", connect)
    config = BrowserConfig(storage_state_path=str(tmp_path / "hh-session.json"))
    await shared.acquire(config)
    await shared.acquire(config)
    await shared.release()
    context.storage_state.assert_not_awaited()
    await shared.release()
    context.storage_state.assert_awaited_once_with(indexed_db=True)
    browser.close.assert_not_awaited()
    async with shared.connection(config) as (_, reused):
        assert reused is context
    await shared.close()
    browser.close.assert_awaited_once()


async def test_failed_snapshot_keeps_previous_login(tmp_path):
    path = tmp_path / "hh-session.json"
    path.write_text('{"cookies": ["previous"]}')
    context = SimpleNamespace(storage_state=AsyncMock(side_effect=RuntimeError("browser disconnected")))
    with pytest.raises(RuntimeError):
        await save_storage_state(context, path)
    assert json.loads(path.read_text()) == {"cookies": ["previous"]}
    assert not path.with_suffix(".pending.json").exists()


async def test_forget_closes_context_and_never_resaves_its_auth(tmp_path):
    shared = SharedBrowserSession()
    config = BrowserConfig(storage_state_path=str(tmp_path / "hh-session.json"))
    storage = Path(config.storage_state_path)
    storage.write_text('{"cookies":["old-login"]}')
    storage.with_suffix(".pending.json").write_text("pending login")
    context = SimpleNamespace(close=AsyncMock(), storage_state=AsyncMock())
    browser = SimpleNamespace(is_connected=lambda: True, close=AsyncMock())
    shared._config, shared._browser, shared._context = config, browser, context
    await shared.forget(config)
    await shared.close()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    context.storage_state.assert_not_awaited()
    assert not storage.exists()
    assert not storage.with_suffix(".pending.json").exists()
    assert not shared.is_connected


async def test_forget_cannot_race_an_active_browser_user(tmp_path):
    shared = SharedBrowserSession()
    shared._users = 1
    config = BrowserConfig(storage_state_path=str(tmp_path / "hh-session.json"))
    Path(config.storage_state_path).write_text("keep until job stops")
    with pytest.raises(RuntimeError, match="остановки"):
        await shared.forget(config)
    assert Path(config.storage_state_path).read_text() == "keep until job stops"
