const { app, BrowserWindow, ipcMain, dialog, shell, Menu, clipboard } = require('electron');
const path = require('node:path');
const { Backend } = require('./backend.cjs');
const { backendMethods, nativeMethods, trustedURL, externalURL } = require('./policy.cjs');

const root = path.resolve(__dirname, '..');
const rendererPath = path.join(root, 'dist-renderer', 'index.html');
const developmentURL = !app.isPackaged ? process.env.BAGU_VITE_URL : undefined;
// Acceptance profiles must not create Chromium state in the real user profile.
if (process.env.BAGU_TEST_INSTANCE && process.env.BAGU_ELECTRON_USER_DATA)
  app.setPath('userData', path.resolve(process.env.BAGU_ELECTRON_USER_DATA));
let mainWindow, practiceWindow, backend, lastSnapshot, fatal, closing = false, quitReady = false;
let practice = { quiz: null, drafts: {}, grades: {}, busy: false, error: '' };

function broadcast(message) {
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed()) window.webContents.send('trainer:event', message);
  }
}

function publishPractice() { broadcast({ type: 'practice_state', data: practice }); }

function secureWindow(window) {
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => {
    if (!trustedURL(url, rendererPath, developmentURL)) event.preventDefault();
  });
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  window.webContents.session.setPermissionCheckHandler(() => false);
}

function makeWindow(answer = false) {
  const window = new BrowserWindow({
    width: answer ? 960 : 1260, height: answer ? 820 : 850,
    minWidth: answer ? 620 : 800, minHeight: 560, show: false,
    title: answer ? '练习 · AI 八股训练器' : 'AI 八股训练器',
    backgroundColor: '#f4f6fa', autoHideMenuBar: true,
    icon: path.join(root, 'packaging', 'BaguTrainer.ico'),
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true },
  });
  secureWindow(window);
  const hash = answer ? 'answer' : 'dashboard';
  if (developmentURL) window.loadURL(`${developmentURL}/#${hash}`);
  else window.loadFile(rendererPath, { hash });
  window.once('ready-to-show', () => {
    if (answer || !process.argv.includes('--background')) window.show();
    else { window.show(); window.minimize(); }
  });
  window.on('close', (event) => {
    if (quitReady) return;
    event.preventDefault();
    if (answer) window.hide();
    else app.quit();
  });
  return window;
}

function showPractice() {
  if (!practiceWindow || practiceWindow.isDestroyed()) practiceWindow = makeWindow(true);
  else { practiceWindow.show(); if (practiceWindow.isMinimized()) practiceWindow.restore(); practiceWindow.focus(); }
}

