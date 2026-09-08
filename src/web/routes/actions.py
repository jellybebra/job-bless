"""Actions: starting jobs, editing settings, managing resumes, SSE stream."""

import asyncio
import functools
import json
import logging
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from src.db.models import TaskKind
from src.web import jobs
from src.web.action_settings import ACTION_KEYS, ACTION_SECTIONS, SHARED_KEYS, action_sections, shared_groups
from src.web.panel import PIPELINE_STAGE_KEYS, hh_login_confirmed, panel_context
from src.web.tasks import LANE_ACTIVITY, LANE_MAIN, LANE_PROFILE, TaskBusyError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/actions")

# Also the worst-case delay before a stream notices the server is shutting down.
HEARTBEAT_SECONDS = 3


async def _panel(request: Request, error: str = "") -> HTMLResponse:
    app = request.app
    return app.state.templates.TemplateResponse(
        request,
        "partials/task_panel.html",
        {
            "tasks": app.state.tasks,
            "current_task": app.state.tasks.current,
            "activity_task": app.state.tasks.activity,
            "scheduler": app.state.scheduler,
            "llm_health": app.state.llm_health.cached,
            "error": error,
            **await panel_context(request),
        },
    )


# What the runner offers on the panel: kind -> (task kind, job, lane).
RUNNABLE_JOBS = {
    "collect": (TaskKind.COLLECT, "collect_job", LANE_MAIN),
    "score": (TaskKind.SCORE, "score_job", LANE_MAIN),
    "apply": (TaskKind.APPLY, "apply_job", LANE_MAIN),
    "pipeline": (TaskKind.COLLECT, "pipeline_job", LANE_MAIN),
    "resume_touch": (TaskKind.RESUME_TOUCH, "resume_touch_job", LANE_MAIN),
    "login": (TaskKind.LOGIN, "login_job", LANE_MAIN),
    # Runs alongside everything else, in its own tab.
    "activity": (TaskKind.ACTIVITY, "activity_job", LANE_ACTIVITY),
    # No browser involved: runs in its own lane, parallel to everything.
    "profile": (TaskKind.PROFILE, "profile_job", LANE_PROFILE),
}


async def _start(request: Request, kind: TaskKind, job, params=None, lane: str = LANE_MAIN) -> HTMLResponse:
    if kind == TaskKind.ACTIVITY and not await hh_login_confirmed(request.app.state.repository):
        return await _panel(request, error="Для фонового просмотра сначала войдите в hh.ru.")
    try:
        await request.app.state.tasks.start(kind, job, params=params or {}, lane=lane)
    except TaskBusyError as e:
        return await _panel(request, error=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("could not start task %s", kind.value)
        return await _panel(request, error=str(e))
    return await _panel(request)


# --- jobs ---------------------------------------------------------------

@router.post("/start", response_class=HTMLResponse)
async def start_selected(request: Request, kind: str = Form("collect")) -> HTMLResponse:
    """Single entry point for the runner: pick a job, then press «Старт»."""
    entry = RUNNABLE_JOBS.get(kind)
    if not entry:
        return await _panel(request, error=f"неизвестная задача «{kind}»")

    task_kind, job_name, lane = entry
    return await _start(request, task_kind, getattr(jobs, job_name), params={"action": kind}, lane=lane)


@router.post("/collect", response_class=HTMLResponse)
async def start_collect(request: Request) -> HTMLResponse:
    return await _start(request, TaskKind.COLLECT, jobs.collect_job)


@router.post("/score", response_class=HTMLResponse)
async def start_score(request: Request) -> HTMLResponse:
    return await _start(request, TaskKind.SCORE, jobs.score_job)


@router.post("/pipeline", response_class=HTMLResponse)
async def start_pipeline(request: Request) -> HTMLResponse:
    return await _start(request, TaskKind.COLLECT, jobs.pipeline_job, params={"action": "pipeline"})


@router.post("/apply", response_class=HTMLResponse)
async def start_apply(
    request: Request,
    vacancy_ids: Optional[List[int]] = Form(None),
    return_to: str = Form(""),
):
    if return_to == "actions" and not vacancy_ids:
        return RedirectResponse("/vacancies", status_code=303)
    job = functools.partial(jobs.apply_job, vacancy_ids=vacancy_ids) if vacancy_ids else jobs.apply_job
    response = await _start(
        request, TaskKind.APPLY, job, params={"vacancy_ids": vacancy_ids or []}
    )
    if return_to == "actions":
        error = response.context.get("error", "")
        return RedirectResponse("/actions" + ("?" + urlencode({"error": error}) if error else ""), status_code=303)
    return response


@router.post("/login", response_class=HTMLResponse)
async def start_login(request: Request, switch_account: bool = Form(False)) -> HTMLResponse:
    return await _start(request, TaskKind.LOGIN, jobs.login_job,
                        params={"switch_account": switch_account})


@router.post("/activity", response_class=HTMLResponse)
async def start_activity(request: Request) -> HTMLResponse:
    return await _start(request, TaskKind.ACTIVITY, jobs.activity_job, lane=LANE_ACTIVITY)


@router.post("/stop", response_class=HTMLResponse)
async def stop_task(request: Request, lane: str = Form(LANE_MAIN)) -> HTMLResponse:
    stopped = request.app.state.tasks.request_stop(lane)
    return await _panel(request, error="" if stopped else "нет выполняющейся задачи")


@router.post("/dismiss", response_class=HTMLResponse)
async def dismiss_task(request: Request, task_id: str = Form(...), lane: str = Form(LANE_MAIN)) -> HTMLResponse:
    try:
        request.app.state.tasks.dismiss(task_id, lane)
    except TaskBusyError as error:
        return await _panel(request, error=str(error))
    return await _panel(request)


@router.post("/confirm", response_class=HTMLResponse)
async def confirm_task(request: Request, lane: str = Form(LANE_MAIN)) -> HTMLResponse:
    confirmed = request.app.state.tasks.confirm(lane)
    return await _panel(request, error="" if confirmed else "задача не ждёт подтверждения")


# --- resume -------------------------------------------------------------

@router.post("/resume/import")
async def import_resume(request: Request, resume_url: str = Form(...)) -> RedirectResponse:
    job = functools.partial(jobs.resume_import_job, resume_url=resume_url.strip())
    try:
        await request.app.state.tasks.start(
            TaskKind.RESUME_IMPORT, job, params={"resume_url": resume_url.strip()}
        )
    except TaskBusyError as e:
        return RedirectResponse("/actions?" + urlencode({"error": str(e)}), status_code=303)
    return RedirectResponse("/actions", status_code=303)


async def _search_settings(request: Request, error: str = "", raw=None, errors=None) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "partials/search_settings.html", {
            **await panel_context(request), "tasks": request.app.state.tasks, "error": error,
            "sections": action_sections(request.app.state.settings, "search", raw),
            "errors": errors or [], "raw": raw,
        },
    )


