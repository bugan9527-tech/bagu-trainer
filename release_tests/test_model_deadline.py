"""Local HTTP deadline regressions; fake credentials and bounded drip servers."""
import datetime
import ipaddress
import json
import socket
import socketserver
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from bagu_trainer.infrastructure import model_http
from bagu_trainer.application.settings import verification as onboarding
from bagu_trainer.application.learning import generator as qg
from release_tests.test_quiz_grounding import CANDIDATE, REVIEW, CONTEXT, SOURCE
from release_tests.test_answer_history import history, pump


QUESTION = {"id": "deadline-fixture", "knowledge_point": "缓存有效期",
            "question": "如何选择缓存有效期？", "difficulty": "easy", "source_excerpt": SOURCE}


@pytest.fixture
def model_server(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    running = []

    def start(tls=None):
        state = SimpleNamespace(stop=threading.Event(), requests=0, disconnected=threading.Event(),
            entered=threading.Event(), mode="body", slow_stage="generation", generation_delay=0)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state.requests += 1
                review = payload["messages"][0]["content"] == qg.REVIEW_PROMPT
                mode = state.mode if state.slow_stage == ("review" if review else "generation") else "normal"
                try:
                    if not review and state.generation_delay:
                        state.stop.wait(state.generation_delay)
                    state.entered.set()
                    if mode in ("status", "headers"):
                        prefix = b"HTTP/1.1 200 " if mode == "status" else b"HTTP/1.1 200 OK\r\nX-Drip: "
                        self.connection.sendall(prefix)
                        self.drip(b"x")
                        self.connection.sendall(b"\r\nContent-Length: 0\r\n\r\n")
                        return
                    if mode == "silent_headers":
                        state.stop.wait(1.2)
                    self.send_response(200)
                    if mode.startswith("chunk"):
                        self.send_header("Transfer-Encoding", "chunked")
                    elif mode == "body":
                        self.send_header("Content-Length", "60")
                    elif mode in ("normal", "oversized"):
                        body = b"x" * (qg.MODEL_RESPONSE_MAX_BYTES + 1) if mode == "oversized" else json.dumps({
                            "choices": [{"message": {"content": json.dumps(REVIEW if review else CANDIDATE)}}]
                        }).encode()
                        self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if mode == "body" or mode == "close_body":
                        self.drip(b" ")
                    elif mode == "chunk_body":
                        self.drip(b"1\r\n \r\n")
                        self.wfile.write(b"0\r\n\r\n")
                    elif mode == "chunk_size":
                        self.drip(b"1")
                        self.wfile.write(b"\r\n")
                    elif mode in ("normal", "oversized"):
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionError, ssl.SSLError):
                    state.disconnected.set()

            def drip(self, data):
                for _ in range(60):
                    self.wfile.write(data)
                    self.wfile.flush()
                    if state.stop.wait(.02):
                        break

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        running.append((state, server, thread))
        state.llm = {"base_url": f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}/v1",
                     "api_key": "local-deadline-fixture", "model": "fixture", "timeout_sec": .12}
        monkeypatch.setattr(qg.QuizGenerator, "_current_llm", staticmethod(lambda: state.llm))
        return state

    try:
        yield start
    finally:
        for state, server, thread in running:
            state.stop.set()
            server.shutdown()
            server.server_close()
            thread.join(2)


@pytest.fixture
def drip_model(model_server):
    return model_server()


def test_continuous_body_bytes_cannot_extend_generation_deadline(drip_model):
    started = time.monotonic()
    result = qg.QuizGenerator().generate(QUESTION, CONTEXT)
    elapsed = time.monotonic() - started
    assert result["available"] is False
    assert "Timeout" in result["unavailable_reason"]
    assert elapsed < .45, f"configured 0.12s total budget took {elapsed:.3f}s under continuous bytes"
    assert drip_model.requests == 1


@pytest.mark.parametrize("mode", ["status", "headers", "silent_headers", "close_body", "chunk_body", "chunk_size"])
def test_deadline_also_bounds_header_and_chunk_parser_reads(drip_model, mode):
    drip_model.mode = mode
    started = time.monotonic()
    result = qg.QuizGenerator().generate(QUESTION, CONTEXT)
    elapsed = time.monotonic() - started
    assert result["available"] is False and "Timeout" in result["unavailable_reason"]
    assert elapsed < .45, f"{mode} exceeded total budget: {elapsed:.3f}s"
    assert drip_model.requests == 1


