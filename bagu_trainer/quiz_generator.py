# -*- coding: utf-8 -*-
"""出题模块：用大模型把「命中知识点 + 来源正文片段 + AI 当前工作上下文」
合成情景选择题。API 采用通用 OpenAI 兼容接口（POST {base_url}/chat/completions），
key 经 bagu_trainer.secrets 解析（环境变量 > 凭据管理器 > 旧配置文件兼容位），
不向项目目录写明文。

隐私硬约束：
  - 上下文进入 LLM 前统一脱敏（当前 Key / sk-* / Bearer / 赋值式 / 私钥块）
    并截断到 CONTEXT_MAX_CHARS；
  - 异常信息只保留异常类名，绝不携带请求/响应细节（防密钥与内容外泄）。

产出契约（generate 返回 dict）：
  - available=True ：四选项齐备、答案/解析合法的可作答题目
      （generated_by=llm；或题库已验证选择题回退 generated_by=fallback）
  - available=False：生成失败且无可靠回退题，带 unavailable_reason，
      绝不返回空选项/空答案的题目

硬性规则：
  - LLM 输出必须通过严格校验（四选项非空互异、A-D 合法答案、非空解析、
    题目长度合理），校验失败一律视为生成失败；
  - 题目标题在题库中没有来源正文片段时不调用 LLM（拒绝无依据合成）；
  - 回退只使用题库中「选项/答案/解析/来源齐备」的已验证选择题，
    没有可靠回退题时返回明确 unavailable，不凭空生成标准答案。
"""
import json
import re
import sys
import urllib.error
import urllib.request

from . import fallback_bank, secrets
from .config import load_config
from .source_extractor import MIN_USABLE_EXCERPT_CHARS, assess_excerpt

SYSTEM_PROMPT = (
    "你是技术面试出题助手。依据用户提供的「来源片段」作为唯一事实依据，"
    "出一道结合给定工作上下文的单选题（四个选项）。要求：\n"
    "1. 题目、四个选项、正确答案与解析必须与来源片段内容一致，"
    "禁止编造来源片段中不存在的事实；\n"
    "2. 四个选项必须互不相同且都与题干相关，正确答案必须唯一且确凿；\n"
    "3. 解析必须引用来源片段中的关键结论，说明为什么正确以及易错点；\n"
    "4. 题目与选项中永远不得暗示或暴露正确答案（答案仅用于服务端判分，"
    "不会展示给答题方）；\n"
    '5. 只输出一个 JSON 对象，格式：{"question": str, '
    '"options": {"A": str, "B": str, "C": str, "D": str}, '
    '"answer": "选项字母", "analysis": str}'
)

QUESTION_MIN_LEN = 5
QUESTION_MAX_LEN = 500
CONTEXT_MAX_CHARS = 2000   # 发送给 LLM 的上下文字符上限


