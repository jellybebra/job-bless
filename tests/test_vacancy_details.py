import asyncio
import csv
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.browser.intervention import HHInterventionRequired
from src.collector.detail_parser import VacancyDetailsReader, parse_details
from src.config import BrowserConfig, Config
from src.db.models import PageCommitParams, SearchRun, TaskKind, VacancyCard, VacancyDetails
from src.matching.scorer import build_vacancy_text
from src.web import jobs
from src.web.app import create_app
from src.web.tasks import TaskContext, TaskState


def snapshot(**changes):
    return {
        'vacancy_id': 42,
        'description_html': '<h2>Обязанности</h2><p>Python &amp; SQL</p><ul><li>Разработка</li><li>Тесты</li></ul>',
        'key_skills': ['Python', 'SQL', 'Python'],
        'published_at': '2026-09-11T12:00:00+03:00',
        'archived': False, 'response_letter_required': True, 'has_test': True,
        **changes,
    }


def test_parse_all_six_fields_and_keep_complete_description():
    data = snapshot(description_html='<p>' + 'Полное описание ' * 1000 + '</p>')
    result = parse_details(data, '42')
    assert len(result.full_description) > 6000
    assert result.key_skills == ['Python', 'SQL']
    assert result.published_at == '2026-09-11T12:00:00+03:00'
    assert (result.archived, result.response_letter_required, result.has_test) == (False, True, True)


def test_description_keeps_lines_and_does_not_execute_html():
    result = parse_details(snapshot(description_html='<p>Первая &lt;строка&gt;</p><script>bad()</script><p>Вторая</p>'), '42')
    assert result.full_description == 'Первая <строка>\nВторая'


def test_missing_flags_are_unknown_and_bad_date_is_not_guessed():
    result = parse_details(snapshot(archived=None, response_letter_required=None, has_test=None,
                                    user_test_id=None, published_at='Вчера', key_skills=None), '42')
    assert (result.archived, result.response_letter_required, result.has_test) == (None, None, None)
    assert result.published_at == ''
    assert result.key_skills is None


def test_empty_skills_and_explicit_false_are_known_answers():
    result = parse_details(snapshot(key_skills=[], response_letter_required=False, has_test=False), '42')
    assert result.key_skills == []
    assert result.response_letter_required is False and result.has_test is False


def test_archived_page_and_jsonld_fallback():
    result = parse_details({
        'dom_archived': True, 'dom_skills': ['SQL'],
        'postings': [
            {'identifier': {'value': 'other'}, 'description': 'Не эта вакансия'},
            {'identifier': {'value': 42}, 'description': '<p>Старая вакансия</p>', 'datePosted': '2025-12-25'},
        ],
    }, '42')
    assert result.archived is True
    assert result.full_description == 'Старая вакансия'
    assert result.published_at.startswith('2025-12-25')
    assert result.response_letter_required is None


def test_other_vacancy_and_empty_pages_are_rejected():
    with pytest.raises(ValueError, match='другую вакансию'):
        parse_details(snapshot(vacancy_id=99), '42')
    with pytest.raises(ValueError, match='не найдены'):
        parse_details({}, '42')


def test_positive_test_id_is_evidence_but_null_is_not():
    result = parse_details(snapshot(has_test=None, user_test_id=123), '42')
    assert result.has_test is True


@pytest.mark.parametrize('status,expected', [(403, HHInterventionRequired), (429, HHInterventionRequired), (404, ValueError)])
async def test_reader_does_not_treat_failed_http_as_archived(status, expected):
    reader = VacancyDetailsReader(BrowserConfig(), limiter=SimpleNamespace(acquire=AsyncMock()), should_stop=lambda: False)
    reader.page = SimpleNamespace(url='https://hh.ru/vacancy/42', goto=AsyncMock(return_value=SimpleNamespace(status=status)))
    with pytest.raises(expected):
        await reader.read(VacancyCard(external_id='42', url='https://hh.ru/vacancy/42'))
    reader.limiter.acquire.assert_awaited_once()


