import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.config import Config
from src.db.connection import init_sqlite
from src.db.models import Resume
from src.db.repository import DatabaseRepository
from src.resume.parser import HHResumeParser, is_resume_url, parse_certificates, parse_verified_skills
from src.resume.service import ResumeService
from src.settings import SettingsService


@pytest.fixture
async def service(tmp_path, monkeypatch):
    connection = await init_sqlite(str(tmp_path / 'resume.db'))
    repository = DatabaseRepository(connection, driver='sqlite')
    settings = SettingsService(Config(), repository)
    await settings.load()

    @asynccontextmanager
    async def connect(self):
        yield Mock()

    monkeypatch.setattr('src.resume.service.BrowserConnector.connect', connect)
    yield ResumeService(repository, settings)
    await connection.close()


async def test_all_resumes_deduplicate_old_links_and_preserve_user_data(service):
    repo = service.repository
    saved = Resume(source_url='https://hh.ru/resume/one?from=profile', external_id='one',
                   title='Old title', raw_text='Old text', search_query='Python',
                   context_text='My projects', profile_summary='My profile', profile_model='model')
    saved.profile_hash = saved.content_fingerprint()
    saved_id = await repo.upsert_resume(saved)
    await repo.set_active_resume(saved_id)
    service.parser.discover = AsyncMock(return_value=['https://hh.ru/resume/one', 'https://hh.ru/resume/two'])

    async def parse(page, url, **kwargs):
        return Resume(source_url=url, external_id=url.rsplit('/', 1)[1], title='New title', raw_text='New text')

    service.parser.parse = parse
    for _ in range(2):
        result = await service.import_account()
        assert result['imported'] == 2
        assert len(await repo.list_resumes()) == 2
        assert (await repo.get_active_resume()).id == saved_id
    updated = await repo.get_resume(saved_id)
    assert updated.raw_text == 'New text'
    assert updated.title == 'New title'
    assert updated.search_query == 'Python'
    assert updated.context_text == 'My projects'
    assert updated.profile_summary == 'My profile'
    assert updated.profile_is_stale


async def test_partial_failure_continues_and_selects_first_success(service):
    service.parser.discover = AsyncMock(return_value=['https://hh.ru/resume/bad', 'https://hh.ru/resume/good'])
    service.parser.parse = AsyncMock(side_effect=[ValueError('Unavailable'), Resume(
        source_url='https://hh.ru/resume/good', external_id='good', title='Engineer')])
    report = await service.import_account()
    assert (report['found'], report['imported'], report['failed']) == (2, 1, 1)
    assert (await service.repository.get_active_resume()).external_id == 'good'


async def test_refresh_inactive_card_keeps_selection_and_concurrent_edits(service):
    repo = service.repository
    active = await repo.upsert_resume(Resume(source_url='https://hh.ru/resume/one', external_id='one'))
    inactive = await repo.upsert_resume(Resume(source_url='https://hh.ru/resume/two', external_id='two'))
    await repo.set_active_resume(active)

    async def parse(page, url, **kwargs):
        await repo.update_resume_fields(inactive, 'New query', 'Saved while parsing')
        return Resume(source_url=url, external_id='two', title='Updated', raw_text='Changed')

    service.parser.parse = parse
    updated = await service.refresh(inactive)
    assert updated.id == inactive and updated.title == 'Updated'
    assert updated.context_text == 'Saved while parsing'
    assert updated.search_query == 'New query'
    assert (await repo.get_active_resume()).id == active


async def test_empty_account_does_not_delete_saved_resumes(service):
    await service.repository.upsert_resume(Resume(source_url='https://hh.ru/resume/old'))
    service.parser.discover = AsyncMock(return_value=[])
    report = await service.import_account()
    assert report['found'] == report['imported'] == report['failed'] == 0
    assert len(await service.repository.list_resumes()) == 1


async def test_cancellation_stops_before_writing(service):
    service.parser.discover = AsyncMock(return_value=['https://hh.ru/resume/one'])
    service.parser.parse = AsyncMock(return_value=Resume(source_url='https://hh.ru/resume/one'))
    checkpoint = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await service.import_account(checkpoint=checkpoint)
    assert await service.repository.list_resumes() == []


async def test_discovery_reads_pagination_and_deduplicates():
    page = Mock()
    page.url = 'https://hh.ru/applicant/resumes'
    page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
    page.wait_for_function = AsyncMock()
    links = Mock(evaluate_all=AsyncMock(side_effect=[
        ['https://hh.ru/resume/one?x=1', 'https://hh.ru/resume/one', 'https://evil.test/resume/fake'],
        ['https://hh.ru/resume/two', 'https://hh.ru/resume/one?x=2']]))
    pager = Mock(count=AsyncMock(side_effect=[1, 0]), get_attribute=AsyncMock(return_value='/applicant/resumes?page=1'))
    page.locator.side_effect = lambda selector: (
        Mock(count=AsyncMock(return_value=0)) if 'captcha' in selector
        else Mock(first=pager) if 'pager-next' in selector else links)
    urls = await HHResumeParser().discover(page)
    assert urls == ['https://hh.ru/resume/one', 'https://hh.ru/resume/two']
    assert page.goto.await_count == 2


