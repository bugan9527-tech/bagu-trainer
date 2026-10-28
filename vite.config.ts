import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ command }) => ({
  root: 'frontend',
  base: './',
  plugins: [react(), {
    name: 'desktop-development-csp',
    transformIndexHtml: (html) => command === 'serve'
      ? html.replace("script-src 'self';", "script-src 'self' 'unsafe-inline';") : html,
  }],
  build: { outDir: '../dist-renderer', emptyOutDir: true },
  server: { host: '127.0.0.1', port: 5173, strictPort: true },
}));