async def test_reader_checks_stop_after_rate_limit_before_navigation():
    stopped = False
    async def acquire(**kwargs):
        nonlocal stopped
        stopped = True
    reader = VacancyDetailsReader(BrowserConfig(), limiter=SimpleNamespace(acquire=AsyncMock(side_effect=acquire)), should_stop=lambda: stopped)
    reader.page = SimpleNamespace(goto=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await reader.read(VacancyCard(external_id='42', url='https://hh.ru/vacancy/42'))
    reader.page.goto.assert_not_awaited()


async def test_reader_retries_same_vacancy_after_browser_crash(monkeypatch):
    from playwright.async_api import Error
    from src.collector import detail_parser
    monkeypatch.setattr(detail_parser, 'asyncio', SimpleNamespace(sleep=AsyncMock()))
    reader = VacancyDetailsReader(BrowserConfig(), limiter=None, should_stop=lambda: False)
    reader.page = SimpleNamespace(url='https://hh.ru/vacancy/42')
    connection = SimpleNamespace(__aexit__=AsyncMock())
    reader.connection = connection
    result = parse_details(snapshot(), '42')
    reader._read = AsyncMock(side_effect=[Error('Page.goto: Target crashed'), result])
    card = VacancyCard(external_id='42', url='https://hh.ru/vacancy/42')
    assert await reader.read(card) is result
    assert reader._read.await_count == 2
    assert all(call.args == (card,) for call in reader._read.await_args_list)
    connection.__aexit__.assert_awaited_once()
    assert reader.page is None


async def test_reader_retry_wait_remains_stoppable(monkeypatch):
    from playwright.async_api import Error
    from src.collector import detail_parser
    stopped = False
    async def stop_during_wait(_):
        nonlocal stopped
        stopped = True
    monkeypatch.setattr(detail_parser, 'asyncio', SimpleNamespace(sleep=stop_during_wait, CancelledError=asyncio.CancelledError))
    reader = VacancyDetailsReader(BrowserConfig(), limiter=None, should_stop=lambda: stopped)
    reader._read = AsyncMock(side_effect=Error('Page.goto: Target crashed'))
    with pytest.raises(asyncio.CancelledError):
        await reader.read(VacancyCard(external_id='42', url='https://hh.ru/vacancy/42'))
    reader._read.assert_awaited_once()


@pytest.fixture
def client(tmp_path):
    config = Config.load('configs/config.yaml')
    config.db.sqlite_path = str(tmp_path / 'details.db')
    with TestClient(create_app(config)) as client:
        yield client


def seed(client):
    repo = client.app.state.repository
    client.portal.call(repo.create_search_run, SearchRun(id='details', task_id='details', search_url='https://hh.ru/search/vacancy'))
    page = PageCommitParams(search_run_id='details', page_key='1', page_number=1,
                          current_url='https://hh.ru/search/vacancy', canonical_url='https://hh.ru/search/vacancy',
                          cards=[VacancyCard(external_id='42', title='Python developer', url='https://hh.ru/vacancy/42', snippet='Короткая карточка')])
    client.portal.call(repo.commit_page_transaction, page)
    return repo, page


def test_details_survive_card_refresh_failed_refresh_and_database_reopen(client):
    repo, page = seed(client)
    client.portal.call(repo.save_vacancy_details, 'hh', '42', parse_details(snapshot(archived=True), '42'))
    client.portal.call(repo.commit_page_transaction, page)
    client.portal.call(repo.save_vacancy_details, 'hh', '42', VacancyDetails(error='Timeout'))
    row = client.portal.call(repo.list_vacancies)[0]
    assert row['full_description'].startswith('Обязанности')
    assert row['key_skills'] == ['Python', 'SQL']
    assert row['archived'] is True and row['details_error'] == 'Timeout'
    # A verified false must replace a previous true, and [] replaces old skills.
    client.portal.call(repo.save_vacancy_details, 'hh', '42', parse_details(snapshot(key_skills=[], response_letter_required=False, has_test=False), '42'))
    row = client.portal.call(repo.list_vacancies)[0]
    assert row['archived'] is False and row['has_test'] is False
    assert row['key_skills'] == [] and row['details_error'] == ''
    # A separate connection reads committed values, not an in-memory overlay.
    import sqlite3
    with sqlite3.connect(client.app.state.config.db.sqlite_path) as db:
        assert db.execute('SELECT archived, has_test FROM vacancy_details').fetchone() == (0, 0)


def test_ui_csv_and_scoring_use_saved_details(client):
    repo, _ = seed(client)
    details = parse_details(snapshot(description_html='<p>&lt;script&gt;unsafe()&lt;/script&gt;</p><p>Полное описание</p>'), '42')
    client.portal.call(repo.save_vacancy_details, 'hh', '42', details)
    html = client.get('/vacancies').text
    assert '&lt;script&gt;unsafe()&lt;/script&gt;' in html and '<script>unsafe()' not in html
    assert 'Обязательное сопроводительное письмо: <strong>Да</strong>' in html
    assert 'Тест или анкета HH: <strong>Да</strong>' in html
    assert '2026-09-11' in html
    assert '<span class="tag">SQL</span>' in html
    response = client.get('/vacancies/export')
    row = next(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig')), delimiter=';'))
    assert row['Полное описание вакансии'] == details.full_description
    assert row['Навыки'] == 'Python, SQL'
    assert row['В архиве'] == 'Нет'
    assert row['Обязательное сопроводительное письмо'] == row['Тест или анкета'] == 'Да'
    scoring_row = client.portal.call(repo.get_unscored_vacancies, 1)[0]
    prompt = build_vacancy_text(scoring_row)
    assert 'Полное описание' in prompt and 'Python, SQL' in prompt
    assert 'Короткая карточка' not in prompt


def test_legacy_cards_and_unknown_flags_are_not_shown_as_no(client):
    repo, _ = seed(client)
    assert 'Подробности ещё не загружены' in client.get('/vacancies').text
    client.portal.call(repo.save_vacancy_details, 'hh', '42', parse_details(snapshot(archived=None, has_test=None, response_letter_required=None), '42'))
    assert client.get('/vacancies').text.count('Не удалось определить') == 3


@pytest.mark.parametrize('outcome', ['ok', 'error', 'stop', 'captcha'])
def test_job_saves_cards_first_and_each_detail_immediately(client, monkeypatch, outcome):
    repo, manager = client.app.state.repository, client.app.state.tasks
    ctx = TaskContext(manager, TaskState(id='collect-details', kind=TaskKind.COLLECT), manager.lane())
    async def collect(self, **kwargs):
        yield PageCommitParams(search_run_id=kwargs['task_id'], page_key='1', page_number=1,
                               current_url=kwargs['search_url'], canonical_url=kwargs['search_url'],
                               cards=[VacancyCard(external_id=str(i), title=f'Vacancy {i}', url=f'https://hh.ru/vacancy/{i}') for i in (42, 43)])
    monkeypatch.setattr(jobs.HHVacancyCardCollector, 'collect', collect)
    seen = []
    class Reader:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): seen.append('closed')
        async def read(self, card):
            assert await repo.count_vacancies() == 2
            if card.external_id == '43':
                rows = await repo.list_vacancies()
                assert next(r for r in rows if r['external_id'] == '42')['full_description']
                if outcome == 'error': raise ValueError('Timeout')
                if outcome == 'stop': raise asyncio.CancelledError()
                if outcome == 'captcha': raise HHInterventionRequired('captcha')
            return parse_details(snapshot(vacancy_id=int(card.external_id)), card.external_id)
    monkeypatch.setattr(jobs, 'VacancyDetailsReader', Reader)
    async def run():
        if outcome in ('stop', 'captcha'):
            with pytest.raises(asyncio.CancelledError if outcome == 'stop' else HHInterventionRequired):
                await jobs.collect_job(ctx)
        else:
            result = await jobs.collect_job(ctx)
            assert result['detailed'] == (1 if outcome == 'error' else 2)
        return await repo._fetch_val('SELECT status FROM search_runs WHERE id = ?', ('web_collect-details',))
    assert client.portal.call(run) == {'ok': 'completed', 'error': 'completed', 'stop': 'cancelled', 'captcha': 'failed'}[outcome]
    assert seen == ['closed']
    assert client.portal.call(repo.count_vacancies) == 2
