"""Desktop startup and settings must work without any protocol SDK imports."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop entry point")
def test_desktop_and_settings_start_when_protocol_sdk_imports_are_blocked(tmp_path):
    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "独立 桌面"
    for directory in ("bagu_trainer", "scripts"):
        shutil.copytree(source / directory, project / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (project / "data").mkdir()
    (project / "data" / "questions.json").write_text("[]\n", encoding="utf-8")
    probe = r'''
import runpy
import sys
from pathlib import Path

project = Path(sys.argv[1])
sys.path.insert(0, str(project))
class BlockProtocolSdk:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mcp' or fullname.startswith('mcp.'):
            raise AssertionError('Desktop tried to import the removed protocol SDK')
sys.meta_path.insert(0, BlockProtocolSdk())

from bagu_trainer import desktop, engine
assert not hasattr(engine, 'mcp')
assert not (project / 'bagu_trainer' / 'server.py').exists()
settings = runpy.run_path(str(project / 'scripts' / 'config_window.py'))
window = settings['build_window']()
window.withdraw()
window.update_idletasks()
window.destroy()

initialise = desktop.DesktopApp.__init__
status = {}
def bounded_init(self, *args, **kwargs):
    initialise(self, *args, **kwargs)
    self.root.withdraw()
    def finish():
        status['value'] = self.status.get()
        self.stop.set()
        self.root.destroy()
    self.root.after(1000, finish)
desktop.DesktopApp.__init__ = bounded_init
assert desktop.main([]) == 0
assert 'Error' not in status['value'], status
assert 'mcp' not in sys.modules
print('desktop and settings started without protocol SDK')
'''
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    env.pop("BAGU_LLM_API_KEY", None)
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-B", "-c", probe, str(project)],
                            cwd=tmp_path, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "desktop and settings started without protocol SDK" in result.stdout
