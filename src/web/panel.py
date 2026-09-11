"""Shared prerequisites and plain-language context for the task panel."""

from fastapi import Request

from src.db.models import TaskKind, TaskStatus
from src.web.tasks import LANE_PROFILE

PIPELINE_STAGE_KEYS = {"schedule.do_collect", "schedule.do_score", "schedule.do_apply"}


def llm_card_context(request: Request) -> dict:
    state = request.app.state
    runtime = state.aistudio
    return {
        "aistudio": runtime.snapshot(),
        "llm_health": state.llm_health.cached,
        "panel_llm_models": runtime.models if runtime.selected else state.llm_health.models,
        "panel_llm_model": str(state.settings.get("llm.model", "")),
        "panel_llm_busy": state.tasks.is_busy or state.tasks.lane(LANE_PROFILE).is_busy,
    }


async def hh_account_status(repository) -> dict:
    settings = await repository.get_all_settings()
    if settings.get("hh.login_required") == "true":
        return {"logged_in": False, "name": "", "confirmed_at": None}
    runs = await repository.list_task_runs(limit=1, kind=TaskKind.LOGIN)
    result = runs[0]["result"] if runs else {}
    confirmed = bool(runs and result.get("logged_in") is True and (
        runs[0]["status"] == TaskStatus.COMPLETED.value or result.get("account_verified") is True))
    if (runs and not confirmed and runs[0]["params"].get("switch_account")
            and result.get("session_preserved", True)):
        previous = runs[0]["params"].get("previous_account")
        if previous:
            return previous
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
    aistudio = request.app.state.aistudio.snapshot()
    stages = []
    if settings.get("schedule.do_collect", True):
        stages.append("поиск вакансий")
    if settings.get("schedule.do_score", True) and settings.get("matching.enabled", True):
        stages.append("оценка соответствия резюме")
    if settings.get("schedule.do_apply", False) and settings.get("apply.mode", "manual") == "auto":
        stages.append("отправка откликов работодателям")
    return {
        "panel_resume": resume,
        "panel_standalone_search": (await request.app.state.repository.get_all_settings()).get("resume.selection") == "none",
        **llm_card_context(request),
        "panel_resumes": await request.app.state.repository.list_resumes(),
        "panel_search_query": resume.search_query if resume else settings.search_query,
        "panel_pipeline_collects": bool(settings.get("schedule.do_collect", True)),
        "panel_pipeline_needs_resume": any(stage != "поиск вакансий" for stage in stages),
        "panel_hh_logged_in": account["logged_in"],
        "panel_hh_account": account,
        "panel_remote_hh": request.app.state.screens.enabled("hh"),
        "panel_hh_manual": request.app.state.screens.active("hh"),
        "panel_llm_enabled": bool(settings.get("llm.enabled", False)) and (
            not aistudio["selected"] or aistudio["state"] == "ready"),
        "panel_threshold": int(settings.get("matching.threshold", 70)),
        "panel_apply_limit": int(settings.get("apply.batch_limit", 20)),
        "panel_pipeline_stages": stages,
        "panel_pipeline_applies": "отправка откликов работодателям" in stages,
    }
