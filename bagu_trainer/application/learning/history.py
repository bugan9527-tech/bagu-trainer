"""History is available independently of the bank, model and learning engine."""
import ntpath
import time
from datetime import datetime, timezone

from bagu_trainer.infrastructure import config
from bagu_trainer.infrastructure.store import Store


def history_time(timestamp):
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except (ValueError, TypeError, OverflowError, OSError):
        return "旧记录未提供有效时间"


class LearningHistory:
    def dashboard(self):
        settings = config.read_user_config()
        roots = settings.get("project_roots") or []
        path = config.db_path(settings.get("db_path") or "")
        stats = {"ok": True, "total_attempts": 0, "accuracy": 0, "due_reviews": 0, "pending": 0}
        expire = int(settings.get("quiz_ttl_seconds") or config.DEFAULTS["quiz_ttl_seconds"]) > 0
        stats.update(Store.dashboard_stats(path, time.time(), expire_pending=expire))
        return {"state": "not_ready", "projects": roots, "stats": stats}

    def get_answer_history(self, **filters):
        try:
            result = Store.history_page(config.db_path(config.read_user_config().get("db_path") or ""), **filters)
            for item in result["items"]:
                item.update(time_text=history_time(item["answered_at"]),
                    project_name=ntpath.basename(item["project_root"].rstrip("/\\")) or "未记录项目",
                    result_text="答对" if item["correct"] else "答错")
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "note": f"查询失败（{type(error).__name__}），请修复配置或数据库后重试。"}

    def get_answer_detail(self, attempt_id):
        try:
            row = Store.history_detail(config.db_path(config.read_user_config().get("db_path") or ""), int(attempt_id))
            if not row:
                return {"ok": False, "note": "作答记录已不存在，请刷新列表。"}
            return {"ok": True, **row, "time_text": history_time(row["answered_at"]),
                "can_retry": False, "retry_reason": "题库或模型未就绪，请在侧栏完成准备后重练。"}
        except Exception as error:
            return {"ok": False, "note": f"详情读取失败（{type(error).__name__}），请重试。"}
