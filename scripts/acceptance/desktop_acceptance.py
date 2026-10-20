"""Standalone acceptance driver; never included in the production portable ZIP.

Can be frozen separately so a clean Windows guest needs no installed Python/Git.
Uses real Tk, engine, watcher, scorer and SQLite; only HTTP source/model fixtures
and an optional CI credential store are injected through existing internal APIs.
"""
import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

KEY = "acceptance-local-only-not-a-secret"
REPOSITORY = "Snailclimb/JavaGuide"
TEXT = ("# 什么是数据库索引？\n\n数据库索引用额外的数据结构记录键值与数据位置的对应关系。"
        "查询可以利用索引缩小扫描范围。维护索引需要存储空间，插入和更新数据也可能增加"
        "索引维护成本，因此应结合查询条件选择索引。\n")
# Multiple unrelated concepts give the unchanged BM25 evidence gate a realistic
# corpus. A one-question fixture has near-zero IDF and should not force a match.
DISTRACTORS = [
    ("栈结构如何保持后进先出顺序？", "栈只允许在同一端插入和删除元素。新元素压入栈顶，弹出操作取出栈顶元素，因此最后加入的元素最先离开。调用栈可以利用这个顺序保存返回位置。"),
    ("队列如何保证先进先出顺序？", "队列从尾部加入元素，并从头部取出元素。入队和出队分别作用于不同端，能够保留元素到达的先后顺序。循环队列可以使用两个位置表示读端和写端。"),
    ("哈希表如何解决哈希碰撞？", "不同键可能映射到同一位置，这种情况称为哈希碰撞。链地址法在每个位置保存一组元素，开放寻址则寻找其他可用位置。装载因子会影响碰撞频率。"),
    ("二分查找为什么要求有序数组？", "二分查找通过比较中间元素排除一半区间。有序性保证目标只可能处于其中一边。每轮缩小区间直到发现目标或区间为空，边界条件决定是否包含中间位置。"),
    ("互斥锁如何保护临界区？", "互斥锁确保同一时刻只有一个执行线程进入受保护区域。线程进入前获取锁，离开时释放锁，其他竞争者等待可用。所有相关访问必须遵守同一个锁协议。"),
    ("TCP 三次握手为什么需要三个报文？", "连接建立时双方交换同步报文并确认对方序列号。客户端发送同步请求，服务端回复同步和确认，客户端再确认服务端报文，以便双方建立连接状态。"),
    ("对象引用计数如何处理循环引用？", "引用计数记录对象被引用的数量，计数归零时对象可以释放。循环引用中的对象可能相互维持计数，需要额外的循环检测或其他垃圾回收策略协助处理。"),
    ("递归算法为什么需要终止条件？", "递归把一个问题转换为规模更小的同类问题。终止条件处理最简单的情况，阻止调用无限继续。每次调用必须向终止条件前进，并正确合并较小问题的结果。"),
    ("异常处理如何保证资源释放？", "程序在遇到错误时可能提前离开正常控制流。将释放逻辑放入清理块或上下文管理器，可以让正常返回和异常离开都执行关闭动作，避免资源一直被占用。"),
]
CHANGE = ('# 数据库索引 查询优化 扫描范围 索引维护\n'
          'query = "SELECT id FROM orders WHERE customer_id = 100"\n'
          'index = "CREATE INDEX customer_lookup ON orders(customer_id)"\n')
