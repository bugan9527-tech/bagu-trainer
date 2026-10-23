"""Relocated installs use their own paths; shortcuts are written only in temp dirs."""
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def relocated_project(tmp_path):
    project = tmp_path / "搬迁 示例" / "训练器"
    for name in ("bagu_trainer", "scripts"):
        shutil.copytree(ROOT / name, project / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return project


def test_example_config_uses_relocated_install_from_another_cwd(tmp_path):
    project = relocated_project(tmp_path)
    shutil.copy2(ROOT / "config.example.json", project / "config.json")
    user = tmp_path / "用户 Local AppData" / "BaguTrainer"
    (user / "data").mkdir(parents=True)
    shutil.copy2(ROOT / "config.example.json", user / "config.json")
    (user / "data" / "questions.json").write_text("[]\n", encoding="utf-8")
    elsewhere = tmp_path / "unrelated directory"
    elsewhere.mkdir()
    probe = """
import json
from pathlib import Path
from bagu_trainer import config, desktop_instance, engine
cfg = config.load_config()
print(json.dumps({
    'project': str(config.PROJECT_ROOT),
    'config': str(config.CONFIG_PATH),
    'roots': cfg['project_roots'],
    'trae': cfg['trae_home'],
    'expected_trae': str(Path.home() / '.trae-cn'),
    'db': engine._db_path(),
    'runtime': str(desktop_instance.RUNTIME),
}))
"""
    # The subprocess imports only the copied package and never starts the desktop.
    env = dict(os.environ, PYTHONPATH=str(project), PYTHONIOENCODING="utf-8", BAGU_USER_DATA_DIR=str(user))
    result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=elsewhere,
                            env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert Path(observed["project"]) == project
    assert Path(observed["config"]) == user / "config.json"
    assert observed["roots"] == []
    assert observed["trae"] == observed["expected_trae"]
    assert Path(observed["db"]) == user / "data" / "trainer.db"
    assert Path(observed["runtime"]) == user / "runtime"
    assert not (project / "data" / "trainer.db").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Startup shortcut integration")
@pytest.mark.parametrize("explicit", [False, True], ids=["project-venv", "explicit-python"])
def test_startup_uses_selected_python_after_relocation(tmp_path, explicit):
    project = relocated_project(tmp_path)
    # Reuse this test interpreter's dependencies, which may live in an isolated
    # venv. system_site_packages would incorrectly select the machine's base Python.
    venv.EnvBuilder(with_pip=False).create(project / ".venv")
    packages = project / ".venv" / "Lib" / "site-packages"
    dependency_path = sysconfig.get_path("purelib")
    (packages / "validation_dependencies.pth").write_text(
        "import site; site.addsitedir(" + ascii(dependency_path) + ")\n", encoding="ascii")
    startup = tmp_path / "temporary startup"
    startup.mkdir()
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(project / "scripts" / "install-startup.ps1"), "-NoStart",
               "-StartupDirectory", str(startup)]
    if explicit:
        command += ["-PythonExe", sys.executable]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    shortcut = startup / "BaguTrainer.lnk"
    assert shortcut.is_file()
    from bagu_trainer.login_startup import read_shortcut
    details = read_shortcut(shortcut)
    expected_python = Path(sys.executable) if explicit else project / ".venv" / "Scripts" / "python.exe"
    assert Path(details["target"]).resolve() == expected_python.with_name("pythonw.exe").resolve()
    assert Path(details["cwd"]) == project
    assert details["arguments"] == '-B "' + str(project / "scripts" / "start_desktop.py") + '" --background'
    assert not (project / "config.json").exists()
    assert not (project / "data" / "trainer.db").exists()