def test_llm(base_url: str, api_key: str, model: str, timeout: int = 8) -> str:
    """发一个最小 chat/completions 请求验证接口，返回结果说明文字。
    供桌面配置窗口调用。返回值经脱敏，不含 Key。
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "max_tokens": 1,
               "messages": [{"role": "user", "content": "hi"}]}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=max(4, timeout)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        reply = secrets.redact_text(str(reply), [api_key])
        return f"连接成功，模型 {model} 正常响应（{reply[:20]}…）" if reply.strip() else "连接成功"
    except urllib.error.HTTPError as e:
        msg = {401: "API Key 无效或未授权", 404: "接口路径不存在(检查 base_url)",
               429: "请求过于频繁/余额不足"}
        return f"HTTP {e.code}: {msg.get(e.code, e.reason)}"
    except (urllib.error.URLError, TimeoutError):
        return "网络失败/超时：检查网络、代理或 base_url"


class QuizGenerator:
    def __init__(self, config: dict = None, urlopen=None):
        # 构造参数仅作兼容（config.json 不存在等场景）；出题时始终实时读配置
        self._config = config or {}
        # urlopen 可注入（测试用 fake，避免真实网络）
        self._urlopen = urlopen or urllib.request.urlopen

    @staticmethod
    def _current_llm() -> dict:
        """读取 LLM 配置并把 api_key 解析为当前生效值（env/keyring/旧文件）"""
        try:
            llm = load_config().get("llm")
        except Exception:
            return None
        if not llm:
            return None
        key = secrets.resolve_api_key(llm)
        return {**llm, "api_key": key} if key else llm

    # ---- 对外入口 ----
    def generate(self, kp_question: dict, context_text: str) -> dict:
        """返回完整 quiz dict（available=True 时含 answer/analysis，作答前不外发；
        available=False 时含 unavailable_reason，无 question/options/answer）。
        """
        excerpt = (kp_question.get("source_excerpt") or "").strip()
        quality = assess_excerpt(excerpt)
        declared_usable = kp_question.get("usable_excerpt")
        quality_label = kp_question.get("excerpt_quality")
        usable = quality["usable"]
        if declared_usable is False or quality_label in ("weak", "empty"):
            usable = False
        base = {
            "kp_id": kp_question["id"],
            "kp_name": kp_question["knowledge_point"],
            "difficulty": kp_question["difficulty"],
            "excerpt_source": kp_question.get("excerpt_source") or "",
            "excerpt_quality": quality["quality"],
            "usable_excerpt": usable,
        }
        llm = self._current_llm()  # 每次出题都实时读配置，配置变更免重启生效
        if llm and llm.get("api_key"):
            # 进 LLM 前统一脱敏 + 截断（防御性：调用方漏脱敏也不外泄）
            context_text = secrets.redact_text(
                str(context_text or ""), [llm["api_key"]])[:CONTEXT_MAX_CHARS]
        fail_reason = None
        if llm and llm.get("api_key"):
            if not excerpt:
                fail_reason = "来源正文缺失(source_excerpt 为空)，拒绝无依据合成"
            elif not usable:
                fail_reason = (
                    f"来源片段不可用({quality['reason']}：有效正文"
                    f"{quality['effective_chars']}字符，低于"
                    f"{MIN_USABLE_EXCERPT_CHARS})，拒绝无依据合成"
                )
            else:
                try:
                    quiz = self._call_llm(llm, kp_question, context_text, excerpt)
                    if quiz:
                        quiz.update(base)
                        quiz["generated_by"] = "llm"
                        return quiz
                    fail_reason = "LLM 输出校验失败(四选项/答案/解析/题长不合法)"
                except urllib.error.HTTPError as e:
                    # Status is useful for diagnosis; response bodies and headers
                    # can contain credentials and must never be logged.
                    fail_reason = f"LLM 调用失败: HTTP {e.code}"
                except Exception as e:
                    # 只保留异常类名：异常文本可能携带请求/响应细节（含密钥）
                    fail_reason = f"LLM 调用失败: {e.__class__.__name__}"
        else:
            fail_reason = "未配置 LLM（请在主窗口打开模型配置，或设置环境变量 BAGU_LLM_API_KEY）"
        print(f"[quiz] {fail_reason} -> 尝试题库已验证选择题回退 ({kp_question['id']})",
              file=sys.stderr)
        return self._fallback(kp_question, base, fail_reason)

    # ---- LLM 合成 ----
    def _call_llm(self, llm: dict, kp_question: dict, context_text: str,
                  excerpt: str):
        url = llm["base_url"].rstrip("/") + "/chat/completions"
        user_msg = (
            f"知识点：{kp_question['knowledge_point']}\n"
            f"题目标题：{kp_question['question']}\n"
            f"来源片段（唯一事实依据）：\n\"\"\"\n{excerpt}\n\"\"\"\n\n"
            f"AI 当前工作上下文：\n{context_text[:CONTEXT_MAX_CHARS]}"
        )
        payload = {
            "model": llm.get("model", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.7,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {llm['api_key']}",
            },
        )
        with self._urlopen(req, timeout=llm.get("timeout_sec", 30)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        return self._parse_quiz(content)

    # ---- 严格校验 ----
    @staticmethod
    def _parse_quiz(content):
        """从 LLM 输出中提取 JSON 并严格校验，任一不合法返回 None。

        校验项：question/options/answer/analysis 字段齐全；选项恰为 A/B/C/D
        四键且全部非空、互不相同；答案必须是 A-D；解析非空；题目长度合理。
        """
        if not isinstance(content, str):
            return None
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
        try:
            d = json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                return None
            try:
                d = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        if not isinstance(d, dict):
            return None
        if not all(k in d for k in ("question", "options", "answer", "analysis")):
            return None
        opts = d["options"]
        if not isinstance(opts, dict) or sorted(opts.keys()) != ["A", "B", "C", "D"]:
            return None
        vals = []
        for k in ("A", "B", "C", "D"):
            v = opts.get(k)
            if not isinstance(v, str) or not v.strip():
                return None
            vals.append(v.strip())
        if len(set(vals)) != 4:
            return None
        if not isinstance(d["answer"], str) or d["answer"].strip().upper() not in ("A", "B", "C", "D"):
            return None
        analysis = d["analysis"]
        if not isinstance(analysis, str) or not analysis.strip():
            return None
        question = d["question"]
        if not isinstance(question, str):
            return None
        question = question.strip()
        if not (QUESTION_MIN_LEN <= len(question) <= QUESTION_MAX_LEN):
            return None
        return {
            "question": question,
            "options": dict(zip(("A", "B", "C", "D"), vals)),
            "answer": d["answer"].strip().upper(),
            "analysis": analysis.strip(),
        }

    # ---- 回退 ----
    @staticmethod
    def _is_verified(q: dict) -> bool:
        """题库已验证选择题：四选项齐备互异、A-D 答案、非占位解析、来源可溯。"""
        opts = q.get("options") or {}
        if not isinstance(opts, dict) or sorted(opts.keys()) != ["A", "B", "C", "D"]:
            return False
        vals = [str(v).strip() for v in opts.values()]
        if any(not v for v in vals) or len(set(vals)) != 4:
            return False
        if q.get("answer") not in ("A", "B", "C", "D"):
            return False
        analysis = (q.get("analysis") or "").strip()
        if not analysis or analysis.startswith("来源:"):
            return False
        if not (q.get("source_paths") or q.get("excerpt_source")
                or q.get("manual_verified") is True or "来源" in analysis):
            return False
        if (str(q.get("generated_by") or "").lower() == "llm"
                and q.get("manual_verified") is not True):
            return False
        return True

    @staticmethod
    def _fallback(kp_question: dict, base: dict, reason: str) -> dict:
        """仅当该题本身是已验证选择题时回退；否则返回明确 unavailable。"""
        if QuizGenerator._is_verified(kp_question):
            return {
                **base,
                "question": kp_question["question"],
                "options": dict(kp_question["options"]),
                "answer": kp_question["answer"],
                "analysis": kp_question["analysis"],
                "generated_by": "fallback",
                "fallback_reason": reason,
            }
        verified = fallback_bank.find_fallback(kp_question)
        if verified:
            return {
                **base,
                "question": verified["question"],
                "options": dict(verified["options"]),
                "answer": verified["answer"],
                "analysis": verified["analysis"],
                "generated_by": "fallback",
                "fallback_reason": reason,
                "fallback_quiz_id": verified.get("id"),
                "fallback_source": "verified_bank",
                "excerpt_source": (
                    verified["source_paths"][0]
                    if verified.get("source_paths") else ""
                ),
            }
        return {
            **base,
            "generated_by": "unavailable",
            "available": False,
            "unavailable_reason": f"{reason}；且该知识点无已验证选择题可回退",
        }


def load_verified_fallbacks(path=None) -> list:
    """加载独立回退题库（严格校验；失败时由调用方决定降级策略）。"""
    return fallback_bank.load_fallback_quizzes(path)


def verified_fallback_count(path=None) -> int:
    return fallback_bank.offline_fallback_count(path)


def get_verified_fallback(qid: str, path=None):
    return fallback_bank.get_fallback_quiz(qid, path)
