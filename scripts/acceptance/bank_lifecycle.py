"""Continuous, two-process bank lifecycle using generated ZIPs and isolated data.

Imported by the separate frozen acceptance driver, never by the production app.
The injected transport cannot reach the network. Resume refuses foreign data.
"""
import io
import json
import os
from pathlib import Path
import sys
import urllib.error
import zipfile
from dataclasses import asdict

REPOS = ["fixture/first", "fixture/middle", "fixture/last"]
COMMITS = [letter * 40 for letter in "abc"]
SNAPSHOT = {"question": "中文历史题干\n如何减少索引扫描？", "kp_name": "索引",
            "options": {letter: f"中文选项 {letter}\n多行说明" for letter in "ABCD"},
            "answer": "A", "analysis": "索引缩小扫描范围。\n需要额外存储与维护成本。", "generated_by": "llm",
            "sources": [{"repository": REPOS[0], "commit": COMMITS[0]}]}


def check(value, message):
    if not value:
        raise AssertionError(message)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run(work, phase):
    from bagu_trainer import config
    from bagu_trainer.bank_build.service import BankBuildService, load_sources
    from bagu_trainer.bank_build.control import SourceBatchError
    from bagu_trainer.bank_storage import active_bank_dir
    from bagu_trainer.store import Store
    from bagu_trainer.scheduler import next_state

    work = Path(work).resolve()
    user = work / "user"
    check(config.USER_DATA_ROOT.resolve() == user, "Refusing a non-fixture user directory")
    marker = work / "fixture.json"
    if phase == "prepare":
        work.mkdir(parents=True, exist_ok=False)
        write(marker, {"purpose": "bank-lifecycle-synthetic-only", "user": str(user)})
    else:
        check(json.loads(marker.read_text(encoding="utf-8")) ==
              {"purpose": "bank-lifecycle-synthetic-only", "user": str(user)}, "Fixture ownership mismatch")
    manifest = work / "sources.json"
    calls, failures = [], set()

    def opener(request, timeout):
        repo = "/".join(request.full_url.split("/")[4:6])
        check(repo in REPOS and request.full_url.endswith(COMMITS[REPOS.index(repo)]), "Unexpected source")
        calls.append(repo)
        if repo in failures:
            raise urllib.error.HTTPError(request.full_url, 404, "synthetic missing source", {}, None)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("snapshot/README.md", f"# 数据库索引的第 {REPOS.index(repo)} 种用途？\n\n"
                "数据库索引用额外的数据结构记录键值与数据位置的对应关系。查询利用索引缩小扫描范围。"
                "维护索引需要存储空间，插入和更新也增加维护成本，因此应该结合查询条件选择索引。\n")
        response = io.BytesIO(stream.getvalue())
        response.headers = {"Content-Length": str(len(response.getvalue()))}
        return response

    service = BankBuildService(user, manifest=manifest, opener=opener)
    milestones = []
    if phase == "prepare":
        write(manifest, {"sources": [{"repository": repo, "directory": f"Repo{i}", "audited_commit": COMMITS[i]}
                                    for i, repo in enumerate(REPOS)]})
        watched = work / "项目 文件"
        watched.mkdir()
        (watched / "keep.py").write_text("# 不得删除项目文件\n", encoding="utf-8")
        write(user / "config.json", {"project_roots": [str(watched)], "db_path": "data/trainer.db",
            "llm": {"base_url": "https://example.invalid/v1", "model": "fixture", "api_key": ""}})
        first = service.build(REPOS[:1])
        check(first.questions == 1 and calls == REPOS[:1], "First download failed")
        milestones.append("first_download")
        all_sources = service.build(REPOS)
        check(all_sources.questions == 3 and calls == REPOS, "All-select redownloaded the first source")
        milestones.append("all_selected_only_missing_downloaded")
        store = Store(str(user / "data" / "trainer.db"))
        try:
            store.create_quiz("legacy-answer", "old-kp", SNAPSHOT, "answered", str(watched), "原始快照",
                              1800000000, 1800003600, answered_at=1800000010)
            store.record_attempt("legacy-answer", "old-kp", "B", "B", False, 1800000010,
                                 next_state(False, None, 1800000010))
            write(work / "learning.json", list(store._conn.iterdump()))
        finally:
            store.close()
        pointer = (user / "data" / "active-bank.json").read_bytes()
        configuration = (user / "config.json").read_bytes()
        calls.clear()
        failures.add(REPOS[1])
        try:
            service.build(REPOS, force_download=True)
        except SourceBatchError as error:
            failed = [source.repository for source in error.failures]
            check(failed == [REPOS[1]] and calls == REPOS, "Batch did not continue after failure")
        else:
            raise AssertionError("Synthetic failure unexpectedly succeeded")
        check((user / "data" / "active-bank.json").read_bytes() == pointer, "Partial batch activated")
        milestones.append("partial_failure_keeps_current")
        failures.clear()
        calls.clear()
        service.build(REPOS, retry_failed=failed, force_download=True)
        check(calls == failed, "Retry downloaded successful sources")
        milestones.append("retry_failed_only")
        calls.clear()
        service.build(REPOS)
        check(not calls, "Same commit rebuild made a network request")
        milestones.append("same_version_rebuild_offline")
        check((user / "config.json").read_bytes() == configuration, "Build changed configuration")
        write(work / "configuration.json", json.loads(configuration))
        write(work / "active.json", {"path": str(active_bank_dir(user)),
                                     "questions": json.loads((active_bank_dir(user) / "questions.json").read_text(encoding="utf-8"))})
    else:
        previous = json.loads((work / "active.json").read_text(encoding="utf-8"))
        check(str(active_bank_dir(user)) == previous["path"], "Restart changed active pointer")
        check(json.loads((active_bank_dir(user) / "questions.json").read_text(encoding="utf-8")) == previous["questions"],
              "Previous UUID bank cannot be read")
        from bagu_trainer import engine
        check(len(engine.kb.all()) == 3, "Engine did not load the previous bank")
        service.build(REPOS)
        engine.reload_bank()
        check(not calls, "Process restart lost verified cache")
        milestones.append("process_restart_reuses_verified_cache")
        engine._store = Store(str(user / "data" / "trainer.db"))
        try:
            check(list(engine._store._conn.iterdump()) == json.loads((work / "learning.json").read_text(encoding="utf-8")),
                  "Upgrade changed learning records")
            check(engine.get_answer_detail(1)["snapshot"] == SNAPSHOT, "Upgrade lost history snapshot")
            check(config.read_user_config() == json.loads((work / "configuration.json").read_text(encoding="utf-8")),
                  "Upgrade changed settings")
            milestones.append("previous_bank_configuration_learning_snapshot_readable")
            copy_practice(SNAPSHOT)
            milestones.append("copy_question_and_gated_multiline_solution")
            from bagu_trainer.bank_cleanup import BankCleanupService
            from bagu_trainer.bank_inventory import BankInventory
            cleanup = BankCleanupService(service)
            plan = cleanup.plan("sources", [REPOS[1]])
            result = cleanup.execute(plan, engine=engine)
            write(work / "source-cleanup-result.json", asdict(result))
            check(result.switched and not result.failures and len(engine.kb.all()) == 2,
                  "Safe source removal failed: " + result.message)
            check(not calls, "Source removal redownloaded retained sources")
            milestones.append("active_source_rebuild_switch_cleanup")
            current = active_bank_dir(user)
            plan = cleanup.plan("history", protected=[current])
            check(plan.targets, "No historical versions to exercise")
            result = cleanup.execute(plan, engine=engine, protected=[current])
            check(not result.failures and active_bank_dir(user) == current and current.exists(), "History cleanup changed current bank")
            check(BankInventory(user).scan(load_sources(manifest)).totals["history"] == 0, "History space was not refreshed")
            check(engine.get_learning_stats()["total_attempts"] == 1,
                  "Cleanup changed unrelated learning records")
            check(list(engine._store._conn.iterdump()) == json.loads((work / "learning.json").read_text(encoding="utf-8")),
                  "Cleanup changed unrelated quizzes, attempts or learning state")
            check(engine.get_answer_detail(1)["snapshot"] == SNAPSHOT, "Cleanup lost history snapshot")
            check((work / "项目 文件" / "keep.py").read_text(encoding="utf-8") == "# 不得删除项目文件\n", "Project file changed")
            milestones.append("history_cleanup_keeps_current_and_learning")
        finally:
            if engine._store is not None:
                engine._store.close()
            engine._store = None
    result = {"passed": True, "phase": phase, "pid": os.getpid(), "frozen": bool(getattr(sys, "frozen", False)),
              "transport": "in-memory generated ZIP; no external network", "milestones": milestones}
    write(work / (phase + "-result.json"), result)
    return result


