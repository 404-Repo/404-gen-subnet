/**
 * Internal static file server for the render page and Three.js bundle.
 * Listens on localhost only — never exposed externally.
 */

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

const threePackageDir = path.resolve(path.dirname(require.resolve('three')), '..');
const threeBuildDir = path.join(threePackageDir, 'build');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
};

const STATIC_FILES = {
  '/render-page.html': path.join(__dirname, 'render-page.html'),
};

const cache = new Map();

function preload() {
  for (const [route, filePath] of Object.entries(STATIC_FILES)) {
    cache.set(route, fs.readFileSync(filePath));
  }
}

function resolveThreeFile(urlPath) {
  const cleaned = path.normalize(urlPath).replace(/^\/+/, '');
  const filePath = path.join(threeBuildDir, cleaned);
  if (!filePath.startsWith(threeBuildDir)) return null;
  if (!fs.existsSync(filePath)) return null;
  return filePath;
}

export const STATIC_PORT = parseInt(process.env.STATIC_PORT || '3000', 10);

export function startStaticServer() {
  preload();

  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      const cached = cache.get(req.url);
      if (cached) {
        const ext = path.extname(req.url) || '.html';
        res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
        res.end(cached);
        return;
      }

      const threeFile = resolveThreeFile(req.url);
      if (threeFile) {
        const ext = path.extname(threeFile);
        res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
        fs.createReadStream(threeFile).pipe(res);
        return;
      }

      res.writeHead(404);
      res.end();
    });

    server.listen(STATIC_PORT, '127.0.0.1', () => {
      resolve(server);
    });
    server.on('error', reject);
  });
}
