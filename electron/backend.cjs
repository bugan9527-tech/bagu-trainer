const { spawn } = require('node:child_process');
const { existsSync } = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { EventEmitter } = require('node:events');

class Backend extends EventEmitter {
  constructor({ root, packaged, resourcesPath, environment = {} }) {
    super();
    this.pending = new Map();
    this.nextId = 0;
    const localPython = path.join(root, '.venv-build', 'Scripts', 'python.exe');
    const executable = packaged ? path.join(resourcesPath, '..', 'BaguBackend.exe')
      : process.env.BAGU_PYTHON || (existsSync(localPython) ? localPython : 'python');
    const args = packaged ? [] : ['-u', '-B', path.join(root, 'scripts', 'start_electron_backend.py')];
    this.child = spawn(executable, args, {
      cwd: packaged ? resourcesPath : root, windowsHide: true,
      env: { ...process.env, ...environment, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    // Python writes its own bounded/redacted logs. Never echo child stderr to a renderer.
    this.child.stderr.on('data', () => {});
    const lines = readline.createInterface({ input: this.child.stdout, crlfDelay: Infinity });
    lines.on('line', (line) => {
      let message;
      try { message = JSON.parse(line); } catch { return; }
      if (message.id != null) {
        const request = this.pending.get(message.id);
        if (request) {
          this.pending.delete(message.id);
          message.error ? request.reject(new Error(message.error)) : request.resolve(message.result);
        }
      } else if (message.type) this.emit('event', message);
    });
    this.child.on('error', () => this.fail('无法启动 Python 引擎，请检查运行环境。'));
    this.exited = new Promise((resolve) => this.child.once('close', (code) => {
      this.fail(code ? '学习引擎已退出，请关闭应用后重新启动。' : '学习引擎已关闭。');
      resolve(code);
    }));
    this.child.stdin.on('error', () => this.fail('无法连接学习引擎，请重新启动应用。'));
  }

  fail(message) {
    if (this.failure) return;
    this.failure = message;
    for (const { reject } of this.pending.values()) reject(new Error(message));
    this.pending.clear();
    this.emit('event', { type: 'fatal', data: { message } });
  }

  call(method, params = {}) {
    if (this.failure) return Promise.reject(new Error(this.failure));
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.child.stdin.write(JSON.stringify({ id, method, params }) + '\n', (error) => {
        if (error) { this.pending.delete(id); reject(new Error('引擎通信失败。')); }
      });
    });
  }

  async close() {
    if (!this.failure) {
      const completion = this.call('shutdown');
      this.child.stdin.end();
      await completion;
    }
    await this.exited;
  }
}

module.exports = { Backend };