@router.get("/search-settings", response_class=HTMLResponse)
async def search_settings(request: Request) -> HTMLResponse:
    return await _search_settings(request)


@router.post("/search-settings", response_class=HTMLResponse)
async def save_search_settings(request: Request) -> HTMLResponse:
    raw = {key: str(value) for key, value in (await request.form()).multi_items()}
    repository = request.app.state.repository
    resume = await repository.get_active_resume()
    query = raw.get("search_query", "").strip()
    errors = []
    if "resume_id" in raw:
        if not resume or str(resume.id) != raw["resume_id"]:
            errors.append("Активное резюме изменилось. Откройте настройки заново.")
        elif query != resume.search_query and request.app.state.tasks.is_busy:
            errors.append("Дождитесь завершения текущей задачи, чтобы изменить запрос.")
    if not errors:
        errors = await request.app.state.settings.save(raw, keys=ACTION_KEYS["search"])
    if errors:
        return await _search_settings(request, raw=raw, errors=errors)
    if "resume_id" in raw and resume and query != resume.search_query:
        await repository.update_resume_fields(resume.id, query, resume.context_text)
    return await _settings_saved(request, "search")


@router.post("/resume/{resume_id}/search-query", response_class=HTMLResponse)
async def update_search_query(
    request: Request, resume_id: int, search_query: str = Form(""),
) -> HTMLResponse:
    """Edit the active resume's query without changing its experience context."""
    repository = request.app.state.repository
    resume = await repository.get_active_resume()
    if not resume or resume.id != resume_id:
        return await _search_settings(request, error="Активное резюме изменилось. Повторите ввод запроса для выбранного резюме.")
    if request.app.state.tasks.is_busy:
        return await _search_settings(request, error="Дождитесь завершения текущей задачи, чтобы изменить запрос.")
    await repository.update_resume_fields(resume.id, search_query.strip(), resume.context_text)
    response = await _panel(request)
    # The dashboard and the resume edit form display the same query.
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/resume/{resume_id}/update")
async def update_resume(
    request: Request,
    resume_id: int,
    search_query: str = Form(""),
    context_text: str = Form(""),
) -> RedirectResponse:
    """Save the two hand-edited fields and refresh the profile if they changed."""
    repository = request.app.state.repository
    before = await repository.get_resume(resume_id)
    await repository.update_resume_fields(resume_id, search_query.strip(), context_text.strip())

    context_changed = bool(before) and before.context_text.strip() != context_text.strip()
    if context_changed and bool(request.app.state.settings.get("llm.enabled", False)):
        # The profile is built from this text, so it is rebuilt in its own lane
        # — it needs no browser and must not wait for a collection run.
        job = functools.partial(jobs.profile_job, resume_id=resume_id)
        try:
            await request.app.state.tasks.start(
                TaskKind.PROFILE, job, params={"resume_id": resume_id}, lane=LANE_PROFILE
            )
        except TaskBusyError as e:
            logger.info("profile rebuild postponed: %s", e)

    return RedirectResponse("/resume", status_code=303)


