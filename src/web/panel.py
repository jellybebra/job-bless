"""Shared prerequisites and plain-language context for the task panel."""

from fastapi import Request

from src.db.models import TaskKind, TaskStatus

PIPELINE_STAGE_KEYS = {"schedule.do_collect", "schedule.do_score", "schedule.do_apply"}


async def hh_account_status(repository) -> dict:
    runs = await repository.list_task_runs(limit=1, kind=TaskKind.LOGIN)
    confirmed = bool(runs and runs[0]["status"] == TaskStatus.COMPLETED.value
                     and runs[0]["result"].get("logged_in") is True)
    return {
        "logged_in": confirmed,
        "name": runs[0]["result"].get("account_name", "") if confirmed else "",
        "confirmed_at": runs[0]["finished_at"] if confirmed else None,
    }


async def hh_login_confirmed(repository) -> bool:
    return (await hh_account_status(repository))["logged_in"]


async def panel_context(request: Request) -> dict:
    settings = request.app.state.settings
    resume = await request.app.state.repository.get_active_resume()
    account = await hh_account_status(request.app.state.repository)
    stages = []
    if settings.get("schedule.do_collect", True):
        stages.append("поиск вакансий")
    if settings.get("schedule.do_score", True) and settings.get("matching.enabled", True):
        stages.append("оценка соответствия резюме")
    if settings.get("schedule.do_apply", False) and settings.get("apply.mode", "manual") == "auto":
        stages.append("отправка откликов работодателям")
    return {
        "panel_resume": resume,
        "panel_search_query": resume.search_query if resume else settings.search_query,
        "panel_pipeline_collects": bool(settings.get("schedule.do_collect", True)),
        "panel_pipeline_needs_resume": any(stage != "поиск вакансий" for stage in stages),
        "panel_hh_logged_in": account["logged_in"],
        "panel_hh_account": account,
        "panel_llm_enabled": bool(settings.get("llm.enabled", False)),
        "panel_threshold": int(settings.get("matching.threshold", 70)),
        "panel_apply_limit": int(settings.get("apply.batch_limit", 20)),
        "panel_pipeline_stages": stages,
        "panel_pipeline_applies": "отправка откликов работодателям" in stages,
    }