async function request(event, method, params = {}) {
  const window = BrowserWindow.fromWebContents(event.sender);
  if (!window || ![mainWindow, practiceWindow].includes(window) ||
      event.senderFrame !== event.sender.mainFrame ||
      !trustedURL(event.senderFrame.url, rendererPath, developmentURL)) throw new Error('窗口访问被拒绝。');
  if ((!backendMethods.has(method) && !nativeMethods.has(method)) ||
      !params || typeof params !== 'object' || Array.isArray(params) ||
      Buffer.byteLength(JSON.stringify(params)) > 200000) throw new Error('无效的桌面操作。');
  if (closing) throw new Error('正在保存并退出，请稍候。');
  if (method === 'native.pickProject') {
    const result = await dialog.showOpenDialog(window, { title: '选择要监听的代码项目', properties: ['openDirectory'] });
    return result.canceled ? null : result.filePaths[0];
  }
  if (method === 'native.openData') {
    const snapshot = await backend.call('snapshot');
    const error = await shell.openPath(snapshot.user_data);
    if (error) throw new Error('无法打开个人数据目录。');
    return { ok: true };
  }
  if (method === 'native.openExternal') { await shell.openExternal(externalURL(params.url)); return { ok: true }; }
  if (method === 'native.copyText') {
    if (typeof params.text !== 'string') throw new Error('无效的复制内容。');
    clipboard.writeText(params.text);
    return { ok: true };
  }
  if (method === 'native.startupStatus') return backend.call('startup.status');
  if (method === 'native.startup') {
    if (!app.isPackaged) throw new Error('请在打包后的桌面应用中设置登录自启动。');
    return backend.call('startup.set', { enabled: Boolean(params.enabled) });
  }
  if (method === 'native.uninstall') {
    const preview = await backend.call('uninstall.preview');
    const result = await dialog.showMessageBox(window, { type: 'warning', title: '完整卸载训练器',
      message: '退出应用并进入完整卸载工具？', detail: preview.detail,
      buttons: ['取消', '退出并进入完整卸载'], defaultId: 0, cancelId: 0, noLink: true });
    if (result.response !== 1) return { ok: false };
    await backend.call('uninstall.launch');
    app.quit();
    return { ok: true };
  }
  if (method === 'native.hidePractice') { practiceWindow?.hide(); return { ok: true }; }
  if (method === 'native.continuePractice') {
    if (practice.quiz) { showPractice(); return { ok: true }; }
    return request(event, 'practice.resume');
  }
  if (method === 'practice.state') return practice;
  if (method === 'practice.select') {
    if (practice.busy || !practice.quiz || practice.quiz.quiz_id !== params.quiz_id ||
        practice.grades[params.quiz_id] || !['A', 'B', 'C', 'D'].includes(params.answer)) return { ok: false };
    practice.drafts[params.quiz_id] = params.answer;
    publishPractice();
    return { ok: true };
  }
  if (method === 'snapshot' && fatal) throw new Error(fatal);
  const isPractice = method.startsWith('practice.') || method === 'history.repractice';
  if (isPractice) {
    if (practice.busy) throw new Error('正在处理当前题目，请稍候。');
    if (method === 'practice.submit' && params.quiz_id !== practice.quiz?.quiz_id) throw new Error('题目已切换，请重新打开。');
    practice.busy = true; practice.error = ''; publishPractice();
  }
  try {
    const result = await backend.call(method, params);
    if (isPractice) {
      if (result.ok === false) throw new Error(result.note || '操作未完成，请重试。');
      if (method === 'practice.submit') practice.grades[params.quiz_id] = result;
      else if (result.quiz) { practice.quiz = result.quiz; showPractice(); }
      else if (method === 'practice.next') practiceWindow?.hide();
    }
    return result;
  } catch (error) {
    if (isPractice) practice.error = error.message;
    throw error;
  } finally {
    if (isPractice) { practice.busy = false; publishPractice(); }
  }
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { mainWindow?.show(); mainWindow?.restore(); mainWindow?.focus(); });
  app.whenReady().then(() => {
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { label: '应用', submenu: [{ label: '继续练习', click: () => {
        if (practice.quiz) showPractice(); else {
          mainWindow?.webContents.send('trainer:event', { type: 'show', data: 'practice' }); mainWindow?.show();
        }
      } }, { type: 'separator' }, { role: 'quit', label: '退出' }] },
      { label: '编辑', submenu: [{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' },
        { type: 'separator' }, { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' }] },
      { label: '视图', submenu: [{ role: 'resetZoom', label: '实际大小' }, { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' }, ...(!app.isPackaged ? [{ role: 'toggleDevTools' }] : [])] },
    ]));
    ipcMain.handle('trainer:request', request);
    backend = new Backend({ root, packaged: app.isPackaged, resourcesPath: process.resourcesPath,
      environment: { BAGU_ELECTRON_OWNER_PID: String(process.pid) } });
    backend.on('event', (message) => {
      if (message.type === 'snapshot') lastSnapshot = message.data;
      if (message.type === 'fatal') fatal = message.data.message;
      if (message.type === 'content_changed') { practice = { quiz: null, drafts: {}, grades: {}, busy: false, error: '' }; publishPractice(); }
      if (message.type === 'bank_built') backend.call('prepare').catch(() => broadcast({ type: 'fatal', data: { message: '题库应用未完成，请重新检查引擎。' } }));
      if (message.type === 'show') { mainWindow?.show(); mainWindow?.restore(); mainWindow?.focus(); }
      broadcast(message);
    });
    mainWindow = makeWindow();
    mainWindow.webContents.on('did-finish-load', () => {
      if (lastSnapshot) broadcast({ type: 'snapshot', data: lastSnapshot });
      if (fatal) broadcast({ type: 'fatal', data: { message: fatal } });
    });
  });
  app.on('before-quit', (event) => {
    if (quitReady) return;
    event.preventDefault();
    if (closing) return;
    closing = true;
    broadcast({ type: 'closing', data: {} });
    (async () => {
      try { await backend?.close(); }
      finally { quitReady = true; app.quit(); }
    })();
  });
  app.on('window-all-closed', () => { if (!closing) app.quit(); });
}
