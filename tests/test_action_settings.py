"""Action forms isolate their settings and preserve shared/global state."""

import re

import pytest
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient

from src.config import Config
from src.db.models import Resume
from src.web.action_settings import ACTION_KEYS, SHARED_KEYS
from src.web.app import create_app


@pytest.fixture()
def client(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "action-settings.db")
    config.web.token = ""
    with TestClient(create_app(config)) as test_client:
        yield test_client


def test_shared_form_excludes_action_fields_and_preserves_their_switches(client):
    settings = client.app.state.settings
    with start_blocking_portal() as portal:
        portal.call(settings.save, {
            "activity.open_vacancies": "1", "schedule.activity_enabled": "1",
            "resume_touch.edit_fallback": "1", "cover_letter.enabled": "1",
            "schedule.enabled": "1", "matching.enabled": "1",
        })
    before = settings.all_values()
    page = client.get('/settings').text
    rendered = set(re.findall(r'name="([a-z_]+\.[^"]+)"', page))
    assert rendered == SHARED_KEYS
    assert 'name="apply.skip_questions"' not in page
    response = client.post('/actions/settings', data={
        'matching.threshold': '82', 'activity.open_vacancies': '', 'profile.max_chars': '9999',
    }, follow_redirects=False)
    assert response.status_code == 303
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    for key, value in before.items():
        if key not in SHARED_KEYS:
            assert settings.get(key) == value, key


@pytest.mark.parametrize('kind,values,expected', [
    ('score', {'matching.batch_size': '7', 'matching.concurrency': '2', 'matching.prompt': 'Оценить опыт'},
     {'matching.batch_size': 7, 'matching.concurrency': 2}),
    ('profile', {'profile.model': 'profile-test', 'profile.max_chars': '7500', 'profile.timeout_sec': '200'},
     {'profile.model': 'profile-test', 'profile.max_chars': 7500}),
    ('activity', {'activity.duration_min': '15', 'activity.open_vacancies': '1',
                  'schedule.activity_enabled': '1', 'schedule.activity_interval_minutes': '80'},
     {'activity.duration_min': 15, 'activity.open_vacancies': True, 'schedule.activity_enabled': True}),
    ('resume_touch', {'resume_touch.edit_fallback': '1', 'schedule.resume_touch_enabled': '1',
                      'schedule.resume_touch_interval_hours': '8'},
     {'resume_touch.edit_fallback': True, 'schedule.resume_touch_enabled': True,
      'schedule.resume_touch_interval_hours': 8}),
])
def test_action_modal_persists_only_its_own_settings(client, kind, values, expected):
    settings = client.app.state.settings
    before = settings.all_values()
    page = client.get('/actions').text
    assert f'data-open-{kind}' in page and f'id="{kind}-dialog"' in page
    modal = client.get(f'/actions/{kind}-settings')
    assert modal.status_code == 200
    for key in ACTION_KEYS[kind]:
        assert f'name="{key}"' in modal.text
    response = client.post(f'/actions/{kind}-settings', data={
        **values, 'llm.enabled': '1', 'matching.threshold': '99', 'schedule.do_apply': '1',
    })
    assert response.headers['HX-Trigger-After-Settle'] == f'{kind}SettingsSaved'
    assert response.headers['HX-Retarget'] == '#task-panel'
    assert not client.app.state.tasks.is_busy and not client.app.state.tasks.activity_busy
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    for key, value in expected.items():
        assert settings.get(key) == value
    for key, value in before.items():
        if key not in ACTION_KEYS[kind]:
            assert settings.get(key) == value, key
    if kind in {'activity', 'resume_touch'}:
        entry = next(entry for entry in client.app.state.scheduler.entries if entry.name == kind)
        assert entry.next_run_at is not None
        client.post(f'/actions/{kind}-settings', data={})
        assert entry.next_run_at is None


@pytest.mark.parametrize('kind,values', [
    ('search', {'scroller.max_pages': '0'}),
    ('score', {'matching.batch_size': '0', 'matching.prompt': '<b>Сохранить мой текст</b>'}),
    ('profile', {'profile.max_chars': '0', 'profile.model': 'my-model'}),
    ('activity', {'activity.duration_min': '0', 'schedule.activity_enabled': '1'}),
    ('resume_touch', {'schedule.resume_touch_interval_hours': '0', 'resume_touch.edit_fallback': '1'}),
    ('pipeline', {'schedule.interval_minutes': '0', 'schedule.do_apply': '1', 'schedule.enabled': '1'}),
])
def test_invalid_action_settings_keep_input_and_do_not_write(client, kind, values):
    settings = client.app.state.settings
    before = settings.all_values()
    response = client.post(f'/actions/{kind}-settings', data=values)
    assert 'HX-Trigger-After-Settle' not in response.headers
    assert 'минимум' in response.text and 'value="0"' in response.text
    if kind == 'score':
        assert '&lt;b&gt;Сохранить мой текст&lt;/b&gt;' in response.text
    if kind == 'pipeline':
        assert re.search(r'name="schedule.do_apply"[^>]*checked', response.text)
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.all_values() == before


def test_invalid_search_settings_do_not_change_query_or_resume_context(client):
    repository = client.app.state.repository
    with start_blocking_portal() as portal:
        resume_id = portal.call(repository.upsert_resume, Resume(
            source_url='https://hh.ru/resume/search-modal', title='Developer',
            search_query='Python', context_text='My experience',
        ))
        portal.call(repository.set_active_resume, resume_id)
    before = client.app.state.settings.all_values()
    response = client.post('/actions/search-settings', data={
        'resume_id': str(resume_id), 'search_query': 'Go', 'scroller.max_pages': '0',
    })
    assert 'value="Go"' in response.text and 'value="0"' in response.text
    assert 'HX-Trigger-After-Settle' not in response.headers
    with start_blocking_portal() as portal:
        resume = portal.call(repository.get_resume, resume_id)
    assert resume.search_query == 'Python' and resume.context_text == 'My experience'
    assert client.app.state.settings.all_values() == before
    stale = client.post('/actions/search-settings', data={
        'resume_id': str(resume_id + 1), 'search_query': 'Go', 'scroller.max_pages': '9',
    })
    assert 'Активное резюме изменилось' in stale.text
    assert client.app.state.settings.all_values() == before
