"""Exercise real pagination without making requests to hh.ru."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.collector import collector as module
from src.collector.collector import HHVacancyCardCollector
from src.config import BrowserConfig, ScrollerConfig, Config
from src.db.models import PageCommitParams, CollectionSummary, VacancyCard


@pytest.fixture
def search(monkeypatch):
    page = SimpleNamespace(url='', number=1, count=13)
    async def goto(url, **kwargs):
        page.url = url
    async def click():
        page.number += 1
        page.url = f'https://hh.ru/search/vacancy?page={page.number}'
    button = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                             is_enabled=AsyncMock(return_value=True), click=AsyncMock(side_effect=click))
    async def query(selector):
        return button if page.number < page.count else None
    page.goto = AsyncMock(side_effect=goto)
    page.query_selector = AsyncMock(side_effect=query)
    page.wait_for_function = AsyncMock()
    page.evaluate = AsyncMock()
    @asynccontextmanager
    async def connect(self):
        yield page
    monkeypatch.setattr(module.BrowserConnector, 'connect', connect)
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(sleep=AsyncMock()))
    async def parse(page, **kwargs):
        return [(VacancyCard(external_id=str(page.number)), None)]
    collector = HHVacancyCardCollector(
        card_parser=SimpleNamespace(parse_cards_from_page=AsyncMock(side_effect=parse)),
        page_guard=SimpleNamespace(check_page_state=AsyncMock()),
        popup_handler=SimpleNamespace(setup_dialog_handler=Mock(), dismiss_known_overlays=AsyncMock()),
    )
    collector._wait_for_cards = AsyncMock(return_value=1)
    return collector, page, button


async def run(search, limiter=None, stop_after=None):
    collector, page, button = search
    results = []
    async for item in collector.collect(BrowserConfig(), 'https://hh.ru/search/vacancy', 'test',
                                        scroller_config=ScrollerConfig(load_mode='instant'), limiter=limiter):
        results.append(item)
        if isinstance(item, PageCommitParams) and item.page_number == stop_after:
            collector.stop()
    return results


async def test_collect_passes_tenth_page_and_finishes_at_end(search):
    limiter = SimpleNamespace(acquire=AsyncMock())
    results = await run(search, limiter)
    pages = [item for item in results if isinstance(item, PageCommitParams)]
    assert [p.page_number for p in pages] == list(range(1, 14))
    assert results[-1].completion_reason == 'no_more_pages'
    assert results[-1].total_pages_processed == 13
    assert search[2].click.await_count == 12
    assert limiter.acquire.await_count >= 13


async def test_user_stop_keeps_last_page_and_does_not_navigate(search):
    results = await run(search, stop_after=2)
    assert results[-1].completion_reason == 'stopped_by_user'
    assert results[-1].total_pages_processed == 2
    assert search[2].click.await_count == 1


async def test_stop_during_pacing_does_not_open_next_page(search):
    calls = 0
    async def acquire(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            search[0].stop()
    results = await run(search, SimpleNamespace(acquire=AsyncMock(side_effect=acquire)))
    assert results[-1].completion_reason == 'stopped_by_user'
    search[2].click.assert_not_awaited()


async def test_pagination_loop_stops_without_reprocessing(search):
    search[2].click.side_effect = None
    results = await run(search)
    assert results[-1].completion_reason == 'loop_detected'
    assert results[-1].total_pages_processed == 1


async def test_guard_checks_redirect_after_next_page(search):
    from src.browser.intervention import HHInterventionRequired
    async def guard(page, **kwargs):
        if page.number == 2:
            raise HHInterventionRequired('captcha')
    search[0].page_guard.check_page_state.side_effect = guard
    with pytest.raises(HHInterventionRequired):
        await run(search)
    assert search[0].card_parser.parse_cards_from_page.await_count == 1


def test_old_yaml_page_limit_does_not_limit_config(tmp_path):
    config_file = tmp_path / 'legacy.yaml'
    config_file.write_text('hh_autoscroller:\n  max_pages: 1\n', encoding='utf-8')
    assert not hasattr(Config.load(str(config_file)).scroller, 'max_pages')
