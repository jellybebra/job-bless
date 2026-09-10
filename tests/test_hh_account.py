"""Login verification and switching, without real accounts or network requests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.browser.account import HH_COOKIE_DOMAIN, LOGIN_SELECTORS, NAME_SELECTORS, read_hh_account
from src.config import BrowserConfig
from src.web import jobs


def account_page(*, name="Анна Петрова", visible=True, href="/applicant/settings", url="https://hh.ru/"):
    element = SimpleNamespace(
        is_visible=AsyncMock(return_value=visible),
        get_attribute=AsyncMock(return_value=href),
        inner_text=AsyncMock(return_value=name),
    )

    async def query(selector):
        return element if selector in (LOGIN_SELECTORS[0], NAME_SELECTORS[0]) else None

    return SimpleNamespace(url=url, query_selector=AsyncMock(side_effect=query))


@pytest.mark.parametrize("visible,href,url", [
    (False, "/applicant/settings", "https://hh.ru/"),
    (True, "/account/login", "https://hh.ru/"),
    (True, "/applicant/settings", "https://not-hh.ru/"),
    (True, "/applicant/settings", "https://hh.ru.example.com/"),
])
async def test_guest_or_unrelated_page_is_not_a_confirmed_login(visible, href, url):
    result = await read_hh_account(account_page(visible=visible, href=href, url=url))
    assert result == {"logged_in": False, "account_name": ""}


@pytest.mark.parametrize("name,expected", [
    ("Анна\n Петрова", "Анна Петрова"),
    ("Мой профиль", ""),
    ("Резюме\nОтклики", ""),
    ("", ""),
    ("Имя 123", ""),
])
async def test_account_name_is_optional_and_does_not_use_menu_labels(name, expected):
    assert await read_hh_account(account_page(name=name)) == {
        "logged_in": True, "account_name": expected,
    }


async def test_name_can_come_from_the_avatar_label():
    page = account_page(name="")
    element = await page.query_selector(LOGIN_SELECTORS[0])
    element.get_attribute.side_effect = lambda name: "Анна Петрова" if name == "aria-label" else None
    assert (await read_hh_account(page))["account_name"] == "Анна Петрова"


async def test_current_hh_header_uses_authenticated_state_and_profile_name():
    page = account_page()
    page.evaluate = AsyncMock(return_value={"user_type": "applicant", "name": "Анна Петрова"})
    page.query_selector = AsyncMock(return_value=None)
    assert await read_hh_account(page) == {"logged_in": True, "account_name": "Анна Петрова"}
    page.query_selector.assert_not_awaited()


@pytest.mark.parametrize("user_type", ["anonymous", "employer"])
async def test_anonymous_or_employer_state_is_not_an_applicant_login(user_type):
    page = account_page()
    page.evaluate = AsyncMock(return_value={"user_type": user_type, "name": "Старое имя"})
    assert await read_hh_account(page) == {"logged_in": False, "account_name": ""}


async def test_current_header_dom_is_a_fallback_when_state_is_unavailable():
    page = account_page(name="Резюме и профиль", href="/applicant/profile/me")
    page.evaluate = AsyncMock(return_value=None)
    assert await read_hh_account(page) == {"logged_in": True, "account_name": ""}


async def test_automatic_login_finishes_without_manual_confirmation(monkeypatch):
    async def wait_for_user(**kwargs):
        await asyncio.Event().wait()

    ctx = SimpleNamespace(wait_for_confirmation=AsyncMock(side_effect=wait_for_user),
                          raise_if_stopped=Mock())
    identity = {"logged_in": True, "account_name": "Анна Петрова"}
    monkeypatch.setattr(jobs, "read_hh_account", AsyncMock(return_value=identity))
    page = SimpleNamespace(goto=AsyncMock())
    assert await asyncio.wait_for(jobs._wait_for_hh_login(ctx, page), 1) == identity
    page.goto.assert_not_awaited()


async def test_manual_confirmation_is_not_proof_of_login(monkeypatch):
    monkeypatch.setattr(jobs, "read_hh_account", AsyncMock(return_value={"logged_in": False, "account_name": ""}))
    ctx = SimpleNamespace(wait_for_confirmation=AsyncMock(return_value=True), raise_if_stopped=Mock())
    page = SimpleNamespace(goto=AsyncMock(), locator=Mock(return_value=SimpleNamespace(
        first=SimpleNamespace(wait_for=AsyncMock()),
    )))
    with pytest.raises(ValueError, match="Вход в hh.ru не подтверждён"):
        await jobs._wait_for_hh_login(ctx, page)


async def test_stopping_login_cancels_confirmation_watcher(monkeypatch):
    finished = asyncio.Event()

    async def wait_for_user(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(jobs, "read_hh_account", AsyncMock(return_value={"logged_in": False, "account_name": ""}))
    ctx = SimpleNamespace(wait_for_confirmation=AsyncMock(side_effect=wait_for_user), raise_if_stopped=Mock())
    task = asyncio.create_task(jobs._wait_for_hh_login(ctx, Mock()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.parametrize("switch_account", [False, True])
@pytest.mark.parametrize("login_succeeds", [False, True])
async def test_switch_keeps_original_session_until_verified(
    monkeypatch, tmp_path, switch_account, login_succeeds,
):
    page = SimpleNamespace(url="about:blank", goto=AsyncMock(), close=AsyncMock(), wait_for_load_state=AsyncMock())
    context = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=page), clear_cookies=AsyncMock(),
                              storage_state=AsyncMock(return_value={'cookies': [], 'origins': []}))
    isolated = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=page), close=AsyncMock())
    browser = SimpleNamespace(contexts=[context], new_context=AsyncMock(return_value=isolated))
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
    manager = AsyncMock()
    manager.__aenter__.return_value = pw
    monkeypatch.setattr(jobs, "async_playwright", Mock(return_value=manager))
    monkeypatch.setattr(jobs, "ensure_browser", AsyncMock(return_value=BrowserConfig()))
    monkeypatch.setattr(jobs, "STORAGE_STATE_PATH", str(tmp_path / "storage.json"))
    importer = AsyncMock(return_value={"found": 2, "imported": 2, "failed": 0})
    monkeypatch.setattr(jobs, "resume_import_job", importer)
    identity = {"logged_in": True, "account_name": "Анна Петрова"}
    commit = AsyncMock(return_value=identity)
    monkeypatch.setattr(jobs, '_commit_hh_account', commit)
    backup = tmp_path / 'storage.json'
    backup.write_text('old session')

    async def verify(ctx, tab):
        context.clear_cookies.assert_not_awaited()
        context.storage_state.assert_not_awaited()
        assert backup.read_text() == 'old session'
        if not login_succeeds:
            raise ValueError("Не удалось подтвердить вход")
        return identity

    monkeypatch.setattr(jobs, "_wait_for_hh_login", verify)
    ctx = SimpleNamespace(state=SimpleNamespace(params={"switch_account": switch_account}),
                          log=Mock(), raise_if_stopped=Mock())
    if login_succeeds:
        result = await jobs.login_job(ctx)
        assert result["account_name"] == "Анна Петрова"
        assert result["logged_in"] is True
        assert result["resume_import"]["imported"] == 2
        importer.assert_awaited_once_with(ctx)
    else:
        with pytest.raises(ValueError):
            await jobs.login_job(ctx)
        importer.assert_not_awaited()
    if login_succeeds:
        page.close.assert_not_awaited()
    elif not switch_account:
        page.close.assert_awaited_once()
    context.clear_cookies.assert_not_awaited()
    assert isolated.close.await_count == int(switch_account)
    assert commit.await_count == int(switch_account and login_succeeds)
    assert context.storage_state.await_count == int(not switch_account and login_succeeds)
    if not login_succeeds:
        assert backup.read_text() == 'old session'


@pytest.mark.parametrize("already_logged_in", [False, True])
async def test_existing_hh_tab_is_reused_without_closing_it(monkeypatch, tmp_path, already_logged_in):
    from unittest.mock import MagicMock

    page = MagicMock()
    page.url = "https://hh.ru/"
    page.is_closed.return_value = False
    page.goto = AsyncMock()
    page.close = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    unrelated = MagicMock()
    unrelated.url = "https://example.com/"
    unrelated.is_closed.return_value = False
    context = SimpleNamespace(pages=[page, unrelated], new_page=AsyncMock(),
                              storage_state=AsyncMock(return_value={'cookies': [], 'origins': []}))
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(
        return_value=SimpleNamespace(contexts=[context]),
    )))
    manager = AsyncMock()
    manager.__aenter__.return_value = pw
    monkeypatch.setattr(jobs, "async_playwright", Mock(return_value=manager))
    monkeypatch.setattr(jobs, "ensure_browser", AsyncMock(return_value=BrowserConfig()))
    monkeypatch.setattr(jobs, "STORAGE_STATE_PATH", str(tmp_path / "storage.json"))
    identity = {"logged_in": True, "account_name": "Анна Петрова"}
    monkeypatch.setattr(jobs, "read_hh_account", AsyncMock(return_value=identity if already_logged_in else {"logged_in": False}))
    waiting = AsyncMock(return_value=identity)
    monkeypatch.setattr(jobs, "_wait_for_hh_login", waiting)
    ctx = SimpleNamespace(state=SimpleNamespace(params={}), log=Mock(), raise_if_stopped=Mock())
    assert (await jobs.login_job(ctx))["logged_in"] is True
    context.new_page.assert_not_awaited()
    page.close.assert_not_awaited()
    context.storage_state.assert_awaited_once()
    if already_logged_in:
        page.goto.assert_not_awaited()
        waiting.assert_not_awaited()
    else:
        page.goto.assert_awaited_once()
        waiting.assert_awaited_once()


def test_switch_cookie_filter_keeps_unrelated_sites():
    for domain in ("hh.ru", ".hh.ru", "spb.hh.ru", "hh.kz", ".rabota.by"):
        assert HH_COOKIE_DOMAIN.search(domain)
    for domain in ("example.com", "not-hh.ru", "hh.ru.example.com"):
        assert not HH_COOKIE_DOMAIN.search(domain)


@pytest.mark.parametrize('failure', ['none', 'cancel', 'unverified', 'save', 'stop_during_save'])
async def test_switch_commit_verifies_cookies_and_rolls_back(monkeypatch, tmp_path, failure):
    old = {'domain': '.hh.ru', 'name': 'session', 'value': 'old'}
    new = {'domain': '.hh.ru', 'name': 'session', 'value': 'new'}
    other = {'domain': 'example.com', 'name': 'session', 'value': 'unrelated'}
    source = SimpleNamespace(cookies=AsyncMock(return_value=[new, other]))
    page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock())
    target = SimpleNamespace(cookies=AsyncMock(return_value=[old, other]),
                             clear_cookies=AsyncMock(), add_cookies=AsyncMock(),
                             new_page=AsyncMock(return_value=page),
                             storage_state=AsyncMock(return_value={'cookies': [new, other]}))
    path = tmp_path / 'session.json'
    path.write_text('original backup')
    ctx = SimpleNamespace(raise_if_stopped=Mock(), state=SimpleNamespace(result={}), log=Mock())
    identity = {'logged_in': True, 'account_name': 'Новый Аккаунт'}
    monkeypatch.setattr(jobs, 'read_hh_account', AsyncMock(return_value=identity))
    if failure == 'cancel':
        page.goto.side_effect = asyncio.CancelledError
    elif failure == 'unverified':
        jobs.read_hh_account.return_value = {'logged_in': False, 'account_name': ''}
    elif failure == 'save':
        target.storage_state.side_effect = OSError('failed to save')
    elif failure == 'stop_during_save':
        ctx.raise_if_stopped.side_effect = [None, None, asyncio.CancelledError()]
    if failure == 'none':
        assert await jobs._commit_hh_account(ctx, source, target, path) == identity
        assert 'new' in path.read_text()
        assert target.add_cookies.await_args_list[0].args == ([new],)
        assert target.add_cookies.await_count == 1
    else:
        with pytest.raises((asyncio.CancelledError, ValueError, OSError)):
            await jobs._commit_hh_account(ctx, source, target, path)
        assert target.add_cookies.await_args_list[-1].args == ([old],)
        assert path.read_text() == 'original backup'
    for call in target.clear_cookies.await_args_list:
        assert call.kwargs == {'domain': HH_COOKIE_DOMAIN}
    page.close.assert_awaited_once()
    assert not path.with_suffix('.pending.json').exists()


async def test_cancel_before_new_login_keeps_original_profile(monkeypatch, tmp_path):
    page = SimpleNamespace(url='about:blank', goto=AsyncMock(), close=AsyncMock())
    original = SimpleNamespace(pages=[], clear_cookies=AsyncMock(), storage_state=AsyncMock())
    isolated = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=page), close=AsyncMock())
    browser = SimpleNamespace(contexts=[original], new_context=AsyncMock(return_value=isolated))
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
    manager = AsyncMock()
    manager.__aenter__.return_value = pw
    monkeypatch.setattr(jobs, 'async_playwright', Mock(return_value=manager))
    monkeypatch.setattr(jobs, 'ensure_browser', AsyncMock(return_value=BrowserConfig()))
    monkeypatch.setattr(jobs, 'STORAGE_STATE_PATH', str(tmp_path / 'session.json'))
    monkeypatch.setattr(jobs, '_wait_for_hh_login', AsyncMock(side_effect=asyncio.CancelledError))
    ctx = SimpleNamespace(state=SimpleNamespace(params={'switch_account': True}), log=Mock(), raise_if_stopped=Mock())
    with pytest.raises(asyncio.CancelledError):
        await jobs.login_job(ctx)
    isolated.close.assert_awaited_once()
    original.clear_cookies.assert_not_awaited()
    original.storage_state.assert_not_awaited()
