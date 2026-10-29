"""Probe a source/frozen JSON-lines backend using only a temporary synthetic profile."""
import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def probe(executable=None):
    from scripts.electron_fixture import create
    with tempfile.TemporaryDirectory(prefix='bagu-backend-probe-') as temporary:
        profile = Path(temporary) / '合成数据'
        create(profile)
        environment = {**os.environ, 'BAGU_USER_DATA_DIR': str(profile),
            'BAGU_TEST_INSTANCE': uuid.uuid4().hex, 'BAGU_LLM_API_KEY': 'synthetic-probe-key',
            'PYTHON_KEYRING_BACKEND': 'keyring.backends.null.Keyring', 'PYTHONIOENCODING': 'utf-8'}
        command = [str(Path(executable).resolve())] if executable else [sys.executable, '-u', '-B', str(ROOT / 'scripts/start_electron_backend.py')]
        messages = queue.Queue()
        child = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def read():
            for line in child.stdout:
                messages.put(json.loads(line))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        sequence = 0
        def call(method, params=None):
            nonlocal sequence
            sequence += 1
            child.stdin.write(json.dumps({'id': sequence, 'method': method, 'params': params or {}}) + '\n')
            child.stdin.flush()
            while True:
                reply = messages.get(timeout=30)
                if reply.get('type') == 'fatal':
                    raise AssertionError(reply['data']['message'])
                if reply.get('id') == sequence:
                    assert 'error' not in reply, reply.get('error')
                    return reply['result']
        try:
            assert call('snapshot')['ready']
            assert call('practice.enter', {'quiz_id': 'electron-fixture-1'})['quiz']
            assert call('practice.submit', {'quiz_id': 'electron-fixture-1', 'answer': 'A'})['correct']
            history = call('history.list')
            assert history['total'] == 1
            repeated = call('history.repractice', {'attempt_id': history['items'][0]['id']})
            assert repeated['quiz']['quiz_id'] != 'electron-fixture-1'
            plan = call('bank.plan', {'kind': 'history'})
            assert call('bank.clean', {'token': plan['token']})['ok']
            assert call('history.list')['total'] == 1
            assert any(q['quiz_id'] == repeated['quiz']['quiz_id'] for q in call('snapshot')['pending'])
            assert call('shutdown')['ok']
            child.stdin.close()
            assert child.wait(timeout=30) == 0
            reader.join(5)
            return {'passed': True, 'backend': 'frozen' if executable else 'source',
                    'checks': ['startup', 'answer', 'history', 'repractice', 'maintenance-pending-reload', 'normal-exit']}
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            reader.join(5)
            child.stdout.close()
            if not child.stdin.closed:
                child.stdin.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path)
    options = parser.parse_args()
    print(json.dumps(probe(options.executable)))