def copy_practice(snapshot):
    import tkinter as tk
    from bagu_trainer.tk_runtime import create_root
    from bagu_trainer.ui_theme import apply_theme
    from bagu_trainer.ui_state import window_var
    from bagu_trainer.practice_controller import PracticeController
    from bagu_trainer.practice_page import PracticePage
    from bagu_trainer.ui_copy import question_text, solution_text
    root = create_root()
    try:
        previous = root.clipboard_get()
    except tk.TclError:
        previous = None
    controller = PracticeController(window_var(root), window_var(root), window_var(root))
    try:
        page = PracticePage(root, apply_theme(root), controller)
        page.pack(fill="both", expand=True)
        controller.enter({"quiz_id": "copy-fixture", **snapshot})
        root.update()
        page.copy_question_button.invoke()
        expected = question_text(snapshot["question"], snapshot["options"])
        check(root.clipboard_get() == expected, "Question/options clipboard mismatch")
        check(page.copy_solution_button.instate(["disabled"]), "Answer available before submit")
        page._copy_solution()
        check(root.clipboard_get() == expected, "Hidden answer leaked")
        controller.choose("A")
        controller.submit()
        controller.apply_grade({"ok": True, "correct": True, "correct_answer": "A", "analysis": snapshot["analysis"]}, "A")
        page.copy_solution_button.invoke()
        check(root.clipboard_get() == solution_text("A", snapshot["analysis"]), "Multiline solution clipboard mismatch")
    finally:
        root.clipboard_clear()
        if previous is not None:
            root.clipboard_append(previous)
        controller.close()
        root.destroy()
