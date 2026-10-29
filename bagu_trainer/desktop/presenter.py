"""Pure assembly of the existing desktop snapshot and user-facing messages."""
from bagu_trainer.application.events import public_value


def snapshot(*, version, ready, error, checks, status, paused, projects,
             project_states, pending, stats, model, model_task, bank_task,
             sources, settings, user_data, bank_count):
    """Produce the unchanged renderer snapshot shape from application facts."""
    return {
        "version": version, "ready": ready, "error": error,
        "checks": public_value(checks), "status": status, "paused": paused,
        "projects": projects, "project_states": project_states,
        "pending": pending, "stats": stats, "model": model,
        "model_task": model_task, "bank_task": bank_task,
        "sources": public_value(sources), "settings": settings,
        "user_data": str(user_data), "bank_count": bank_count,
    }


_MONITOR_STATUS = {
    "watching": "正在监听项目；文件稳定 15 秒后匹配知识点。",
    "quiz_ready": "新题已准备好，请在练习中心查看。",
    "generation_unavailable": "本次出题未完成，可手动重试。",
    "project_unavailable": "有项目目录不可读取，请检查监听项目。",
    "paused": "监听已暂停，仍可练习已有题目。",
}


def monitor_status(result, previous):
    return _MONITOR_STATUS.get((result or {}).get("state"), previous)


def monitor_failure(error):
    return f"本次检测未完成（{type(error).__name__}），请检查目录或模型配置。"


def readiness_status(ready, error):
    return "正在监听项目；文件稳定 15 秒后匹配知识点。" if ready else (
        error or "请完成题库、模型和监听项目的准备。")