@router.post("/resume/{resume_id}/profile")
async def rebuild_profile(
    request: Request, resume_id: int, model: str = Form("")
) -> RedirectResponse:
    model = model.strip()
    if model:
        # Picked next to the button; remembered so it is preselected next time.
        await request.app.state.settings.save({"profile.model": model})

    job = functools.partial(jobs.profile_job, resume_id=resume_id, model=model)
    try:
        await request.app.state.tasks.start(
            TaskKind.PROFILE, job, params={"resume_id": resume_id}, lane=LANE_PROFILE
        )
    except TaskBusyError as e:
        return RedirectResponse("/actions?" + urlencode({"error": str(e)}), status_code=303)
    return RedirectResponse("/actions", status_code=303)


@router.post("/resume/{resume_id}/activate")
async def activate_resume(request: Request, resume_id: int) -> RedirectResponse:
    await request.app.state.repository.set_active_resume(resume_id)
    return RedirectResponse("/resume", status_code=303)


@router.post("/resume/{resume_id}/delete")
async def delete_resume(request: Request, resume_id: int) -> RedirectResponse:
    await request.app.state.repository.delete_resume(resume_id)
    return RedirectResponse("/resume", status_code=303)


# --- settings -----------------------------------------------------------

APPLY_SETTING_SECTIONS = ACTION_SECTIONS["apply"]
APPLY_SETTING_KEYS = ACTION_KEYS["apply"]


async def _apply_settings(request: Request, errors=None, raw=None) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, "partials/apply_settings.html", {
        "sections": action_sections(request.app.state.settings, "apply", raw),
        "errors": errors or [],
    })


@router.get("/apply-settings", response_class=HTMLResponse)
async def apply_settings(request: Request) -> HTMLResponse:
    return await _apply_settings(request)


@router.post("/apply-settings", response_class=HTMLResponse)
async def save_apply_settings(request: Request) -> HTMLResponse:
    raw = {key: str(value) for key, value in (await request.form()).multi_items()}
    errors = await request.app.state.settings.save(raw, keys=APPLY_SETTING_KEYS)
    if errors:
        return await _apply_settings(request, errors=errors, raw=raw)
    return await _settings_saved(request, "apply")


async def _settings_saved(request: Request, kind: str) -> HTMLResponse:
    response = await _panel(request)
    response.headers["HX-Retarget"] = "#task-panel"
    response.headers["HX-Reswap"] = "outerHTML"
    response.headers["HX-Trigger-After-Settle"] = f"{kind}SettingsSaved"
    return response


@router.get("/pipeline-settings", response_class=HTMLResponse)
async def pipeline_settings(request: Request) -> HTMLResponse:
    return await _pipeline_settings(request)


async def _pipeline_settings(request: Request, raw=None, errors=None) -> HTMLResponse:
    settings = request.app.state.settings
    return request.app.state.templates.TemplateResponse(
        request, "partials/pipeline_settings.html", {
            "do_collect": raw.get("schedule.do_collect") == "1" if raw is not None else settings.get("schedule.do_collect", True),
            "do_score": raw.get("schedule.do_score") == "1" if raw is not None else settings.get("schedule.do_score", True) and settings.get("matching.enabled", True),
            "do_apply": raw.get("schedule.do_apply") == "1" if raw is not None else settings.get("schedule.do_apply", False) and settings.get("apply.mode", "manual") == "auto",
            "llm_enabled": settings.get("llm.enabled", False),
            "sections": action_sections(settings, "pipeline", raw), "errors": errors or [],
        },
    )