QUESTION = {"question": "数据库查询需要减少扫描的数据量，应当如何结合查询条件优化索引？",
            "options": {"A": "结合查询条件建立合适索引", "B": "删除所有索引", "C": "总是全表扫描", "D": "只改变量名"},
            "answer": "A", "analysis": "本地模拟解析：索引缩小扫描范围，同时增加存储与维护成本。"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def check(value, message):
    if not value:
        raise AssertionError(message)


def database_state(path):
    connection = sqlite3.connect(path)
    try:
        return {"attempts": connection.execute("SELECT count(*) FROM attempts").fetchone()[0],
                "answered": connection.execute("SELECT count(*) FROM quizzes WHERE status='answered'").fetchone()[0],
                "concepts": [list(row) for row in connection.execute("SELECT kp_id,attempts,mastery,next_review_at FROM concept_state ORDER BY kp_id")]}
    finally:
        connection.close()


def phase(options):
    # Imports happen only after the orchestrator has selected an isolated profile.
    from bagu_trainer import config, desktop, first_use, secrets, startup
    from bagu_trainer.bank_build.service import BankBuildService
    from bagu_trainer.bank_storage import active_bank_dir
    if options.memory_credentials:
        class Credentials:
            def get_password(self, *_):
                return KEY
            def set_password(self, *_):
                pass
        secrets._load_keyring = lambda: Credentials()
    work = options.work
    profile = config.USER_DATA_ROOT
    watched = work / "监听 项目"
    errors, milestones = [], []
    old_wizard = first_use.FirstUseWizard.__init__
    old_dashboard = desktop.DesktopApp.__init__
    bank_service = BankBuildService(manifest=work / "fixture-sources.json",
        opener=lambda request, timeout: urllib.request.urlopen(options.base_url + "/bank.zip", timeout=timeout))
    def schedule(root, callback, cleanup):
        deadline = time.monotonic() + 65
        def tick():
            try:
                check(time.monotonic() < deadline, "UI phase timed out; milestones=" + repr(milestones))
                if callback():
                    return
            except Exception as error:
                errors.append(type(error).__name__ + ": " + str(error))
                write_json(work / "failure.json", {"errors": errors, "milestones": milestones})
                cleanup()
                return
            root.after(40, tick)
        root.after(80, tick)
    def wizard_init(app, coordinator, **kwargs):
        check(options.phase == "first", "Restart unexpectedly opened first-use wizard")
        old_wizard(app, coordinator, bank_service=bank_service)
        stage = [0]
        def step():
            if app.busy or app._build_busy():
                return False
            if stage[0] == 0:
                check(app.step == "bank", "Expected bank step")
                check("bagu_trainer.engine" not in sys.modules, "Engine loaded before readiness")
                app.bank_setup._start()
                check("选择" in app.bank_setup.status.get(), "Missing selection guidance")
                app.bank_setup.source_list.selection_set(REPOSITORY)
                app.bank_setup._start()
            elif stage[0] == 1:
                check("503" in app.bank_setup.status.get(), "Missing download failure guidance")
                check(not (config.data_dir() / "active-bank.json").exists(), "Failed build became active")
                milestones.append("download_failure_and_automatic_retries")
                app.bank_setup._start()
            elif stage[0] == 2:
                check(app.step == "model", "Build did not advance wizard")
                check((active_bank_dir() / "question_concepts.json").is_file(), "Concept build missing")
                milestones.append("bank_download_extract_import_tag_concepts")
                app.base_url.set("bad-url")
                app.model_name.set("local-model")
                app.api_key.set(KEY)
                app._test_model()
            elif stage[0] == 3:
                check(app.verified is None and "地址" in app.status.get(), "Invalid URL guidance missing")
                app.base_url.set(options.base_url + "/v1")
                app.model_name.set("missing-model")
                app._test_model()
            elif stage[0] == 4:
                check(app.verified is None and "404" in app.status.get(), "Invalid model guidance missing")
                milestones.append("invalid_config_and_model_404_recovery")
                app.model_name.set("local-model")
                app._test_model()
            elif stage[0] == 5:
                check(app.verified is not None, "Valid local model did not pass test")
                app._next()
            elif stage[0] == 6:
                check(app.step == "projects" and not app.api_key.get(), "Model was not saved securely")
                check(KEY not in config.CONFIG_PATH.read_text(encoding="utf-8"), "Plaintext key in configuration")
                milestones.append("model_explicit_test_and_secure_save")
                app.projects = [str(watched)]
                app._render_projects()
                app._next()
            elif stage[0] == 7:
                check(app.step == "finish", "Projects did not advance wizard")
                milestones.append("project_selection")
                app._next()
                return True
            stage[0] += 1
            return False
        schedule(app.root, step, lambda: app.root.destroy())
    def dashboard_init(app, monitor, *args, **kwargs):
        old_dashboard(app, monitor, *args, **kwargs)
        stage = [0]
        first_quiz = [None]
        def cleanup():
            app.stop.set()
            if app.popup is not None:
                app.popup.destroy()
            app.root.destroy()
        def step():
            engine = monitor.engine
            if options.phase == "restart":
                if not monitor.initialized:
                    return False
                check(app.popup is None, "Restart automatically repeated answered quiz")
                state = database_state(config.db_path())
                expected = json.loads((work / "first-state.json").read_text(encoding="utf-8"))
                check(state == expected, "Restart/upgrade lost learning data")
                check(all(Path(path).exists() for path in config.load_config()["project_roots"]), "Projects lost")
                milestones.append("restart_direct_main_and_history_restore")
                cleanup()
                return True
            if stage[0] == 0:
                if not monitor.initialized:
                    return False
                milestones.append("watcher_baseline")
                (watched / "query.py").write_text(CHANGE, encoding="utf-8")
                stage[0] = 1
            elif stage[0] == 1:
                if app.popup is None:
                    return False
                check(app.submit_button.instate(["!disabled"]), "Popup cannot be answered")
                first_quiz[0] = monitor.active
                app.answer.set("B")
                app._submit(first_quiz[0])
                milestones.append("real_file_change_debounce_model_popup")
                stage[0] = 2
            elif stage[0] == 2:
                if "已记录作答" not in app.feedback.get():
                    return False
                check("回答错误" in app.feedback.get() and "正确答案：A" in app.feedback.get(), "Wrong grade/analysis")
                app._submit(first_quiz[0])  # Disabled button prevents duplicate scoring.
                state = database_state(config.db_path())
                check(state["attempts"] == 1 and state["answered"] == 1, "Answer not persisted exactly once")
                check(state["concepts"][0][1] == 1 and state["concepts"][0][2] == 0, "Concept state missing")
                write_json(work / "first-state.json", state)
                milestones.append("grade_analysis_and_sqlite_persistence")
                cleanup()
                return True
            return False
        schedule(app.root, step, cleanup)
    first_use.FirstUseWizard.__init__ = wizard_init
    desktop.DesktopApp.__init__ = dashboard_init
    try:
        desktop.main([])
    finally:
        if "bagu_trainer.engine" in sys.modules:
            engine = sys.modules["bagu_trainer.engine"]
            if engine._store:
                engine._store.close()
    check(not errors, "; ".join(errors))
    check(milestones, "No acceptance milestones completed")
    write_json(work / (options.phase + "-result.json"), {"passed": True, "milestones": milestones,
               "frozen": bool(getattr(sys, "frozen", False)), "credential_backend": "memory fixture" if options.memory_credentials else "Windows vault, isolated profile"})


def orchestrate(options):
    work = options.work
    work.mkdir(parents=True, exist_ok=False)
    watched = work / "监听 项目"
    watched.mkdir()
    (watched / "query.py").write_text('query = "SELECT id FROM orders"\n', encoding="utf-8")
    write_json(work / "fixture-sources.json", {"sources": [{"repository": REPOSITORY, "directory": "JavaGuide",
        "audited_commit": "a" * 40, "observed_license": "synthetic self-authored test fixture"}]})
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("snapshot/README.md", TEXT + "\n" + "\n\n".join("# " + title + "\n\n" + body for title, body in DISTRACTORS))
    counts = {"downloads": 0, "probes": 0, "quizzes": 0, "rejected": 0}
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, value):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path != "/bank.zip":
                self.send(404, {})
                return
            counts["downloads"] += 1
            self.send(503, {}) if counts["downloads"] <= 3 else self.send(200, archive.getvalue())
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/chat/completions" or request.get("model") != "local-model":
                counts["rejected"] += 1
                self.send(404, {})
                return
            if self.headers.get("Authorization") != "Bearer " + KEY:
                self.send(401, {})
                return
            is_probe = "max_tokens" in request
            counts["probes" if is_probe else "quizzes"] += 1
            self.send(200, {"choices": [{"message": {"content": "OK" if is_probe else json.dumps(QUESTION, ensure_ascii=False)}}]})
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    environment = dict(os.environ, BAGU_USER_DATA_DIR=str(work / "用户 数据"), PATH="",
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_PROXY="127.0.0.1,localhost")
    for variable in ("BAGU_LLM_API_KEY", "PYTHON_KEYRING_BACKEND", "PYTHONHOME", "PYTHONPATH"):
        environment.pop(variable, None)
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-B", str(Path(__file__).resolve())]
    results = {"date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "os": platform.platform(),
               "architecture": platform.machine(), "frozen": bool(getattr(sys, "frozen", False)),
               "path_empty_in_app": True, "mcp_configured": False,
               "clean_machine_claim": False, "mock_network": "localhost HTTP, self-authored ZIP and fixed model responses"}
    if getattr(sys, "frozen", False):
        results.update(json.loads((Path(sys._MEIPASS) / "acceptance-provenance.json").read_text(encoding="utf-8")))
    try:
        for name in ("first", "restart"):
            args = [*command, "--work", str(work), "--phase", name, "--base-url", base]
            if options.memory_credentials:
                args.append("--memory-credentials")
            result = subprocess.run(args, env=environment, capture_output=True, timeout=80)
            check(result.returncode == 0, f"{name} child failed: " + result.stderr.decode("utf-8", errors="replace")[-3000:])
            results[name] = json.loads((work / (name + "-result.json")).read_text(encoding="utf-8"))
            if name == "first":
                bank_hashes = {str(p.relative_to(work)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (work / "用户 数据" / "data" / "banks").rglob("*") if p.is_file()}
        check(counts == {"downloads": 4, "probes": 1, "quizzes": 1, "rejected": 1}, "Unexpected network requests or restart requested model")
        check(all(hashlib.sha256((work / p).read_bytes()).hexdigest() == digest for p, digest in bank_hashes.items()), "Bank changed on restart")
        check(KEY not in (work / "用户 数据" / "config.json").read_text(encoding="utf-8"), "Plaintext fixture key in config")
        if options.production_exe:
            from bagu_trainer import desktop_instance
            desktop_instance.RUNTIME = work / "用户 数据" / "runtime"
            production_manifest = json.loads((options.production_exe.parent / "BUILD_MANIFEST.json").read_text(encoding="utf-8"))
            results["production_commit"] = production_manifest.get("source_commit")
            results["production_dirty"] = production_manifest.get("source_dirty")
            prod_env = dict(environment)
            if options.memory_credentials:
                # Matches the verified receipt without touching a CI runner's vault.
                prod_env["BAGU_LLM_API_KEY"] = KEY
            upgraded = work / "升级 后程序"
            shutil.copytree(options.production_exe.parent, upgraded)
            def smoke(exe):
                child = subprocess.Popen([str(exe)], env=prod_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        check(child.poll() is None, "Production EXE unexpectedly exited")
                        try:
                            status = desktop_instance.remote_call("_desktop_status", {})
                            if status.get("startup_state") == "running":
                                return
                        except (OSError, ValueError):
                            pass
                        time.sleep(0.1)
                    raise AssertionError("Production EXE did not enter main window")
                finally:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=10)
            smoke(options.production_exe)
            check(database_state(work / "用户 数据" / "data" / "trainer.db") == json.loads((work / "first-state.json").read_text(encoding="utf-8")), "Production EXE lost learning data")
            smoke(upgraded / options.production_exe.name)
            check(database_state(work / "用户 数据" / "data" / "trainer.db") == json.loads((work / "first-state.json").read_text(encoding="utf-8")), "Upgrade lost learning data")
            results["production_exe"] = {"first_start": "running", "upgraded_start": "running", "history_retained": True}
        results["requests"] = counts
        results["passed"] = True
        write_json(work / "ACCEPTANCE.json", results)
        print(json.dumps(results, ensure_ascii=True))
    finally:
        server.shutdown()
        server.server_close()
        if not options.memory_credentials:
            # Delete only this driver's isolated credential, never the default service.
            from bagu_trainer import secrets
            profile = os.path.normcase(str((work / "用户 数据").resolve()))
            service = "bagu-trainer.profile." + hashlib.sha256(profile.encode("utf-8")).hexdigest()[:24]
            keyring = secrets._load_keyring()
            if keyring is not None:
                try:
                    keyring.delete_password(service, secrets.KEYRING_USER)
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=lambda p: Path(p).resolve(), required=True)
    parser.add_argument("--phase", choices=["first", "restart"])
    parser.add_argument("--base-url")
    parser.add_argument("--memory-credentials", action="store_true")
    parser.add_argument("--production-exe", type=lambda p: Path(p).resolve())
    options = parser.parse_args()
    if options.phase:
        phase(options)
    else:
        orchestrate(options)


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    try:
        main()
    except Exception as error:
        # A windowed acceptance EXE must fail with an exit code/report instead of
        # showing a modal crash dialog that would block an unattended run.
        if "--work" in sys.argv:
            location = Path(sys.argv[sys.argv.index("--work") + 1])
            write_json(location / "ERROR.json", {"error": type(error).__name__, "message": str(error)})
        if sys.stderr is not None:
            import traceback
            traceback.print_exc()
        raise SystemExit(1)
