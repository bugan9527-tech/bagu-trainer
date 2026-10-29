const { _electron: electron, expect } = require('@playwright/test');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');

(async () => {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'validation', `electron-${Date.now()}`);
  await fs.mkdir(output, { recursive: true });
  const profile = path.join(output, '合成验收数据');
  const python = process.env.BAGU_PYTHON || 'python';
  const modelRequests = [];
  const mock = http.createServer(async (req, res) => {
    try {
      if (req.url !== '/v1/chat/completions' || req.method !== 'POST') throw new Error('Unexpected mock request');
      let body = ''; for await (const chunk of req) body += chunk;
      const prompt = JSON.parse(body).messages[1].content;
      const reviewing = prompt.startsWith('{') && JSON.parse(prompt).task === 'review_context_grounding';
      modelRequests.push(reviewing ? 'review' : 'generate');
      const content = reviewing ? { context_relevant: true, source_supported: true, answer_unique: true,
        no_answer_leak: true, correct_answer: 'A', reason: '合成场景与来源一致，四个选项答案唯一。' } : {
        question: '模拟模型出题：订单数据库更新后，如何处理 Redis 旧缓存？',
        options: { A: '先更新数据库，再删除缓存并处理失败。', B: '永久保留旧缓存。', C: '停止所有数据库写入。', D: '只修改前端显示。' },
        answer: 'A', analysis: '数据库更新后删除缓存，并处理失败，让后续读取重新加载数据。',
        grounding: { context_quote: 'redis_cache_generation_marker',
          source_quote: prompt.split('"""\n')[1].split('\n"""')[0].slice(0, 80),
          rationale: '订单代码修改触发缓存失效，与自编来源的缓存更新规则对应。' },
      };
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify(content) } }] }));
    } catch { res.writeHead(500); res.end('{}'); }
  });
  await new Promise(resolve => mock.listen(0, '127.0.0.1', resolve));
  mock.unref();
  const fixture = spawnSync(python, ['-B', 'scripts/electron_fixture.py', profile, `http://127.0.0.1:${mock.address().port}/v1`], { cwd: root, encoding: 'utf8', windowsHide: true });
  if (fixture.status !== 0) throw new Error(fixture.stderr || '无法生成隔离验收数据。');
  const env = { ...process.env, BAGU_PYTHON: python, BAGU_USER_DATA_DIR: profile,
    BAGU_ELECTRON_USER_DATA: path.join(output, 'chromium-profile'),
    BAGU_TEST_INSTANCE: `electron-${crypto.randomUUID()}`, BAGU_LLM_API_KEY: 'synthetic-electron-key',
    HTTP_PROXY: 'http://127.0.0.1:9', HTTPS_PROXY: 'http://127.0.0.1:9', NO_PROXY: '127.0.0.1,localhost' };
  delete env.BAGU_VITE_URL;
  const app = await electron.launch({
    ...(process.env.BAGU_ELECTRON_EXECUTABLE ? { executablePath: process.env.BAGU_ELECTRON_EXECUTABLE, args: [] } : { args: [root] }),
    cwd: root, env, timeout: 60000,
  });
  const appProcess = app.process();
  const errors = [];
  try {
    const main = await app.firstWindow();
    main.on('pageerror', (error) => errors.push(error.message));
    await expect(main.getByRole('heading', { name: '今天的练习' })).toBeVisible({ timeout: 60000 });
    await expect(main.getByText('2 道情景题，等你解答')).toBeVisible();
    await main.screenshot({ path: path.join(output, 'dashboard.png') });
    await main.getByRole('button', { name: '模型配置', exact: true }).click();
    await main.getByLabel('模型名称', { exact: true }).fill('unsaved-model-draft');
    await main.getByLabel('模型名称', { exact: true }).press('Control+r');
    await expect.poll(() => app.windows().length).toBe(1);
    await expect(main.getByLabel('模型名称', { exact: true })).toHaveValue('unsaved-model-draft');
    await main.getByRole('button', { name: '监听项目', exact: true }).click();
    await expect(main.locator('.project-row').getByText('示例代码项目', { exact: true })).toBeVisible();
    await main.getByRole('button', { name: '模型配置', exact: true }).click();
    await expect(main.getByLabel('模型名称', { exact: true })).toHaveValue('unsaved-model-draft');
    await main.getByRole('button', { name: '练习中心', exact: false }).click();
    await expect(main.locator('.quiz-row')).toHaveCount(2);
    const before = await main.evaluate(() => window.trainer.request('snapshot'));
    if (before.pending.some((quiz) => quiz.status !== 'ready' || quiz.answer || quiz.analysis)) throw new Error('浏览列表交付了题目或泄露答案。');
    const answerPromise = app.waitForEvent('window');
    await main.locator('.quiz-row').first().click();
    const answer = await answerPromise;
    answer.on('pageerror', (error) => errors.push(error.message));
    await expect(answer.getByRole('heading', { name: '缓存一致性练习题' })).toBeVisible();
    await answer.getByRole('radio', { name: /先更新数据库/ }).check();
    await answer.screenshot({ path: path.join(output, 'answer.png') });
    await answer.getByRole('button', { name: '收起练习' }).click();
    await main.locator('.quiz-row').first().click();
    await expect(answer.getByRole('radio', { name: /先更新数据库/ })).toBeChecked();
    await expect.poll(() => app.windows().length).toBe(2);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().find((window) => window.getTitle().startsWith('练习 ·'))?.close());
    await main.locator('.quiz-row').first().click();
    await expect(answer.getByRole('radio', { name: /先更新数据库/ })).toBeChecked();
    await answer.keyboard.press('Control+Enter');
    await expect(answer.getByRole('heading', { name: '回答正确' })).toBeVisible();
    await expect(answer.getByText('本次作答已保存')).toBeVisible();
    await expect(answer.getByRole('button', { name: '提交答案' })).toHaveCount(0);
    const rejected = await main.evaluate(() => window.trainer.request('practice.submit', { quiz_id: 'electron-fixture-1', answer: 'A' }).then(() => false, () => true));
    if (!rejected) throw new Error('重复提交未被拒绝。');
    await answer.screenshot({ path: path.join(output, 'grade.png') });
    await main.getByRole('button', { name: '答题记录', exact: true }).click();
    await expect(main.locator('.history-row')).toHaveCount(1);
    await main.locator('.history-row').click();
    await expect(main.getByRole('heading', { name: '答案解析' })).toBeVisible();
    await main.getByRole('button', { name: '重新练习本题' }).click();
    await expect(answer.getByRole('button', { name: '提交答案' })).toBeVisible();
    await expect.poll(() => app.windows().length).toBe(2);
    // Exercise the real watcher and its unchanged 15-second stable trigger.
    await expect.poll(async () => Object.values((await main.evaluate(() => window.trainer.request('snapshot'))).project_states)
      .some(project => project.state === 'watching'), { timeout: 20000 }).toBe(true);
    const waiting = await main.evaluate(() => window.trainer.request('snapshot'));
    const previousIds = waiting.pending.map(quiz => quiz.quiz_id);
    await fs.appendFile(path.join(profile, '示例代码项目', 'cache.py'), '\n# Redis 缓存一致性如何处理？数据库更新成功后删除缓存，并处理删除失败与并发更新。\nredis_cache_generation_marker = "invalidate redis cache after database update"\n');
    await expect.poll(async () => (await main.evaluate(() => window.trainer.request('snapshot'))).pending
      .filter(quiz => !previousIds.includes(quiz.quiz_id)).length, { timeout: 60000 }).toBe(1);
    const generated = (await main.evaluate(() => window.trainer.request('snapshot'))).pending.find(quiz => !previousIds.includes(quiz.quiz_id));
    if (modelRequests.join(',') !== 'generate,review') throw new Error(`Unexpected model calls: ${modelRequests}`);
    await main.evaluate(() => window.trainer.request('monitor.pause', { paused: true }));
    await main.evaluate(async () => {
      const snapshot = await window.trainer.request('snapshot');
      return window.trainer.request('bank.build', { repositories: [snapshot.sources[0].repository], timeout: 30 });
    });
    await expect.poll(async () => (await main.evaluate(() => window.trainer.request('snapshot'))).bank_task.state,
      { timeout: 30000 }).toBe('applied');
    const reloaded = await main.evaluate(() => window.trainer.request('snapshot'));
    if (!reloaded.paused || !reloaded.pending.some(quiz => quiz.quiz_id === generated.quiz_id)) throw new Error('Reload lost pending/paused state');
    await main.evaluate(id => window.trainer.request('practice.enter', { quiz_id: id }), generated.quiz_id);
    await expect(answer.getByRole('heading', { name: /Redis 缓存一致性/ })).toBeVisible();
    await answer.getByRole('radio', { name: /先更新数据库，再删除缓存并处理失败/ }).check();
    await answer.getByRole('button', { name: '提交答案', exact: true }).click();
    await expect(answer.getByRole('heading', { name: '回答正确' })).toBeVisible();
    if ((await main.evaluate(() => window.trainer.request('history.list'))).total !== 2) throw new Error('Generated answer did not persist');
    await main.getByRole('button', { name: '题库管理', exact: true }).click();
    await expect(main.locator('.source-row').first()).toBeVisible();
    await main.getByRole('button', { name: '应用设置', exact: true }).click();
    await expect(main.getByRole('heading', { name: '个人数据' })).toBeVisible();
    const security = await main.evaluate(() => ({ node: typeof window.require, process: typeof window.process }));
    if (security.node !== 'undefined' || security.process !== 'undefined') throw new Error('渲染进程暴露了 Node。');
    for (const [width, height] of [[800, 650], [1440, 900], [375, 812]]) {
      await main.setViewportSize({ width, height });
      await main.getByRole('button', { name: '工作台', exact: true }).click();
      const overflow = await main.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
      if (overflow) throw new Error(`${width}px 页面出现水平溢出。`);
    }
    await main.emulateMedia({ reducedMotion: 'reduce' });
    const cleaned = await main.evaluate(async () => {
      const snapshot = await window.trainer.request('snapshot');
      const plan = await window.trainer.request('bank.plan', { kind: 'sources', repositories: [snapshot.sources[0].repository] });
      if (!plan.last_source) throw new Error('Synthetic deletion did not select the last source');
      return window.trainer.request('bank.clean', { token: plan.token, include_unknown: true });
    });
    if (!cleaned.ok) throw new Error(cleaned.note);
    const cleared = await main.evaluate(() => window.trainer.request('snapshot'));
    if (cleared.ready || cleared.bank_count || cleared.pending.length) throw new Error('Cleanup revived removed content');
    if (errors.length) throw new Error(errors.join('\n'));
    await fs.writeFile(path.join(output, 'RESULTS.json'), JSON.stringify({ passed: true,
      checks: ['seven-pages', 'real-python-engine', 'pending-read-only', 'answer-hidden', 'shared-draft',
        'single-answer-window', 'grade-persistence', 'duplicate-rejection', 'history', 'repractice',
        'watch-15-second-stability', 'mock-http-generation-review', 'bank-build-offline-cache', 'bank-reload-pending',
        'paused-answer', 'source-cleanup', 'node-isolation', 'responsive-layout', 'reduced-motion'], modelRequests, errors }, null, 2));
    console.log(`Electron desktop acceptance passed. Evidence: ${output}`);
  } finally { await app.close(); mock.close(); }
  if (appProcess.exitCode !== 0) throw new Error(`Electron exit code: ${appProcess.exitCode}`);
  await fs.writeFile(path.join(output, 'EXIT.json'), JSON.stringify({ passed: true, code: appProcess.exitCode }));
  const emptyProfile = path.join(output, '首次使用数据');
  await fs.mkdir(emptyProfile);
  const emptyApp = await electron.launch({
    ...(process.env.BAGU_ELECTRON_EXECUTABLE ? { executablePath: process.env.BAGU_ELECTRON_EXECUTABLE, args: [] } : { args: [root] }),
    cwd: root, timeout: 60000,
    env: { ...env, BAGU_USER_DATA_DIR: emptyProfile, BAGU_ELECTRON_USER_DATA: path.join(output, 'first-use-chromium'),
      BAGU_TEST_INSTANCE: `electron-${crypto.randomUUID()}` },
  });
  try {
    const firstUse = await emptyApp.firstWindow();
    await expect(firstUse.getByRole('heading', { name: '今天的练习' })).toBeVisible({ timeout: 60000 });
    await expect(firstUse.locator('.nav-item')).toHaveCount(7);
    await expect(firstUse.getByText('连接项目与知识', { exact: true })).toBeVisible();
    const snapshot = await firstUse.evaluate(() => window.trainer.request('snapshot'));
    if (snapshot.ready || snapshot.pending.length) throw new Error('首次使用错误加载了个人状态。');
    await firstUse.screenshot({ path: path.join(output, 'first-use.png') });
    await fs.writeFile(path.join(output, 'FIRST_USE.json'), JSON.stringify({ passed: true }));
    console.log('First-use readiness and seven-page navigation passed.');
  } finally { await emptyApp.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