def test_generation_and_review_share_real_network_budget(drip_model):
    drip_model.llm["timeout_sec"] = .4
    drip_model.generation_delay = .28
    drip_model.slow_stage = "review"
    started = time.monotonic()
    result = qg.QuizGenerator().generate(QUESTION, CONTEXT)
    elapsed = time.monotonic() - started
    assert result["available"] is False and "审核" in result["unavailable_reason"]
    assert "Timeout" in result["unavailable_reason"]
    assert elapsed < .58, f"review reset the generation budget: {elapsed:.3f}s"
    assert drip_model.requests == 2


@pytest.mark.parametrize("mode", ["normal", "oversized"])
def test_normal_responses_and_size_limit_still_work(drip_model, mode):
    drip_model.mode = mode
    drip_model.llm["timeout_sec"] = 1
    result = qg.QuizGenerator().generate(QUESTION, CONTEXT)
    if mode == "normal":
        assert result["generated_by"] == "llm" and result["answer"] == CANDIDATE["answer"]
        assert drip_model.requests == 2
    else:
        assert result["available"] is False and "响应过大" in result["unavailable_reason"]
        assert drip_model.requests == 1


def test_connection_test_obeys_total_budget(drip_model):
    started = time.monotonic()
    settings = onboarding.ModelSettings(drip_model.llm["base_url"], "fixture", "local-deadline-fixture", .12)
    with pytest.raises(onboarding.SetupError) as error:
        onboarding.test_connection(settings)
    assert error.value.code == "timeout"
    assert time.monotonic() - started < .45


def test_timeout_closes_response_disconnects_peer_and_allows_retry(drip_model):
    responses = []
    def opener(*args, **kwargs):
        response = model_http.open_model_request(*args, **kwargs)
        responses.append(response)
        return response
    generator = qg.QuizGenerator(urlopen=opener)
    result = generator.generate(QUESTION, CONTEXT)
    assert result["available"] is False
    assert responses and responses[0].closed
    assert drip_model.disconnected.wait(.45), "timed-out response remained connected"
    drip_model.mode = "normal"
    drip_model.llm["timeout_sec"] = 1
    assert generator.generate(QUESTION, CONTEXT)["generated_by"] == "llm"
    assert all(response.closed for response in responses)
    assert drip_model.requests == 3


@pytest.fixture
def tls_model(model_server, tmp_path, monkeypatch):
    # Test-only certificate generation; production uses only stdlib.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2040, 1, 1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    default_context = ssl.create_default_context
    monkeypatch.setattr(ssl, "_create_default_https_context", lambda: default_context(cafile=str(cert_path)))
    return model_server(server_context)


@pytest.mark.parametrize("mode", ["normal", "body", "headers"])
def test_https_certificate_verification_and_deadlines(tls_model, mode):
    tls_model.mode = mode
    tls_model.llm["timeout_sec"] = .25
    started = time.monotonic()
    result = qg.QuizGenerator().generate(QUESTION, CONTEXT)
    if mode == "normal":
        assert result["generated_by"] == "llm" and tls_model.requests == 2
    else:
        assert result["available"] is False and "Timeout" in result["unavailable_reason"]
        assert time.monotonic() - started < .6


def test_https_does_not_trust_unknown_certificate(tls_model, monkeypatch):
    monkeypatch.setattr(ssl, "_create_default_https_context", ssl.create_default_context)
    tls_model.mode = "normal"
    tls_model.llm["timeout_sec"] = 1
    result = qg.QuizGenerator().generate(QUESTION, CONTEXT)
    assert result["available"] is False and tls_model.requests == 0


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), None, "1", True])
def test_transport_rejects_invalid_timeout_without_network(timeout):
    with pytest.raises(ValueError):
        model_http.open_model_request("http://127.0.0.1:1/", timeout=timeout)


def test_address_fallback_connections_share_remaining_budget(model_server, monkeypatch):
    clock, attempts, sockets = [0.0], [], []
    original_socket = socket.socket
    class FailedConnect(original_socket):
        def connect(self, endpoint):
            sockets.append(self)
            attempts.append(self.gettimeout())
            clock[0] += min(.05, self.gettimeout())
            raise TimeoutError("fixture connect timeout")
    monkeypatch.setattr(model_http, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(socket, "socket", FailedConnect)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)) for port in (1, 2, 3)])
    with pytest.raises(qg.urllib.error.URLError):
        model_http.open_model_request("http://fixture.invalid/", timeout=.12)
    assert attempts == pytest.approx([.12, .07, .02])
    assert clock[0] == pytest.approx(.12)
    assert all(sock.fileno() == -1 for sock in sockets)


