const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { existsSync } = require('node:fs');

module.exports = async function afterPack({ appOutDir, packager }) {
  const root = packager.projectDir;
  const localPython = path.join(root, '.venv-build', 'Scripts', 'python.exe');
  const python = process.env.BAGU_BUILD_PYTHON || (existsSync(localPython) ? localPython : 'python');
  const provenance = JSON.parse(execFileSync(python, [path.join(root, 'scripts', 'stamp_electron.py'),
    path.join(appOutDir, 'BaguTrainer.exe')], { cwd: root, encoding: 'utf8', windowsHide: true }));
  await fs.copyFile(path.join(root, 'LICENSE'), path.join(appOutDir, 'BaguTrainer-LICENSE.txt'));
  await fs.copyFile(path.join(root, 'docs', 'ELECTRON_DESKTOP.md'), path.join(appOutDir, '使用说明.md'));
  const notices = ['Bundled web interface dependency licenses',
    'This inventory includes build-time packages as well as bundled dependencies.', ''];
  async function licenseDirectory(directory) {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
      const packageDirectory = path.join(directory, entry.name);
      if (entry.name.startsWith('@')) { await licenseDirectory(packageDirectory); continue; }
      let metadata;
      try { metadata = JSON.parse(await fs.readFile(path.join(packageDirectory, 'package.json'), 'utf8')); }
      catch { continue; }
      for (const name of await fs.readdir(packageDirectory)) {
        if (/^(?:licen[cs]e|copying|notice)(?:[.-]|$)/i.test(name) && (await fs.stat(path.join(packageDirectory, name))).isFile()) {
          notices.push(`\n--- ${metadata.name} ${metadata.version}: ${name} ---\n`,
            await fs.readFile(path.join(packageDirectory, name), 'utf8'));
        }
      }
      if (existsSync(path.join(packageDirectory, 'node_modules'))) await licenseDirectory(path.join(packageDirectory, 'node_modules'));
    }
  }
  await licenseDirectory(path.join(root, 'node_modules'));
  await fs.writeFile(path.join(appOutDir, 'WEB_DEPENDENCY_LICENSES.txt'), notices.join('\n'));
  const entries = [];
  const forbidden = new Set(['config.json', 'questions.json', 'question_concepts.json', 'onboarding.json',
    'active-bank.json', 'bridge-credentials.json', 'owner.lock']);
  async function visit(directory) {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) throw new Error('发布包禁止包含符号链接。');
      if (entry.isDirectory()) await visit(file);
      else if (entry.isFile() && entry.name !== 'BUILD_MANIFEST.json') {
        if (forbidden.has(entry.name.toLowerCase()) || /\.(?:db|sqlite|log|key|pem)$/i.test(entry.name))
          throw new Error(`发布包包含本机数据：${entry.name}`);
        const content = await fs.readFile(file);
        entries.push({ path: path.relative(appOutDir, file).split(path.sep).join('/'),
          bytes: content.length, sha256: crypto.createHash('sha256').update(content).digest('hex') });
      }
    }
  }
  await visit(appOutDir);
  await fs.writeFile(path.join(appOutDir, 'BUILD_MANIFEST.json'), JSON.stringify({
    application: 'BaguTrainer', version: packager.appInfo.version, layout: 'Electron with Python engine',
    architecture: 'windows-x64', ...provenance, files: entries,
  }, null, 2));
};