@router.post("/pipeline-settings", response_class=HTMLResponse)
async def save_pipeline_settings(request: Request) -> HTMLResponse:
    form = await request.form()
    settings = request.app.state.settings
    values = {key: str(value) for key, value in form.multi_items()}
    values.update({key: "1" if form.get(key) == "1" else "" for key in PIPELINE_STAGE_KEYS})
    keys = set(PIPELINE_STAGE_KEYS) | ACTION_KEYS["pipeline"]
    # The switches represent effective stages, including their existing gates.
    if values["schedule.do_score"]:
        values["matching.enabled"] = "1"
        keys.add("matching.enabled")
    if values["schedule.do_apply"]:
        values["apply.mode"] = "auto"
        keys.add("apply.mode")
    errors = await settings.save(values, keys=keys)
    if errors:
        return await _pipeline_settings(request, raw=values, errors=errors)
    request.app.state.scheduler.reschedule()
    response = await _panel(request)
    response.headers["HX-Retarget"] = "#task-panel"
    response.headers["HX-Reswap"] = "outerHTML"
    response.headers["HX-Trigger-After-Settle"] = json.dumps({"pipelineSettingsSaved": {
        "matchingEnabled": settings.get("matching.enabled", False),
        "applyMode": settings.get("apply.mode", "manual"),
    }})
    return response


@router.get("/{kind}-settings", response_class=HTMLResponse)
async def action_settings(request: Request, kind: str) -> HTMLResponse:
    return await _action_settings(request, kind)


async def _action_settings(request: Request, kind: str, raw=None, errors=None) -> HTMLResponse:
    if kind not in {"score", "profile", "activity", "resume_touch"}:
        raise HTTPException(status_code=404)
    return request.app.state.templates.TemplateResponse(request, "partials/action_settings.html", {
        "kind": kind, "sections": action_sections(request.app.state.settings, kind, raw), "errors": errors or [],
    })


@router.post("/{kind}-settings", response_class=HTMLResponse)
async def save_action_settings(request: Request, kind: str) -> HTMLResponse:
    if kind not in {"score", "profile", "activity", "resume_touch"}:
        raise HTTPException(status_code=404)
    raw = {key: str(value) for key, value in (await request.form()).multi_items()}
    errors = await request.app.state.settings.save(raw, keys=ACTION_KEYS[kind])
    if errors:
        return await _action_settings(request, kind, raw=raw, errors=errors)
    if kind in {"activity", "resume_touch"}:
        request.app.state.scheduler.reschedule()
    return await _settings_saved(request, kind)


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request) -> HTMLResponse:
    form = await request.form()
    raw = {key: str(value) for key, value in form.multi_items()}

    settings = request.app.state.settings
    errors = await settings.save(raw, keys=SHARED_KEYS)
    if errors:
        return request.app.state.templates.TemplateResponse(
            request,
            "settings.html",
            {
                **await panel_context(request),
                "activity_task": request.app.state.tasks.activity,
                "llm_health": request.app.state.llm_health.cached,
                "groups": shared_groups(settings),
                "errors": errors,
                "saved": False,
                "tasks": request.app.state.tasks,
                "current_task": request.app.state.tasks.current,
                "scheduler": request.app.state.scheduler,
                "path": "/settings",
            },
            status_code=400,
        )

    request.app.state.scheduler.reschedule()
    request.app.state.llm_health.invalidate()  # endpoint/key/model may have changed
    request.app.state.limiter.reconfigure(settings.ratelimit_config())
    return RedirectResponse("/settings?saved=1", status_code=303)


# --- llm connectivity ---------------------------------------------------

@router.get("/llm-models", response_class=HTMLResponse)
async def llm_models(
    request: Request, force: bool = False, field: str = "llm.model"
) -> HTMLResponse:
    """Model dropdown for a model-typed setting, filled from the endpoint."""
    monitor = request.app.state.llm_health
    health = await monitor.get(force=force)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/model_field.html",
        {
            "models": monitor.models,
            "current": str(request.app.state.settings.get(field, "")),
            "field_key": field,
            # Only the main model must be set; the others may follow it.
            "allow_empty": field != "llm.model",
            "error": "" if health.ok else health.message,
        },
    )


@router.get("/llm-health", response_class=HTMLResponse)
async def llm_health(request: Request, force: bool = False, compact: bool = False) -> HTMLResponse:
    health = await request.app.state.llm_health.get(force=force)
    # `initial` stays False here: a refreshed lamp that still carried
    # hx-trigger="load" would re-request itself instantly, forever.
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/llm_status.html",
        {"llm_health": health, "compact": compact, "initial": False},
    )


# --- live updates -------------------------------------------------------

@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    manager = request.app.state.tasks
    queue = manager.subscribe()

    async def stream():
        try:
            state = manager.current
            if state:
                yield _sse({"type": "snapshot", "task": state.as_dict()})
                for line in list(state.logs):
                    yield _sse({"type": "log", "line": line, "task": state.as_dict()})
            while True:
                # An endless stream would make uvicorn hang on Ctrl+C waiting
                # for this connection, so the server closes it itself.
                if request.app.state.shutting_down:
                    yield _sse({"type": "server_closing"})
                    break
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(event)
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
