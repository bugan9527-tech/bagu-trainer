"""Layer direction and legacy compatibility through public import/learning behaviour."""
import ast
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bagu_trainer import bootstrap
from bagu_trainer.infrastructure import config
from bagu_trainer.infrastructure.store import SCHEMA
from bagu_trainer.desktop.commands import COMMANDS
from bagu_trainer.desktop.service import DesktopService
from bagu_trainer.desktop.startup import StartupReport, ReadinessCheck
from tests.test_engine_instances import make_engine

ROOT = Path(__file__).resolve().parents[1]


def test_dependency_direction_and_sqlite_exit():
    violations = []
    for tier in ('domain', 'infrastructure', 'application'):
        for path in (ROOT / 'bagu_trainer' / tier).rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                         [item.name for item in node.names] if isinstance(node, ast.Import) else [])
                for name in names:
                    if name.startswith('sqlite3') and path.name != 'store.py':
                        violations.append((str(path.relative_to(ROOT)), name))
                    if not name.startswith('bagu_trainer.'):
                        continue
                    forbidden = (tier == 'domain' and not name.startswith('bagu_trainer.domain')) or (
                        tier == 'infrastructure' and name.startswith(('bagu_trainer.desktop', 'bagu_trainer.application', 'bagu_trainer.bootstrap', 'bagu_trainer.engine'))) or (
                        tier == 'application' and name.startswith('bagu_trainer.desktop'))
                    if forbidden:
                        violations.append((str(path.relative_to(ROOT)), name))
    assert not violations


def test_transport_and_bootstrap_imports_do_not_load_tk_or_assemble_engine():
    code = '''
import sys
from bagu_trainer.infrastructure.knowledge_base import KnowledgeBase
KnowledgeBase.__init__ = lambda *a, **k: (_ for _ in ()).throw(AssertionError('bank loaded'))
import bagu_trainer.desktop.service
import bagu_trainer.bootstrap
import bagu_trainer.engine
assert 'bagu_trainer.desktop.tk_app' not in sys.modules
assert 'tkinter' not in sys.modules
'''
    subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, check=True, timeout=10)


def test_supported_legacy_import_shares_application_instance_and_lifecycle(tmp_path, monkeypatch):
    shared = make_engine(tmp_path, 'compatibility')
    monkeypatch.setattr(bootstrap, '_engine', shared)
    legacy = importlib.import_module('bagu_trainer.engine')
    canonical = importlib.import_module('bagu_trainer.application.learning.engine')
    assert legacy is canonical
    service = DesktopService()
    service.coordinator.checker = lambda: StartupReport((ReadinessCheck('synthetic', True, 'ready'),))
    monkeypatch.setattr(config, 'read_user_config', lambda: {'project_roots': shared.cfg['project_roots']})
    try:
        assert service.prepare()['ready']
        assert service.monitor.engine is shared is bootstrap.get_engine()
        assert legacy.learning is shared.learning
        info = {'context': 'synthetic compatibility context', 'sources': []}
        quiz = legacy.generate_activity(shared.cfg['project_roots'][0], info)[1][0]
        assert shared.learning.enter_question(quiz['quiz_id'])['quiz_id'] == quiz['quiz_id']
        assert service.command('practice.submit', {'quiz_id': quiz['quiz_id'], 'answer': 'A'})['ok']
        assert legacy.get_answer_history()['total'] == 1
    finally:
        service.close()
    with pytest.raises(RuntimeError, match='关闭'):
        shared.learning.resume_practice()
    shared.initialize()
    try:
        assert legacy.learning is shared.learning
        assert shared.learning.history()['total'] == 1
    finally:
        shared.close()


def test_persisted_and_desktop_contracts_match_pre_relocation_baseline():
    baseline = json.loads((ROOT / 'tests/fixtures/backend_contract_baseline.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(SCHEMA.encode('utf-8')).hexdigest() == baseline['sqlite_schema_sha256']
    tree = ast.parse((ROOT / 'bagu_trainer/infrastructure/config.py').read_text(encoding='utf-8'))
    defaults = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'DEFAULTS' for target in node.targets))
    assert hashlib.sha256(ast.dump(defaults).encode()).hexdigest() == baseline['config_defaults_ast_sha256']
    assert sorted(COMMANDS) == baseline['desktop_commands']
