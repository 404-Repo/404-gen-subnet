/**
 * Browser lifecycle management.
 *
 * Launches headless Chromium once on startup, keeps it alive across requests.
 * The render pool manages per-context recovery; restartBrowser() is only
 * called when the browser process itself is unhealthy.
 */

import puppeteer from 'puppeteer';

let browser = null;

const BASE_ARGS = [
  '--no-sandbox',
  '--disable-setuid-sandbox',
  '--disable-dev-shm-usage',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
  '--disable-renderer-backgrounding',
  '--enable-webgl',
  '--ignore-gpu-blocklist',
];

const GPU_ARGS = process.env.USE_GL === 'egl'
  ? ['--use-gl=egl', '--enable-gpu', '--disable-gpu-sandbox']
  : ['--use-gl=angle', '--use-angle=swiftshader', '--in-process-gpu'];

export function isBrowserHealthy() {
  return browser != null && browser.connected;
}

export async function ensureBrowser() {
  if (browser && browser.connected) return browser;
  if (browser) {
    try { await browser.close(); } catch {}
  }
  browser = await puppeteer.launch({
    headless: 'new',
    args: [...BASE_ARGS, ...GPU_ARGS],
    protocolTimeout: 30_000,
  });
  return browser;
}

export async function restartBrowser() {
  if (browser) {
    try { await browser.close(); } catch {}
  }
  browser = null;
  return ensureBrowser();
}

export async function closeBrowser() {
  if (browser) {
    try { await browser.close(); } catch {}
    browser = null;
  }
}
