from unittest.mock import AsyncMock
from src.browser import login_session


async def test_manual_login_uses_web_panel(monkeypatch):
    serve = AsyncMock()
    monkeypatch.setattr(login_session, "serve", serve)
    await login_session.run_manual_login("configs/config.yaml")
    assert serve.await_count == 1
    assert serve.await_args.args[0].browser.endpoint == ""
