"""Real localhost transports; fixture credentials only, never provider APIs."""
import json
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from bagu_trainer import onboarding, quiz_generator as qg
from release_tests.test_quiz_grounding import CANDIDATE, CONTEXT, REVIEW, SOURCE


KEY = "local-redirect-fixture-key"
QUESTION = {"id": "redirect-fixture", "knowledge_point": "缓存有效期",
            "question": "如何选择缓存有效期？", "difficulty": "easy", "source_excerpt": SOURCE}


@pytest.fixture
def model_http(monkeypatch):
    # Ignore machine proxies so both origins always stay on loopback.
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    state = SimpleNamespace(status=302, fail_at=1, origin=[], target=[], location=None)

    def record(handler, calls):
        body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
        calls.append({"method": handler.command, "path": handler.path,
                      "authorized": handler.headers.get("Authorization") == "Bearer " + KEY,
                      "has_authorization": "Authorization" in handler.headers,
                      "payload": json.loads(body) if body else None})

    def reply(handler, status, document, location=None):
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        handler.send_response(status, "fixture " + KEY)
        if location:
            handler.send_header("Location", location)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def envelope(content):
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            record(self, state.target)
            reply(self, 200, envelope(CANDIDATE))

        do_POST = do_GET

        def log_message(self, *_):
            pass

    class Origin(BaseHTTPRequestHandler):
        def do_POST(self):
            record(self, state.origin)
            number = len(state.origin)
            if number == state.fail_at:
                reply(self, state.status, {"error": KEY}, state.location or state.target_url)
            else:
                reply(self, 200, envelope(CANDIDATE if number == 1 else REVIEW))

        do_GET = do_POST

        def log_message(self, *_):
            pass

    with ExitStack() as stack:
        for handler, name in ((Target, "target_url"), (Origin, "base_url")):
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=lambda s=server: s.serve_forever(poll_interval=0.01), daemon=True)
            thread.start()
            stack.callback(thread.join, 2)
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            setattr(state, name, f"http://127.0.0.1:{server.server_port}/v1")
        llm = {"base_url": state.base_url, "api_key": KEY, "model": "local-fixture", "timeout_sec": 3}
        monkeypatch.setattr(qg.QuizGenerator, "_current_llm", staticmethod(lambda: llm))
        state.settings = onboarding.ModelSettings(state.base_url, llm["model"], KEY, 3)
        yield state


def invoke(state, entry):
    if entry == "connection":
        try:
            onboarding.test_connection(state.settings)
        except onboarding.SetupError as error:
            return str(error)
        return ""
    if entry == "legacy_connection":
        return qg.test_llm(state.base_url, KEY, "local-fixture", timeout=3)
    result = qg.QuizGenerator().generate(QUESTION, CONTEXT)
    assert not any(call["has_authorization"] for call in state.target), "redirect target received Authorization"
    assert result.get("available") is False
    assert not {"question", "options", "answer", "grounding"} & result.keys()
    return result["unavailable_reason"]


@pytest.mark.parametrize("entry", ["generation", "review", "connection", "legacy_connection"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", ["cross_origin", "same_origin_relative", "cross_origin_scheme_relative"])
def test_redirects_never_forward_authorization(model_http, entry, status, destination, capsys):
    model_http.status = status
    model_http.fail_at = 2 if entry == "review" else 1
    if destination == "same_origin_relative":
        model_http.location = "/redirected?token=" + KEY
    elif destination == "cross_origin_scheme_relative":
        model_http.location = model_http.target_url.removeprefix("http:") + "?token=" + KEY
    else:
        model_http.location = model_http.target_url + "?token=" + KEY
    message = invoke(model_http, entry)
    assert not any(call["has_authorization"] for call in model_http.target), "redirect target received Authorization"
    assert model_http.target == [], "redirect target must not be contacted"
    assert len(model_http.origin) == model_http.fail_at
    assert all(call["authorized"] and call["method"] == "POST" for call in model_http.origin)
    assert f"HTTP {status}" in message
    assert "最终基础地址" in message
    if entry == "review":
        assert "审核" in message
    assert KEY not in message + "".join(capsys.readouterr())
    assert model_http.target_url not in message


@pytest.mark.parametrize("entry", ["generation", "review", "connection", "legacy_connection"])
@pytest.mark.parametrize("status,hint", [(401, "Key"), (403, "权限"), (404, "地址"),
                                         (429, "额度"), (500, "重试"), (418, "重试")])
def test_http_errors_keep_status_and_safe_actionable_message(model_http, entry, status, hint, capsys):
    model_http.status = status
    model_http.fail_at = 2 if entry == "review" else 1
    message = invoke(model_http, entry)
    assert f"HTTP {status}" in message and hint in message
    assert KEY not in message + "".join(capsys.readouterr())
    assert len(model_http.origin) == model_http.fail_at
    assert model_http.target == []


def test_normal_generation_and_review_use_authenticated_post(model_http):
    model_http.fail_at = 0
    result = qg.QuizGenerator().generate(QUESTION, CONTEXT)
    assert result["generated_by"] == "llm"
    assert result["answer"] == CANDIDATE["answer"]
    assert result["grounding"]["review"]["context_relevant"] is True
    assert len(model_http.origin) == 2 and model_http.target == []
    for call in model_http.origin:
        assert call["authorized"] and call["method"] == "POST"
        assert call["path"] == "/v1/chat/completions"
    review = model_http.origin[1]["payload"]
    assert review["temperature"] == 0
    assert json.loads(review["messages"][-1]["content"])["task"] == "review_context_grounding"


@pytest.mark.parametrize("entry", ["connection", "legacy_connection"])
def test_normal_connection_test_still_succeeds(model_http, entry):
    model_http.fail_at = 0
    if entry == "connection":
        assert onboarding.test_connection(model_http.settings).settings == model_http.settings
    else:
        assert "连接成功" in qg.test_llm(model_http.base_url, KEY, "local-fixture", timeout=3)
    assert len(model_http.origin) == 1 and model_http.target == []
    assert model_http.origin[0]["authorized"]