def test_stalled_tls_handshake_is_bounded(model_server, monkeypatch):
    stop, entered = threading.Event(), threading.Event()
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(1)  # TLS ClientHello, without starting a handshake server.
            entered.set()
            stop.wait(1.2)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    llm = {"base_url": f"https://127.0.0.1:{server.server_address[1]}/v1", "model": "fixture",
           "api_key": "local-deadline-fixture", "timeout_sec": .2}
    monkeypatch.setattr(qg.QuizGenerator, "_current_llm", staticmethod(lambda: llm))
    try:
        started = time.monotonic()
        result = qg.QuizGenerator().generate(QUESTION, CONTEXT)
        assert result["available"] is False and entered.is_set()
        assert time.monotonic() - started < .55
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_slow_system_dns_does_not_hold_request_or_grow_workers(model_server, monkeypatch):
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def blocked_dns(*args, **kwargs):
        calls.append(1)
        entered.set()
        try:
            assert resume.wait(2)
            return []
        finally:
            finished.set()
    monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
    try:
        for _ in range(2):
            started = time.monotonic()
            with pytest.raises(qg.urllib.error.URLError) as error:
                model_http.open_model_request("http://dns-fixture.invalid/", timeout=.12)
            assert isinstance(error.value.reason, TimeoutError)
            assert time.monotonic() - started < .45
        assert entered.is_set() and len(calls) == 1
        workers = [thread for thread in threading.enumerate() if thread.name == "bagu-model-dns"]
        assert len(workers) == 1 and workers[0].daemon
    finally:
        resume.set()
        assert finished.wait(1)


@pytest.fixture
def engine_model(history, drip_model, monkeypatch):
    engine, store, _ = history
    from bagu_trainer.application.monitor import DesktopMonitor
    monkeypatch.setattr(engine, "generator", qg.QuizGenerator())
    monkeypatch.setattr(engine, "kb", SimpleNamespace(get=lambda _: QUESTION, all=lambda: [QUESTION]))
    monkeypatch.setattr(engine, "cfg", {"max_quizzes_per_hit": 1})
    monkeypatch.setattr(engine, "matcher", SimpleNamespace(rank=lambda *args, **kwargs: [{
        "question": QUESTION, "score": 5, "hits": ["缓存"], "confidence": 1.0, "margin": 1.0}]))
    monitor = DesktopMonitor(engine)
    root = engine._normalize_project_root(engine.load_config()["project_roots"][0])
    return engine, store, monitor, root, {"context": CONTEXT, "sources": ["fixture"]}


def test_real_engine_lock_releases_and_explicit_retry_succeeds(engine_model, drip_model):
    engine, store, monitor, root, info = engine_model
    started = time.monotonic()
    assert monitor._generate(root, info) == "generation_unavailable"
    assert time.monotonic() - started < .45
    assert root in monitor.failed_generations
    assert store._conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 0
    with ThreadPoolExecutor(max_workers=1) as workers:
        assert workers.submit(engine.get_learning_stats).result(timeout=.5)["ok"]
    drip_model.mode = "normal"
    drip_model.llm["timeout_sec"] = 1
    assert monitor.retry_generation()["state"] == "quiz_ready"
    assert not monitor.failed_generations
    quiz = monitor.take_quiz()
    assert quiz and engine.submit_answer(quiz["quiz_id"], "A")["ok"]
    assert store.attempt_count(quiz["quiz_id"]) == 1
    assert drip_model.requests == 3  # One timed-out generation, then generation + review.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk desktop")
def test_real_desktop_exit_waits_only_until_network_deadline(engine_model, drip_model, monkeypatch):
    from tkinter import messagebox
    from bagu_trainer.desktop import DesktopApp
    engine, _, monitor, root, info = engine_model
    drip_model.llm["timeout_sec"] = .3
    monitor.failed_generations[root] = (info, (QUESTION["id"],))
    monkeypatch.setattr(monitor, "poll", lambda: {"state": "watching", "projects": 1})
    monkeypatch.setattr(messagebox, "askyesno", lambda *_a, **_k: True)
    app = DesktopApp(monitor)
    app.root.withdraw()
    try:
        app.generation_retryable = True
        assert app._request_action("retry_generation")
        app._start_worker()
        assert drip_model.entered.wait(2)
        started = time.monotonic()
        app._close()
        assert app.closing
        pump(app, lambda: app.closed, timeout=2)
        assert time.monotonic() - started < .7
        app.worker_thread.join(.2)
        assert app.worker_done.is_set() and not app.worker_thread.is_alive()
        assert drip_model.requests == 1
        assert engine.get_learning_stats()["ok"]
    finally:
        app.stop.set()
        if app.worker_thread:
            app.worker_thread.join(2)
        if not app.closed:
            app.root.destroy()
