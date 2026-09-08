"""HTML pages rendered with Jinja."""

import logging
import math
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from src.web.panel import panel_context
from src.web.action_settings import shared_groups
from src.web.export import vacancy_csv

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_SIZE = 25


def _min_score(value: str = Query("", alias="min_score")) -> Optional[int]:
    """An empty number input is submitted as min_score= by the browser."""
    if not value.strip():
        return None
    try:
        score = int(value)
    except ValueError:
        raise HTTPException(422, "Оценка должна быть целым числом от 0 до 100") from None
    if not 0 <= score <= 100:
        raise HTTPException(422, "Оценка должна быть целым числом от 0 до 100")
    return score


async def _render(request: Request, template: str, **context) -> HTMLResponse:
    app = request.app
    if template in ("actions.html", "partials/task_panel.html"):
        context.update(await panel_context(request))
    context.setdefault("tasks", app.state.tasks)
    context.setdefault("current_task", app.state.tasks.current)
    context.setdefault("activity_task", app.state.tasks.activity)
    context.setdefault("scheduler", app.state.scheduler)
    context.setdefault("path", request.url.path)
    # Cached verdict only — the lamp refreshes itself over htmx, so rendering
    # a page never waits on the network.
    context.setdefault("llm_health", app.state.llm_health.cached)
    return app.state.templates.TemplateResponse(request, template, context)


@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/actions", status_code=303)


@router.get("/actions", response_class=HTMLResponse)
async def actions_page(request: Request, error: str = Query("")) -> HTMLResponse:
    return await _render(request, "actions.html", error=error)


@router.get("/vacancies", response_class=HTMLResponse)
async def vacancies(
    request: Request,
    page: int = Query(1, ge=1),
    min_score: Optional[int] = Depends(_min_score),
    search: str = Query(""),
    only_unapplied: bool = Query(False),
    only_scored: bool = Query(False),
    found_for_resume: int = Query(0),
    order: str = Query("score"),
) -> HTMLResponse:
    repository = request.app.state.repository
    settings = request.app.state.settings
    resume = await repository.get_active_resume()
    resume_id = resume.id if resume else None

    filters = dict(
        resume_id=resume_id,
        min_score=min_score,
        only_unapplied=only_unapplied,
        only_scored=only_scored,
        search=search.strip(),
        found_for_resume=found_for_resume or None,
    )
    total = await repository.count_vacancies(**filters)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, pages)
    rows = await repository.list_vacancies(
        **filters, order=order, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )

    return await _render(
        request,
        "vacancies.html",
        rows=rows,
        total=total,
        page=page,
        pages=max(1, math.ceil(total / PAGE_SIZE)),
        min_score=min_score,
        search=search,
        only_unapplied=only_unapplied,
        only_scored=only_scored,
        order=order,
        resume=resume,
        resumes=await repository.list_resumes(),
        found_for_resume=found_for_resume,
        filters_active=bool(search.strip() or min_score is not None or only_scored or only_unapplied or found_for_resume),
        selectable=any(not row['application_status'] for row in rows),
        pagination_query=urlencode({key: value for key, value in {
            'search': search, 'order': order, 'min_score': min_score,
            'only_scored': '1' if only_scored else None,
            'only_unapplied': '1' if only_unapplied else None,
            'found_for_resume': found_for_resume or None,
        }.items() if value is not None}),
        threshold=int(settings.get("matching.threshold", 70)),
    )


@router.get("/vacancies/export")
async def export_vacancies(
    request: Request,
    min_score: Optional[int] = Depends(_min_score),
    search: str = Query(""),
    only_unapplied: bool = Query(False),
    only_scored: bool = Query(False),
    found_for_resume: int = Query(0),
    order: str = Query("score"),
) -> StreamingResponse:
    repository = request.app.state.repository
    resume = await repository.get_active_resume()
    rows = await repository.list_vacancies(
        resume_id=resume.id if resume else None,
        min_score=min_score, search=search.strip(),
        only_unapplied=only_unapplied, only_scored=only_scored,
        found_for_resume=found_for_resume or None, order=order,
        limit=None, include_details=True,
    )
    filename = f"vacancies-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(
        vacancy_csv(rows), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "Cache-Control": "no-store"},
    )


@router.get("/applications", response_class=HTMLResponse)
async def applications(
    request: Request,
    page: int = Query(1, ge=1),
    status: str = Query(""),
) -> HTMLResponse:
    repository = request.app.state.repository
    total = await repository.count_applications(status)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, pages)
    rows = await repository.list_applications(
        status=status, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )
    stats = await repository.get_dashboard_stats(None)

    return await _render(
        request,
        "applications.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        status=status,
        by_status=stats["by_status"],
    )


@router.get("/resume", response_class=HTMLResponse)
async def resume_page(request: Request) -> HTMLResponse:
    repository = request.app.state.repository
    settings = request.app.state.settings
    resumes = await repository.list_resumes()
    return await _render(
        request,
        "resume.html",
        resumes=resumes,
        # Cached model list — the profile model is picked right by the button.
        models=request.app.state.llm_health.models,
        profile_model=settings.profile_config().model,
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = Query(False)) -> HTMLResponse:
    settings = request.app.state.settings
    return await _render(request, "settings.html", groups=shared_groups(settings), saved=saved, errors=[])


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    runs = await request.app.state.repository.list_task_runs(limit=50)
    return await _render(request, "runs.html", runs=runs)


@router.get("/partials/status", response_class=HTMLResponse)
async def status_partial(request: Request) -> HTMLResponse:
    return await _render(request, "partials/task_panel.html")
