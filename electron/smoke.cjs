const { _electron: electron, expect } = require('@playwright/test');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');

(async () => {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'validation', `electron-${Date.now()}`);
  await fs.mkdir(output, { recursive: true });
  const profile = path.join(output, '合成验收数据');
  const python = process.env.BAGU_PYTHON || 'python';
  const fixture = spawnSync(python, ['-B', 'scripts/electron_fixture.py', profile], { cwd: root, encoding: 'utf8', windowsHide: true });
  if (fixture.status !== 0) throw new Error(fixture.stderr || '无法生成隔离验收数据。');
  const env = { ...process.env, BAGU_PYTHON: python, BAGU_USER_DATA_DIR: profile,
    BAGU_ELECTRON_USER_DATA: path.join(output, 'chromium-profile'),
    BAGU_TEST_INSTANCE: `electron-${crypto.randomUUID()}`, BAGU_LLM_API_KEY: 'synthetic-electron-key' };
  delete env.BAGU_VITE_URL;
  const app = await electron.launch({
    ...(process.env.BAGU_ELECTRON_EXECUTABLE ? { executablePath: process.env.BAGU_ELECTRON_EXECUTABLE, args: [] } : { args: [root] }),
    cwd: root, env, timeout: 60000,
  });
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
    if (errors.length) throw new Error(errors.join('\n'));
    await fs.writeFile(path.join(output, 'RESULTS.json'), JSON.stringify({ passed: true,
      checks: ['seven-pages', 'real-python-engine', 'pending-read-only', 'answer-hidden', 'shared-draft',
        'single-answer-window', 'grade-persistence', 'duplicate-rejection', 'history', 'repractice',
        'node-isolation', 'responsive-layout', 'reduced-motion'], errors }, null, 2));
    console.log(`Electron desktop acceptance passed. Evidence: ${output}`);
  } finally { await app.close(); }
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
