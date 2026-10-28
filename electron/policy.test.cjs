const test = require('node:test');
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');
const path = require('node:path');
const { trustedURL, externalURL, backendMethods, nativeMethods } = require('./policy.cjs');

test('production IPC trusts only the packaged document, including its answer hash', () => {
  const file = path.resolve('dist-renderer/index.html');
  assert.ok(trustedURL(pathToFileURL(file).href + '#answer', file));
  assert.ok(!trustedURL(pathToFileURL(path.resolve('other.html')).href, file));
  assert.ok(!trustedURL('https://example.org', file));
  assert.ok(!trustedURL('file://attacker/index.html', file));
});
test('development origin cannot be impersonated using a URL prefix', () => {
  assert.ok(trustedURL('http://127.0.0.1:5173/#dashboard', '', 'http://127.0.0.1:5173'));
  assert.ok(!trustedURL('http://127.0.0.1:5173.attacker.org/', '', 'http://127.0.0.1:5173'));
  assert.ok(!trustedURL('http://127.0.0.1:5174', '', 'http://127.0.0.1:5173'));
});
test('external navigation rejects executable protocols and embedded credentials', () => {
  assert.equal(externalURL('https://example.org/docs'), 'https://example.org/docs');
  for (const url of ['file:///C:/Windows/system32/cmd.exe', 'javascript:alert(1)', 'https://key@example.org', 'data:text/html,test'])
    assert.throws(() => externalURL(url));
});
test('renderer capabilities do not include arbitrary process, path or shutdown access', () => {
  for (const name of ['shutdown', 'uninstall.launch', 'startup.set', 'eval', 'execute', 'native.readFile', 'native.spawn'])
    assert.ok(!backendMethods.has(name) && !nativeMethods.has(name));
  assert.ok(backendMethods.has('practice.submit'));
});
