const { spawn } = require('node:child_process');
const { createServer } = require('vite');

(async () => {
  const server = await createServer();
  await server.listen();
  const child = spawn(require('electron'), ['.'], {
    stdio: 'inherit', windowsHide: true, env: { ...process.env, BAGU_VITE_URL: 'http://127.0.0.1:5173' },
  });
  child.once('exit', async (code) => { await server.close(); process.exitCode = code || 0; });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
})().catch((error) => { console.error(error.message); process.exitCode = 1; });