@pytest.mark.parametrize('url,status', [('https://hh.ru/account/login', 200), ('https://hh.ru/captcha', 200), ('https://hh.ru/resume/one', 404)])
async def test_inaccessible_pages_are_not_parsed(url, status):
    page = Mock(url=url)
    page.locator.return_value.count = AsyncMock(return_value=0)
    with pytest.raises((PermissionError, ValueError)):
        await HHResumeParser()._check_access(page, SimpleNamespace(status=status))


def test_resume_url_requires_complete_hh_host_and_id():
    assert is_resume_url('https://hh.ru/resume/abc?from=profile')
    assert is_resume_url('https://maikop.hh.ru/resume/abc')
    assert not is_resume_url('https://hh.ru.evil.test/resume/abc')
    assert not is_resume_url('https://hh.ru/resume/abc-invalid')


def test_only_effective_confirmed_skills_are_recognised():
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    current = {'status': 'SUCCESS', 'state': 'EFFECTIVE', 'validUntil': '2026-12-22T09:55:43+0300'}
    skills = [
        {'name': 'Docker', 'verified': True, 'verifications': [current]},
        {'name': 'Golang', 'verified': True, 'verifications': [current]},
        {'name': 'Python', 'verified': False, 'verifications': [current]},
        {'name': 'Expired', 'verified': True, 'verifications': [{**current, 'validUntil': '2026-01-01T00:00:00Z'}]},
        {'name': 'Failed', 'verified': True, 'verifications': [{**current, 'status': 'FAILED'}]},
        {'name': 'Revoked', 'verified': True, 'verifications': [{**current, 'state': 'EXPIRED'}]},
        {'name': 'Docker', 'verified': True},
        {'name': 'Boolean string', 'verified': 'true'},
    ]
    assert parse_verified_skills(skills, now) == ['Docker', 'Golang']


def test_certificate_state_requires_a_saved_title():
    assert parse_certificates([]) == []
    assert parse_certificates([{'title': 'CKA', 'achievementDate': '2025-03-01'},
                               {'title': ''}, {'name': 'Suggested skill'}, None]) == ['CKA (2025)']


async def test_resume_parser_uses_verified_state_and_does_not_read_city_or_salary(monkeypatch):
    parser = HHResumeParser()
    page = Mock(url='https://hh.ru/resume/one')
    page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.evaluate = AsyncMock(return_value={
        'certificates': [], 'skills': [{'name': 'Docker', 'verified': True}, {'name': 'Python', 'verified': False}],
    })
    parser._page_text = AsyncMock(return_value='Навыки\nDocker\nPython\nСертификаты\nНедостоверный текст\nО себе\nРазработчик')
    parser._first_text = AsyncMock(return_value='Engineer')
    monkeypatch.setattr('src.resume.parser.asyncio.sleep', AsyncMock())
    resume = await parser.parse(page, page.url)
    assert resume.verified_skills == ['Docker']
    assert resume.certificates == []  # an explicit empty saved list overrides textual guesses
    assert resume.city == resume.salary_text == ''
    requested = str(parser._first_text.call_args_list)
    assert 'resume-block-salary' not in requested and 'resume-personal-address' not in requested


async def test_verified_skills_persist_and_refresh_clears_false_certificates(service):
    repo = service.repository
    resume_id = await repo.upsert_resume(Resume(
        source_url='https://hh.ru/resume/one', external_id='one', skills=['Python', 'Docker'],
        verified_skills=['Python'], certificates=['Фотографию', 'Портфолио'], context_text='My context',
    ))
    service.parser.parse = AsyncMock(return_value=Resume(
        source_url='https://hh.ru/resume/one', external_id='one', title='Engineer',
        skills=['Python', 'Docker'], verified_skills=['Docker'], certificates=[],
    ))
    refreshed = await service.refresh(resume_id)
    assert refreshed.verified_skills == ['Docker']
    assert refreshed.certificates == []
    assert refreshed.context_text == 'My context'
    assert (await repo.list_resumes())[0].verified_skills == ['Docker']


def test_verified_skills_are_available_to_profile_and_invalidate_it_when_changed():
    resume = Resume(raw_text='Engineer', verified_skills=['Docker'], profile_summary='Profile')
    resume.profile_hash = resume.content_fingerprint()
    assert not resume.profile_is_stale
    assert 'Docker' in resume.source_text()
    resume.verified_skills = []
    assert resume.profile_is_stale
