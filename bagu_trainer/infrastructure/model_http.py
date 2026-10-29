"""HTTP policy shared by authenticated model requests."""
import http.client
import ipaddress
import json
import math
import queue
import socket
import ssl
import threading
import time
import urllib.request
from concurrent.futures import Future, TimeoutError as FutureTimeout


_DNS_QUEUE = queue.Queue(maxsize=1)
_DNS_LOCK = threading.Lock()
_dns_thread = None


def _resolve(host, port, remaining):
    """Bound system DNS waits without leaving HTTP requests running in threads.

    OS name resolution cannot be interrupted. A single lazy daemon and one
    queued lookup bound its resources and never keep normal app exit waiting.
    No credentials, requests or sockets are handed to this worker.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST)

    global _dns_thread
    with _DNS_LOCK:
        if _dns_thread is None:
            _dns_thread = threading.Thread(target=_resolve_worker, name="bagu-model-dns", daemon=True)
            _dns_thread.start()
    future = Future()
    try:
        _DNS_QUEUE.put((future, host, port, time.monotonic() + remaining()), timeout=remaining())
        return future.result(timeout=remaining())
    except (queue.Full, FutureTimeout, TimeoutError):
        future.cancel()
        raise TimeoutError("model DNS deadline") from None


def _resolve_worker():
    while True:
        future, host, port, deadline = _DNS_QUEUE.get()
        try:
            if future.set_running_or_notify_cancel():
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("model DNS deadline")
                    value = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
                except Exception as error:
                    future.set_exception(error)
                else:
                    future.set_result(value)
        finally:
            _DNS_QUEUE.task_done()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # Never replay credentials or model context to a redirect destination,
        # even on the same origin. The user must configure the final API URL.
        return None


def open_model_request(request, *, timeout):
    """Use one monotonic budget for connect, TLS, headers and response reads.

    urllib's socket timeout alone is an inactivity limit. Rearm it with the
    remaining total budget before each underlying I/O, including reads hidden
    inside HTTP header/chunk parsing. No detached request thread is needed.
    The caller must close the returned response (normally with a with block).
    """
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("model timeout must be a finite positive number")
    deadline = time.monotonic() + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("model request deadline")
        return value

    class DeadlineIO:
        def recv_into(self, *args, **kwargs):
            self.settimeout(remaining())
            return super().recv_into(*args, **kwargs)

        def send(self, *args, **kwargs):
            self.settimeout(remaining())
            return super().send(*args, **kwargs)

        def sendall(self, *args, **kwargs):
            self.settimeout(remaining())
            return super().sendall(*args, **kwargs)

        def do_handshake(self, *args, **kwargs):
            self.settimeout(remaining())
            return super().do_handshake(*args, **kwargs)

    class DeadlineSocket(DeadlineIO, socket.socket):
        pass

    class DeadlineSSLSocket(DeadlineIO, ssl.SSLSocket):
        pass

    def create_connection(address, timeout=None, source_address=None):
        host, port = address
        error = None
        # Recompute for each address, so fallback connects share the budget.
        for family, kind, protocol, _, endpoint in _resolve(host, port, remaining):
            remaining()
            sock = DeadlineSocket(family, kind, protocol)
            try:
                sock.settimeout(remaining())
                if source_address:
                    sock.bind(source_address)
                sock.connect(endpoint)
                return sock
            except OSError as failure:
                error = failure
                sock.close()
        if error is not None:
            raise error
        raise OSError("model host resolved to no addresses")

    def http_connection(host, **kwargs):
        connection = http.client.HTTPConnection(host, **kwargs)
        connection._create_connection = create_connection
        return connection

    def https_connection(host, **kwargs):
        connection = http.client.HTTPSConnection(host, **kwargs)
        connection._create_connection = create_connection
        # HTTPSConnection creates a private, certificate-verifying context.
        # Its SocketIO must rearm the same deadline after the TLS handshake.
        connection._context.sslsocket_class = DeadlineSSLSocket
        return connection

    class DeadlineHTTP(urllib.request.HTTPHandler):
        def http_open(self, request):
            return self.do_open(http_connection, request)

    class DeadlineHTTPS(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return self.do_open(https_connection, request,
                                context=self._context, check_hostname=self._check_hostname)

    # A private opener also prevents a global opener weakening redirect policy.
    return urllib.request.build_opener(_NoRedirect(), DeadlineHTTP(), DeadlineHTTPS()).open(
        request, timeout=remaining())


def http_error_message(code):
    """Keep status/action, never provider reason, URL, headers or response body."""
    if 300 <= code < 400:
        message = "接口返回重定向，已阻止自动跳转；请填写最终基础地址后重试。"
    else:
        message = {
            401: "Key 无效或未授权，请核对 Key 后重试。",
            403: "接口拒绝访问，请检查模型权限。",
            404: "接口或模型不存在，请核对基础地址和模型名称。",
            429: "请求受限，请检查额度或稍后重试。",
        }.get(code, "接口暂不可用，请稍后重试。")
    return f"HTTP {code}：{message}"


class ChatModelClient:
    """Bounded chat HTTP transport; quiz content rules belong to domain.quizzes."""
    def __init__(self, *, opener=None, clock=time.monotonic, response_max_bytes=256 * 1024):
        self._opener = opener or open_model_request
        self._clock = clock
        self._response_max_bytes = response_max_bytes

    def request(self, llm, system, user, temperature, deadline):
        from bagu_trainer.infrastructure import secrets
        from bagu_trainer.domain.quizzes import QuizValidationError

        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("model request deadline")
        payload = {
            "model": llm.get("model", "gpt-4o-mini"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
        }
        request = urllib.request.Request(
            llm["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {llm['api_key']}"})
        with self._opener(request, timeout=remaining) as response:
            raw = response.read(self._response_max_bytes + 1)
        if self._clock() >= deadline:
            raise TimeoutError("model request deadline")
        if len(raw) > self._response_max_bytes:
            raise QuizValidationError("AI 响应过大，请重试。", failure_kind="service")
        content = json.loads(raw.decode("utf-8"))["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise QuizValidationError("AI 响应内容格式不正确，请重试。", failure_kind="service")
        return secrets.redact_text(content, [llm["api_key"]])
