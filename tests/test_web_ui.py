"""Smoke tests for the web UI: pages render, settings persist, jobs are guarded.

No browser and no LLM are involved — jobs are only inspected, never started
against real hh.ru.
"""

import pytest
from fastapi.testclient import TestClient

from src.config import Config
from src.db.models import Resume, TaskKind, VacancyScore
from src.web.app import create_app


@pytest.fixture()
def client(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "test.db")
    config.web.token = ""
    with TestClient(create_app(config)) as test_client:
        yield test_client


def test_pages_render(client):
    for path in ("/", "/actions", "/vacancies", "/applications", "/resume", "/settings", "/runs"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "job-bless" in response.text


def test_task_panel_partial(client):
    response = client.get("/partials/status")
    assert response.status_code == 200
    assert 'id="task-panel"' in response.text


def test_settings_roundtrip(client):
    response = client.post(
        "/actions/settings",
        data={
            "matching.threshold": "85",
            "apply.mode": "auto",
            "schedule.enabled": "1",
            "schedule.interval_minutes": "60",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"

    settings = client.app.state.settings
    assert settings.get("matching.threshold") == 85
    assert settings.get("apply.mode") == "auto"
    # Checkboxes absent from the payload must become False, not stay True.
    assert settings.get("schedule.do_apply") is False
    assert settings.get("schedule.enabled") is True

    # Values survive a reload from the database (fresh process would do the same).
    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.get("matching.threshold") == 85
    assert settings.get("apply.mode") == "auto"
    assert "85" in client.get("/settings").text


def test_settings_validation_rejects_bad_number(client):
    response = client.post("/actions/settings", data={"matching.threshold": "500"})
    assert response.status_code == 400
    assert "максимум" in response.text
    assert client.app.state.settings.get("matching.threshold") != 500


def test_settings_feed_typed_configs(client):
    client.post(
        "/actions/settings",
        data={
            "browser.headless": "1",
            "llm.standard": "anthropic",
            "llm.model": "gemini-2.5-flash-lite",
            "scroller.max_pages": "7",
        },
        follow_redirects=False,
    )
    settings = client.app.state.settings
    assert settings.browser_config().headless is True
    assert settings.llm_config().standard == "anthropic"
    assert settings.scroller_config().max_pages == 7


def test_secret_is_not_cleared_by_empty_field(client):
    client.post("/actions/settings", data={"llm.api_key": "secret-key"}, follow_redirects=False)
    assert client.app.state.settings.get("llm.api_key") == "secret-key"

    client.post("/actions/settings", data={"llm.api_key": ""}, follow_redirects=False)
    assert client.app.state.settings.get("llm.api_key") == "secret-key"


async def _seed_vacancy(repository, external_id="v1", title="Python developer") -> int:
    from src.db.models import PageCommitParams, SearchRun, VacancyCard

    run_id = "seed-run"
    await repository.create_search_run(SearchRun(id=run_id, task_id=run_id, search_url="https://hh.ru/search"))
    card = VacancyCard(
        external_id=external_id, url=f"https://hh.ru/vacancy/{external_id}", title=title,
        company_name="ООО Ромашка", salary_text="200 000 ₽", city="Москва", snippet="Django, PostgreSQL",
    )
    await repository.commit_page_transaction(
        PageCommitParams(
            search_run_id=run_id, page_key="page_1", page_number=1,
            current_url="https://hh.ru/search", canonical_url="https://hh.ru/search", cards=[card],
        )
    )
    row = await repository._fetch_one("SELECT id FROM vacancies WHERE external_id = ?;", (external_id,))
    return row["id"]


def test_vacancies_page_shows_scores(client):
    from anyio.from_thread import start_blocking_portal

    repository = client.app.state.repository
    with start_blocking_portal() as portal:
        vacancy_id = portal.call(_seed_vacancy, repository)
        resume_id = portal.call(
            repository.upsert_resume,
            Resume(source_url="https://hh.ru/resume/abc", title="Python-разработчик", skills=["Python"]),
        )
        portal.call(repository.set_active_resume, resume_id)
        portal.call(
            repository.upsert_score,
            VacancyScore(vacancy_id=vacancy_id, resume_id=resume_id, score=91,
                         verdict="Полное совпадение по стеку", matched_skills=["Python"]),
        )

    page = client.get("/vacancies")
    assert page.status_code == 200
    assert "Python developer" in page.text
    assert "91" in page.text
    assert "Полное совпадение по стеку" in page.text

    dashboard = client.get("/")
    assert "готовы к отклику" in dashboard.text


def test_apply_without_resume_reports_error_in_panel(client, monkeypatch):
    # A job that fails must surface in the panel, not crash the request.
    response = client.post("/actions/apply")
    assert response.status_code == 200
    assert 'id="task-panel"' in response.text


def test_stop_and_confirm_without_task(client):
    assert "нет выполняющейся задачи" in client.post("/actions/stop").text
    assert "задача не ждёт подтверждения" in client.post("/actions/confirm").text


def test_resume_import_rejects_bad_url(client):
    from anyio.from_thread import start_blocking_portal
    from src.resume.parser import extract_resume_id, is_resume_url

    assert is_resume_url("https://hh.ru/resume/abc123") is True
    assert is_resume_url("https://hh.ru/vacancy/123") is False
    assert extract_resume_id("https://hh.ru/resume/abc123?query=1") == "abc123"

    service_error = None
    with start_blocking_portal() as portal:
        from src.resume.service import ResumeService

        service = ResumeService(client.app.state.repository, client.app.state.settings)
        try:
            portal.call(service.import_from_url, "https://example.com/not-a-resume")
        except ValueError as e:
            service_error = str(e)
    assert "hh.ru/resume" in (service_error or "")


def test_token_guard_blocks_without_token(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "guard.db")
    config.web.token = "s3cret"

    with TestClient(create_app(config)) as guarded:
        assert guarded.get("/").status_code == 401
        assert guarded.get("/static/app.css").status_code == 200
        allowed = guarded.get("/?token=s3cret", follow_redirects=True)
        assert allowed.status_code == 200


def test_search_query_builds_url_and_keeps_other_filters(client):
    """The query now comes from a resume; the URL template still carries filters."""
    settings = client.app.state.settings
    settings._search_url_template = "https://spb.hh.ru/search/vacancy?text=Python&area=2&experience=between1And3"

    url = settings.search_url_for("Go разработчик")
    assert url.startswith("https://spb.hh.ru/search/vacancy?")
    assert "text=Go+%D1%80%D0%B0%D0%B7%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D1%87%D0%B8%D0%BA" in url
    assert "area=2" in url and "experience=between1And3" in url
    assert settings.scroller_config(query="Go разработчик").search_url == url


def test_settings_page_has_no_search_fields(client):
    """Both the raw URL and the query live outside the settings page now."""
    page = client.get("/settings").text
    assert 'name="search.url"' not in page
    assert 'name="search.query"' not in page


def test_legacy_search_url_setting_migrates_to_query(client):
    from anyio.from_thread import start_blocking_portal

    repository = client.app.state.repository
    settings = client.app.state.settings
    with start_blocking_portal() as portal:
        portal.call(
            repository.save_settings,
            {"search.url": "https://hh.ru/search/vacancy?text=Data+Engineer&area=1"},
        )
        portal.call(settings.load)

    # The query is kept to seed a resume that has none of its own yet.
    assert settings.legacy_query == "Data Engineer"
    assert "area=1" in settings.search_url


# --- llm lamp -----------------------------------------------------------

def test_llm_lamp_reports_disabled(client):
    client.post("/actions/settings", data={"llm.enabled": ""}, follow_redirects=False)
    response = client.get("/actions/llm-health")
    assert response.status_code == 200
    assert "llm-status disabled" in response.text
    assert "нейросеть выключена" in response.text


def test_llm_lamp_reports_unreachable_endpoint(client):
    client.post(
        "/actions/settings",
        data={"llm.enabled": "1", "llm.base_url": "http://127.0.0.1:1", "llm.timeout_sec": "5"},
        follow_redirects=False,
    )
    response = client.get("/actions/llm-health?force=1")
    assert "llm-status error" in response.text
    assert "нет подключения" in response.text


def test_refreshed_lamp_does_not_retrigger_itself(client):
    """A returned lamp carrying hx-trigger="load" would loop forever."""
    fragment = client.get("/actions/llm-health").text
    assert "load" not in fragment
    assert "every 10s" in fragment

    # The copy embedded in a page does need the initial load.
    assert "load, every 10s" in client.get("/").text


def test_compact_lamp_shows_only_model(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(
        ok=True, state="ok", message="есть подключение", model="gemini-2.5-flash-lite", latency_ms=900
    )
    monitor._checked_monotonic = float("inf")

    compact = client.get("/actions/llm-health?compact=1").text
    assert "gemini-2.5-flash-lite" in compact
    assert 'class="llm-text"' not in compact  # no visible message, only the title
    assert 'title="есть подключение"' in compact
    assert "проверить" not in compact
    assert "compact=1" in compact  # keeps polling in compact form

    full = client.get("/actions/llm-health").text
    assert 'class="llm-text"' in full
    assert "есть подключение" in full and "проверить" in full


def test_panel_has_llm_settings_button_and_dashboard_keeps_health_status(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(ok=True, state="ok", message="есть подключение", model="gemini-2.5-flash-lite")
    monitor._checked_monotonic = float("inf")

    panel = client.get("/partials/status").text
    assert 'aria-label="Настроить нейросеть"' in panel
    assert "search-model" not in panel
    assert "llm-status" not in panel

    dashboard = client.get("/").text
    assert 'class="llm-text"' in dashboard  # full lamp with the message
    assert "проверить" in dashboard


def test_llm_lamp_is_on_dashboard_and_settings(client):
    assert "llm-status" in client.get("/").text
    assert "llm-status" in client.get("/settings").text


def test_llm_health_is_cached(client):
    monitor = client.app.state.llm_health
    client.get("/actions/llm-health?force=1")
    first = monitor.cached
    client.get("/actions/llm-health")
    assert monitor.cached is first  # served from cache, no second probe


def test_model_field_is_dropdown_when_models_known(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(
        ok=True, state="ok", message="есть подключение",
        models=["gemini-2.5-flash-lite", "gemini-3-flash-preview"],
    )
    monitor._checked_monotonic = float("inf")  # keep the fake result cached

    response = client.get("/actions/llm-models")
    assert "<select" in response.text
    assert 'name="llm.model"' in response.text
    assert "gemini-3-flash-preview" in response.text
    assert "моделей доступно: 2" in response.text


def test_model_field_keeps_custom_value_not_in_list(client):
    from src.llm.health import LLMHealth

    client.post("/actions/settings", data={"llm.model": "my-own-model"}, follow_redirects=False)
    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(ok=True, state="ok", models=["gemini-2.5-flash-lite"])
    monitor._checked_monotonic = float("inf")

    text = client.get("/actions/llm-models").text
    assert "my-own-model — своё значение" in text


def test_model_field_falls_back_to_text_input(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(ok=False, state="error", message="нет подключения: ключ отклонён", models=[])
    monitor._checked_monotonic = float("inf")

    text = client.get("/actions/llm-models").text
    assert "<select" not in text
    assert 'type="text"' in text and 'name="llm.model"' in text
    assert "ключ отклонён" in text


def test_scroll_settings_are_behind_a_spoiler(client):
    page = client.get("/settings").text
    assert "<details" in page and "Тонкая настройка" in page

    # Everyday fields stay visible, the scroll knobs move inside the spoiler.
    before_details = page.split('class="settings-form"', 1)[1].split("<details", 1)[0]
    assert 'name="scroller.load_mode"' in before_details
    assert 'name="scroller.max_pages"' not in before_details
    assert 'name="scroller.max_pages"' in page


def test_advanced_fields_still_save(client):
    client.post(
        "/actions/settings",
        data={"scroller.max_pages": "7", "scroller.stable_cycles": "5"},
        follow_redirects=False,
    )
    settings = client.app.state.settings
    assert settings.get("scroller.max_pages") == 7
    assert settings.scroller_config().stable_cycles == 5


def test_spoiler_opens_when_a_value_differs_from_default(client):
    client.post("/actions/settings", data={"scroller.max_pages": "9"}, follow_redirects=False)
    page = client.get("/settings").text
    assert "<details class=\"advanced\" open" in page


def _stage_button(panel, kind):
    import re

    match = re.search(r'<button\b[^>]*hx-post="/actions/' + kind + r'"[^>]*>', panel)
    assert match, f"Missing stage button: {kind}"
    return match.group()


def test_new_user_sees_disabled_actions_with_resume_setup(client):
    import re

    panel = client.get("/partials/status").text
    assert 'href="/resume#resume-import">Добавить резюме</a>' in panel
    assert panel.count('class="resume-banner"') == 1
    assert panel.count('data-action-id=') == 8
    assert "Начните с вашего резюме" not in panel
    assert 'aria-label="Настроить нейросеть"' in panel
    assert "Собрать вакансии" in panel
    assert "Оценить соответствие вакансий резюме" in panel
    for kind in ("collect", "score", "apply"):
        assert "disabled" in _stage_button(panel, kind)
        assert f'aria-describedby="{kind}-context"' in _stage_button(panel, kind)
    assert "Собрать вакансии" in panel
    assert 'hx-get="/actions/search-settings"' in panel
    assert 'id="panel-search-query"' not in panel
    modal = client.get("/actions/search-settings").text
    query = re.search(r'<input\b[^>]*id="panel-search-query"[^>]*>', modal)
    assert query and "disabled" in query.group()
    assert 'label for="panel-search-query">Какую работу ищете</label>' in modal
    assert 'type="radio"' not in panel


def test_settings_page_requests_model_dropdown(client):
    """Every model-typed setting asks for its own dropdown."""
    page = client.get("/settings").text
    assert 'hx-get="/actions/llm-models?field=llm.model"' in page
    assert 'hx-get="/actions/llm-models?field=profile.model"' in page
    assert 'hx-get="/actions/llm-models?field=cover_letter.model"' in page


def test_static_assets_are_versioned_and_revalidated(client):
    page = client.get("/").text
    assert "/static/app.css?v=" in page
    assert "/static/app.js?v=" in page

    response = client.get("/static/app.css")
    assert "no-cache" in response.headers.get("cache-control", "")


# --- runner -------------------------------------------------------------

def test_resume_unlocks_actions_and_explains_missing_setup(client):
    from anyio.from_thread import start_blocking_portal

    repository = client.app.state.repository
    with start_blocking_portal() as portal:
        resume_id = portal.call(repository.upsert_resume, Resume(source_url="https://hh.ru/resume/qa", title="Разработчик"))
        portal.call(repository.set_active_resume, resume_id)

    panel = client.get("/partials/status").text
    assert "Укажите должность в окне «Настроить поиск»." in panel
    assert "Настроить нейросеть для оценки" in panel
    assert "disabled" in _stage_button(panel, "collect")
    assert "disabled" in _stage_button(panel, "score")
    assert "disabled" not in _stage_button(panel, "apply")

    with start_blocking_portal() as portal:
        portal.call(repository.update_resume_fields, resume_id, "Python", "")
        portal.call(client.app.state.settings.save, {"llm.enabled": True, "matching.threshold": 85, "apply.batch_limit": 3})

    for path in ("/partials/status", "/actions"):
        panel = client.get(path).text
        for kind in ("collect", "score", "apply"):
            assert "disabled" not in _stage_button(panel, kind)
        assert "Ищем: «Python»" in panel
        assert "До 3 откликов" in panel
        assert "оценка от 85" in panel


def test_panel_query_edit_preserves_resume_context(client):
    from anyio.from_thread import start_blocking_portal

    repository = client.app.state.repository
    with start_blocking_portal() as portal:
        resume_id = portal.call(repository.upsert_resume, Resume(
            source_url="https://hh.ru/resume/query-test", title="Разработчик",
            context_text="Опыт разработки на Python", search_query="Python",
        ))
        portal.call(repository.set_active_resume, resume_id)

    modal = client.get("/actions/search-settings").text
    assert 'value="Python"' in modal
    assert f'hx-post="/actions/resume/{resume_id}/search-query"' in modal

    response = client.post(f"/actions/resume/{resume_id}/search-query", data={"search_query": "  Backend Python  "})
    assert response.status_code == 200
    assert response.headers["HX-Refresh"] == "true"
    assert "Ищем: «Backend Python»" in response.text
    assert "disabled" not in _stage_button(response.text, "collect")
    with start_blocking_portal() as portal:
        saved = portal.call(repository.get_resume, resume_id)
    assert saved.search_query == "Backend Python"
    assert saved.context_text == "Опыт разработки на Python"

    response = client.post(f"/actions/resume/{resume_id}/search-query", data={"search_query": " "})
    assert "disabled" in _stage_button(response.text, "collect")
    response = client.post(f"/actions/resume/{resume_id + 1}/search-query", data={"search_query": "Java"})
    assert "Активное резюме изменилось" in response.text


def test_pipeline_dialog_matches_effective_settings(client):
    from anyio.from_thread import start_blocking_portal

    settings = client.app.state.settings
    with start_blocking_portal() as portal:
        portal.call(settings.save, {"schedule.do_collect": True, "schedule.do_apply": True, "apply.mode": "manual", "matching.enabled": False})
    modal = client.get("/actions/pipeline-settings").text
    assert modal.count(" checked") == 1

    with start_blocking_portal() as portal:
        portal.call(settings.save, {"schedule.do_collect": True, "schedule.do_apply": True, "apply.mode": "auto", "matching.enabled": False})
    modal = client.get("/actions/pipeline-settings").text
    assert modal.count(" checked") == 2


def test_start_requires_known_kind(client):
    response = client.post("/actions/start", data={"kind": "nonsense"})
    assert "неизвестная задача" in response.text


def test_start_runs_selected_job(client):
    # `score` fails fast without a resume, which is enough to prove routing.
    response = client.post("/actions/start", data={"kind": "score"})
    assert response.status_code == 200
    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        runs = portal.call(client.app.state.repository.list_task_runs, 5)
    assert runs and runs[0]["kind"] == "score"


# --- stopping -----------------------------------------------------------

def test_stop_hook_fires_immediately(client):
    import anyio
    from anyio.from_thread import start_blocking_portal

    from src.db.models import TaskKind

    manager = client.app.state.tasks
    fired = {"value": False}

    async def slow_job(ctx):
        ctx.on_stop(lambda: fired.__setitem__("value", True))
        for _ in range(200):
            ctx.raise_if_stopped()
            await anyio.sleep(0.05)
        return {}

    async def stop_from_loop():
        manager.request_stop()

    with start_blocking_portal() as portal:
        portal.call(manager.start, TaskKind.COLLECT, slow_job)
        portal.call(anyio.sleep, 0.1)
        assert manager.is_busy
        portal.call(stop_from_loop)
        assert fired["value"] is True  # hook ran on the button press, not later
        portal.call(anyio.sleep, 0.3)
        assert not manager.is_busy


def test_second_stop_press_cancels_stuck_job(client):
    import anyio
    from anyio.from_thread import start_blocking_portal

    from src.db.models import TaskKind, TaskStatus

    manager = client.app.state.tasks

    async def stuck_job(ctx):
        # Ignores the stop flag entirely, like a hung browser call.
        await anyio.sleep(30)
        return {}

    async def stop_from_loop():
        manager.request_stop()

    with start_blocking_portal() as portal:
        portal.call(manager.start, TaskKind.COLLECT, stuck_job)
        portal.call(anyio.sleep, 0.1)
        portal.call(stop_from_loop)     # polite
        portal.call(stop_from_loop)     # forced
        portal.call(anyio.sleep, 0.2)
        assert not manager.is_busy
        assert manager.current.status == TaskStatus.CANCELLED


# --- shutdown -----------------------------------------------------------

def test_event_stream_closes_when_server_shuts_down(client):
    """Ctrl+C used to hang forever waiting for the live-log connection."""
    client.app.state.shutting_down = True

    with client.stream("GET", "/actions/events") as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "server_closing" in body  # the stream ended by itself


def test_serve_marks_app_as_shutting_down_on_signal(tmp_path, monkeypatch):
    """The signal handler must flip the flag before uvicorn waits for sockets."""
    import asyncio

    import uvicorn

    from src.web import app as web_app

    config = Config.load("configs/config.local.yaml")
    config.db.sqlite_path = str(tmp_path / "serve.db")
    config.web.port = 8099

    captured = {}

    class FakeServer(uvicorn.Server):
        def __init__(self, uvicorn_config):
            super().__init__(uvicorn_config)
            captured["server"] = self
            captured["app"] = uvicorn_config.app
            captured["timeout"] = uvicorn_config.timeout_graceful_shutdown

        async def serve(self, sockets=None):
            return None

        def handle_exit(self, sig, frame):
            captured["exited"] = True

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    asyncio.run(web_app.serve(config))

    server = captured["server"]
    assert captured["timeout"] == web_app.SHUTDOWN_DEADLINE_SECONDS
    assert captured["app"].state.shutting_down is False
    server.handle_exit(2, None)  # the subclass created inside serve()
    assert captured["app"].state.shutting_down is True


def test_task_kinds_cover_all_jobs():
    from src.web import jobs

    assert {TaskKind.COLLECT, TaskKind.SCORE, TaskKind.APPLY, TaskKind.RESUME_IMPORT, TaskKind.LOGIN}
    assert callable(jobs.collect_job) and callable(jobs.score_job) and callable(jobs.apply_job)
    assert callable(jobs.resume_import_job) and callable(jobs.login_job) and callable(jobs.pipeline_job)


def test_pipeline_modal_saves_only_its_settings(client):
    from anyio.from_thread import start_blocking_portal

    settings = client.app.state.settings
    before = settings.all_values()
    response = client.post('/actions/pipeline-settings', data={
        'schedule.do_collect': '1', 'schedule.do_score': '1', 'schedule.do_apply': '1',
        'llm.enabled': '1', 'schedule.enabled': '1',  # unrelated input is ignored
    })
    assert response.status_code == 200
    assert response.headers['HX-Retarget'] == '#task-panel'
    assert 'pipelineSettingsSaved' in response.headers['HX-Trigger-After-Settle']
    changed_keys = {'schedule.do_collect', 'schedule.do_score', 'schedule.do_apply', 'matching.enabled', 'apply.mode'}
    for key, value in before.items():
        if key not in changed_keys:
            assert settings.get(key) == value, key
    assert settings.get('matching.enabled') is True
    assert settings.get('apply.mode') == 'auto'
    assert not client.app.state.tasks.is_busy
    assert client.get('/actions/pipeline-settings').text.count(' checked') == 3

    # The full settings form no longer owns the stage flags.
    client.post('/actions/settings', data={'matching.threshold': '85'})
    for key in ('schedule.do_collect', 'schedule.do_score', 'schedule.do_apply'):
        assert settings.get(key) is True

    # Turning off every stage persists; no old checkbox value sneaks back in.
    client.post('/actions/pipeline-settings', data={})
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    for key in ('schedule.do_collect', 'schedule.do_score', 'schedule.do_apply'):
        assert settings.get(key) is False
    assert 'disabled' in _stage_button(client.get('/partials/status').text, 'pipeline')


def test_stage_controls_moved_out_of_settings_and_tools_are_visible(client):
    panel = client.get('/partials/status').text
    main, tools = panel.split('id="search-tools"', 1)
    assert 'Выполнить несколько действий подряд' in main
    assert 'Настроить действия' in main
    assert '<details' not in panel
    assert 'Войти в hh.ru' in tools
    page = client.get('/settings').text
    assert 'id="pipeline-dialog"' not in page
    assert 'id="pipeline-dialog"' in client.get("/actions").text
    for key in ('schedule.do_collect', 'schedule.do_score', 'schedule.do_apply'):
        assert f'name="{key}"' not in page
        assert f'name="{key}"' in client.get('/actions/pipeline-settings').text


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_dismiss_finished_task_preserves_history(client, status):
    from anyio.from_thread import start_blocking_portal
    from src.db.models import TaskRun, TaskStatus
    from src.web.tasks import TaskState

    manager = client.app.state.tasks
    state = TaskState(id="dismiss-qa", kind=TaskKind.LOGIN, status=TaskStatus(status), error_message="Test failure")
    manager.lane().current = state
    manager.history.append(state)
    with start_blocking_portal() as portal:
        portal.call(manager.repository.create_task_run, TaskRun(id=state.id, kind=state.kind))
        portal.call(manager.repository.finish_task_run, state.id, state.status, {}, state.error_message)
    assert '>Скрыть</button>' in client.get('/partials/status').text
    # An old page cannot dismiss a different task's message.
    client.post('/actions/dismiss', data={'task_id': 'old-task'})
    assert manager.current is state
    response = client.post('/actions/dismiss', data={'task_id': state.id})
    assert response.status_code == 200
    assert manager.current is None
    assert 'dismiss-task' not in response.text
    assert manager.history == [state]
    with start_blocking_portal() as portal:
        runs = portal.call(manager.repository.list_task_runs)
    assert runs[0]['id'] == state.id and runs[0]['status'] == status
    assert runs[0]['error_message'] == 'Test failure'
    assert 'Test failure' in client.get('/runs').text


def test_cannot_dismiss_running_task(client):
    from src.web.tasks import TaskState

    manager = client.app.state.tasks
    state = TaskState(id="running-qa", kind=TaskKind.LOGIN)
    manager.lane().current = state
    assert '>Скрыть</button>' not in client.get('/partials/status').text
    client.post('/actions/dismiss', data={'task_id': state.id})
    assert manager.current is state


def test_action_panel_and_console_only_appear_on_actions_page(client):
    for path in ("/", "/vacancies", "/applications", "/resume", "/settings", "/runs"):
        page = client.get(path).text
        assert 'href="/actions"' in page
        for element_id in ("task-panel", "console-block", "pipeline-dialog", "search-dialog"):
            assert f'id="{element_id}"' not in page
    page = client.get('/actions').text
    for element_id in ("task-panel", "console-block", "pipeline-dialog", "search-dialog"):
        assert f'id="{element_id}"' in page
    assert 'href="/actions" class="active"' in page


def test_selected_vacancies_start_and_redirect_to_actions(client, monkeypatch):
    from unittest.mock import AsyncMock
    from src.web import jobs

    job = AsyncMock(return_value={})
    monkeypatch.setattr(jobs, 'apply_job', job)
    response = client.post('/actions/apply', data={'return_to': 'actions', 'vacancy_ids': ['12', '15']}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/actions'
    assert client.app.state.tasks.current.params['vacancy_ids'] == [12, 15]
    page = client.get('/vacancies').text
    assert 'hx-target="#task-panel"' not in page


@pytest.mark.parametrize("action,lane,kind", [
    ("collect", "main", TaskKind.COLLECT), ("score", "main", TaskKind.SCORE),
    ("apply", "main", TaskKind.APPLY), ("pipeline", "main", TaskKind.COLLECT),
    ("login", "main", TaskKind.LOGIN), ("resume_touch", "main", TaskKind.RESUME_TOUCH),
    ("profile", "profile", TaskKind.PROFILE), ("activity", "activity", TaskKind.ACTIVITY),
])
def test_running_card_switches_play_to_stop_in_its_lane(client, action, lane, kind):
    import asyncio
    import re
    from anyio.from_thread import start_blocking_portal

    manager = client.app.state.tasks

    async def idle_job(ctx):
        while not ctx.should_stop():
            await asyncio.sleep(.01)
        return {}

    async def start():
        await manager.start(kind, idle_job, lane=lane, params={"action": action})

    async def wait_finished():
        await asyncio.wait_for(manager.lane(lane).task, 2)

    def card(html, name):
        return re.search(r'<article[^>]*data-action-id="' + name + r'"[^>]*>(.*?)</article>', html, re.S).group()

    with start_blocking_portal() as portal:
        portal.call(start)
        panel = client.get('/partials/status').text
        active = card(panel, action)
        assert 'is-running' in active
        assert 'is-stop' in active
        assert 'hx-post="/actions/stop"' in active
        assert f'"lane": "{lane}"' in active
        assert 'aria-label="Остановить:' in active
        if action == 'pipeline':
            assert 'is-stop' not in card(panel, 'collect')
        if lane != 'main':
            assert 'disabled' not in _stage_button(card(panel, 'login'), 'login')
        client.post('/actions/stop', data={'lane': lane})
        portal.call(wait_finished)
    stopped = card(client.get('/partials/status').text, action)
    assert 'is-running' not in stopped
    assert 'is-stop' not in stopped
    assert 'class="action-control"' in stopped


def test_apply_settings_modal_persists_only_reply_settings(client):
    from anyio.from_thread import start_blocking_portal
    from src.web.routes.actions import APPLY_SETTING_KEYS

    page = client.get('/actions').text
    assert 'aria-label="Настроить отклики"' in page
    assert 'id="apply-dialog"' in page
    modal = client.get('/actions/apply-settings').text
    for key in APPLY_SETTING_KEYS:
        assert f'name="{key}"' in modal
    assert 'name="apply.skip_questions"' not in modal
    settings = client.app.state.settings
    before = settings.all_values()
    response = client.post('/actions/apply-settings', data={
        'matching.threshold': '84', 'apply.batch_limit': '12', 'apply.delay_sec': '3.5',
        'apply.recheck_with_llm': '1', 'apply.mode': 'auto', 'cover_letter.enabled': '1',
        'cover_letter.when': 'always', 'cover_letter.model': 'letter-test',
        'cover_letter.max_chars': '900', 'cover_letter.prompt': 'Кратко',
        'cover_letter.fallback_text': 'Здравствуйте!', 'llm.enabled': '1',
        'schedule.do_apply': '1',
    })
    assert response.headers['HX-Trigger-After-Settle'] == 'applySettingsSaved'
    assert response.headers['HX-Retarget'] == '#task-panel'
    assert not client.app.state.tasks.is_busy
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.get('matching.threshold') == 84
    assert settings.get('apply.batch_limit') == 12
    assert settings.get('apply.delay_sec') == 3.5
    assert settings.get('apply.recheck_with_llm') is True
    assert settings.cover_letter_config().when == 'always'
    assert settings.cover_letter_config().fallback_text == 'Здравствуйте!'
    for key, value in before.items():
        if key not in APPLY_SETTING_KEYS:
            assert settings.get(key) == value
    assert 'value="84"' in client.get('/settings').text
    assert 'value="84"' in client.get('/actions/apply-settings').text
    client.post('/actions/apply-settings', data={'matching.threshold': '84'})
    assert settings.get('cover_letter.enabled') is False
    assert settings.get('apply.recheck_with_llm') is False


def test_invalid_reply_settings_preserve_input_and_change_nothing(client):
    from anyio.from_thread import start_blocking_portal

    settings = client.app.state.settings
    before = settings.all_values()
    response = client.post('/actions/apply-settings', data={
        'matching.threshold': '84', 'apply.batch_limit': '999',
        'cover_letter.prompt': 'Мой текст',
    })
    assert 'максимум' in response.text
    assert 'value="999"' in response.text
    assert 'Мой текст' in response.text
    assert 'HX-Trigger-After-Settle' not in response.headers
    assert settings.all_values() == before
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.all_values() == before



def test_empty_vacancy_selection_never_starts_automatic_batch(client):
    response = client.post('/actions/apply', data={'return_to': 'actions'}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/vacancies'
    assert client.app.state.tasks.current is None


def test_vacancy_empty_states_distinguish_filters(client):
    empty = client.get('/vacancies').text
    assert 'Пока нет сохранённых вакансий' in empty
    assert 'id="apply-selected"' not in empty
    filtered = client.get('/vacancies?search=nonexistent').text
    assert 'Нет вакансий по этим фильтрам' in filtered
    assert 'Сбросить фильтры' in filtered


def test_vacancy_pagination_preserves_all_filters(client, monkeypatch):
    import html
    import re
    from urllib.parse import parse_qs, urlparse
    from unittest.mock import AsyncMock

    monkeypatch.setattr(client.app.state.repository, 'count_vacancies', AsyncMock(return_value=30))
    monkeypatch.setattr(client.app.state.repository, 'list_vacancies', AsyncMock(return_value=[]))
    params = {'search': 'Python & Go #1', 'found_for_resume': '7', 'min_score': '60',
              'only_scored': '1', 'only_unapplied': '1', 'order': 'recent'}
    response = client.get('/vacancies', params=params)
    href = re.search(r'href="([^"]+)" aria-label="Страница 2"', response.text).group(1)
    query = parse_qs(urlparse(html.unescape(href)).query)
    assert query == {**{key: [value] for key, value in params.items()}, 'page': ['2']}
