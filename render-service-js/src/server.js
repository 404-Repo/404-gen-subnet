/**
 * Rendering service — renders Three.js JS submissions to multi-view images.
 *
 * Three-stage pipeline per request:
 *   Stage 1: Static analysis (parse + AST rules)
 *   Stage 2: Execution + post-validation (worker_thread pool)
 *   Stage 3: Render (Chromium BrowserContext pool)
 *
 * Endpoints:
 *     POST /render         → list of individual view images (PNG)
 *     POST /render/grid    → single composite grid image (PNG)
 *     GET  /health         → {"status": "ok"}
 *     GET  /ping           → 200 if ready, 204 if initializing
 */

import http from 'node:http';
import { URL } from 'node:url';

import { startStaticServer } from './static-server.js';
import { ensureBrowser } from './browser.js';
import { renderViews, renderGrid } from './renderer.js';
import { staticValidate } from './validator/index.js';
import { validationPool } from './validation-pool.js';

const PORT = parseInt(process.env.PORT || '80', 10);

let ready = false;

function parseQuery(urlStr) {
  const url = new URL(urlStr, 'http://localhost');
  const params = {};

  const thetas = url.searchParams.get('thetas');
  if (thetas) params.thetas = thetas.split(',').map(Number);

  const phis = url.searchParams.get('phis');
  if (phis) {
    const parts = phis.split(',').map(Number);
    params.phis = parts.length === 1 ? parts[0] : parts;
  }

  const imgSize = url.searchParams.get('img_size');
  if (imgSize) params.imgSize = parseInt(imgSize, 10);

  const camRadius = url.searchParams.get('cam_radius');
  if (camRadius) params.camRadius = parseFloat(camRadius);

  const camFovDeg = url.searchParams.get('cam_fov_deg');
  if (camFovDeg) params.camFovDeg = parseFloat(camFovDeg);

  const gap = url.searchParams.get('gap');
  if (gap) params.gap = parseInt(gap, 10);

  const bgColor = url.searchParams.get('bg_color');
  if (bgColor) params.bgColor = bgColor;

  const lighting = url.searchParams.get('lighting');
  if (lighting === 'follow') params.lighting = 'follow';

  return params;
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf-8');
}

async function getSource(body) {
  const json = JSON.parse(body);
  if (json.source) return json.source;
  if (json.url) {
    const resp = await fetch(json.url, { signal: AbortSignal.timeout(30_000) });
    if (!resp.ok) throw new Error(`Failed to fetch source: ${resp.status}`);
    return resp.text();
  }
  throw new Error('Provide "source" or "url" in the JSON body');
}

function sendJson(res, status, data) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(data));
}

function sendPng(res, buf) {
  res.writeHead(200, {
    'Content-Type': 'image/png',
    'Content-Length': buf.length,
  });
  res.end(buf);
}

function sendError(res, status, message) {
  sendJson(res, status, { error: message });
}

async function handleRender(req, res) {
  const t0 = performance.now();
  const body = await readBody(req);
  const source = await getSource(body);

  const tStatic = performance.now();
  const staticResult = staticValidate(source);
  const staticMs = (performance.now() - tStatic).toFixed(1);
  if (!staticResult.passed) {
    console.log(`[render] static analysis failed (${staticMs}ms)`);
    sendJson(res, 422, { error: 'validation_failed', failures: staticResult.failures });
    return;
  }

  const tExec = performance.now();
  const execResult = await validationPool.validate(source);
  const execMs = (performance.now() - tExec).toFixed(1);
  if (!execResult.passed) {
    console.log(`[render] post-validation failed (${execMs}ms)`);
    sendJson(res, 422, {
      error: 'post_validation_failed',
      failures: execResult.failures,
      metrics: execResult.metrics,
    });
    return;
  }

  const options = parseQuery(req.url);
  const tRender = performance.now();
  const images = await renderViews(source, options);
  const renderMs = (performance.now() - tRender).toFixed(1);
  const totalMs = (performance.now() - t0).toFixed(1);
  console.log(`[render] ${images.length} views — static ${staticMs}ms, exec ${execMs}ms, render ${renderMs}ms, total ${totalMs}ms`);

  if (images.length === 1) {
    sendPng(res, images[0]);
    return;
  }

  const encoded = images.map((buf) => buf.toString('base64'));
  sendJson(res, 200, {
    images: encoded,
    count: encoded.length,
    img_size: options.imgSize || 518,
  });
}

async function handleRenderGrid(req, res) {
  const t0 = performance.now();
  const body = await readBody(req);
  const source = await getSource(body);

  const tStatic = performance.now();
  const staticResult = staticValidate(source);
  const staticMs = (performance.now() - tStatic).toFixed(1);
  if (!staticResult.passed) {
    console.log(`[render/grid] static analysis failed (${staticMs}ms)`);
    sendJson(res, 422, { error: 'validation_failed', failures: staticResult.failures });
    return;
  }

  const tExec = performance.now();
  const execResult = await validationPool.validate(source);
  const execMs = (performance.now() - tExec).toFixed(1);
  if (!execResult.passed) {
    console.log(`[render/grid] post-validation failed (${execMs}ms)`);
    sendJson(res, 422, {
      error: 'post_validation_failed',
      failures: execResult.failures,
      metrics: execResult.metrics,
    });
    return;
  }

  const options = parseQuery(req.url);
  const tRender = performance.now();
  const gridBuf = await renderGrid(source, options);
  const renderMs = (performance.now() - tRender).toFixed(1);
  const totalMs = (performance.now() - t0).toFixed(1);
  console.log(`[render/grid] static ${staticMs}ms, exec ${execMs}ms, render ${renderMs}ms, total ${totalMs}ms`);

  sendPng(res, gridBuf);
}

const server = http.createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, 'http://localhost').pathname;

    if (req.method === 'GET' && pathname === '/health') {
      sendJson(res, 200, { status: 'ok' });
      return;
    }

    if (req.method === 'GET' && pathname === '/ping') {
      res.writeHead(ready ? 200 : 204);
      res.end();
      return;
    }

    if (req.method === 'POST' && pathname === '/render') {
      await handleRender(req, res);
      return;
    }

    if (req.method === 'POST' && pathname === '/render/grid') {
      await handleRenderGrid(req, res);
      return;
    }

    sendError(res, 404, 'not found');
  } catch (err) {
    console.error('Request error:', err);
    sendError(res, 500, err.message || 'internal server error');
  }
});

async function main() {
  console.log('Starting static file server...');
  await startStaticServer();

  server.listen(PORT, '0.0.0.0', () => {
    console.log(`Render service listening on port ${PORT}`);
  });

  console.log('Initializing validation pool...');
  await validationPool.init();

  console.log('Launching browser...');
  await ensureBrowser();

  ready = true;
  console.log('Browser ready, service is fully operational');
}

main().catch((err) => {
  console.error('Fatal startup error:', err);
  process.exit(1);
});
