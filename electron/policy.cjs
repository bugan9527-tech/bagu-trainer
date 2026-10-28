const { pathToFileURL } = require('node:url');

const backendMethods = new Set([
  'snapshot', 'prepare', 'projects.save', 'monitor.pause', 'monitor.retry',
  'practice.enter', 'practice.resume', 'practice.next', 'practice.submit',
  'history.list', 'history.detail', 'history.repractice', 'model.test',
  'model.cancel', 'model.save', 'bank.build', 'bank.cancel', 'bank.inventory',
  'bank.plan', 'bank.clean', 'settings.save',
]);
const nativeMethods = new Set([
  'native.pickProject', 'native.openData', 'native.openExternal', 'native.startup',
  'native.startupStatus', 'native.hidePractice', 'practice.state', 'practice.select',
  'native.copyText',
  'native.uninstall',
  'native.continuePractice',
]);

function trustedURL(url, rendererPath, developmentURL) {
  try {
    const parsed = new URL(url);
    if (developmentURL) return parsed.origin === new URL(developmentURL).origin;
    return parsed.protocol === 'file:' && !parsed.host && parsed.pathname === new URL(pathToFileURL(rendererPath)).pathname;
  } catch { return false; }
}

function externalURL(value) {
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password)
    throw new Error('只允许打开普通 HTTP / HTTPS 链接。');
  return url.href;
}

module.exports = { backendMethods, nativeMethods, trustedURL, externalURL };
