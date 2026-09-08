"""Shared prerequisites and plain-language context for the task panel."""

from fastapi import Request

from src.db.models import TaskKind, TaskStatus

PIPELINE_STAGE_KEYS = {"schedule.do_collect", "schedule.do_score", "schedule.do_apply"}


async def hh_login_confirmed(repository) -> bool:
    runs = await repository.list_task_runs(limit=1, kind=TaskKind.LOGIN)
    return bool(runs and runs[0]["status"] == TaskStatus.COMPLETED.value
                and runs[0]["result"].get("logged_in") is True)


async def panel_context(request: Request) -> dict:
    settings = request.app.state.settings
    stages = []
    if settings.get("schedule.do_collect", True):
        stages.append("поиск вакансий")
    if settings.get("schedule.do_score", True) and settings.get("matching.enabled", True):
        stages.append("оценка соответствия резюме")
    if settings.get("schedule.do_apply", False) and settings.get("apply.mode", "manual") == "auto":
        stages.append("отправка откликов работодателям")
    return {
        "panel_resume": await request.app.state.repository.get_active_resume(),
        "panel_hh_logged_in": await hh_login_confirmed(request.app.state.repository),
        "panel_llm_enabled": bool(settings.get("llm.enabled", False)),
        "panel_threshold": int(settings.get("matching.threshold", 70)),
        "panel_apply_limit": int(settings.get("apply.batch_limit", 20)),
        "panel_pipeline_stages": stages,
        "panel_pipeline_applies": "отправка откликов работодателям" in stages,
    }
