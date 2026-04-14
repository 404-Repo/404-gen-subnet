/**
 * Browser lifecycle management.
 *
 * Launches headless Chromium once on startup, keeps it alive across requests.
 * Provides ensureBrowser() for the happy path and restartBrowser() for recovery
 * after crashes, timeouts, or WebGL context loss.
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

// USE_GL=egl: NVIDIA GPU hardware acceleration (for RunPod GPU workers).
// Default: SwANGLE (software-rendered WebGL via ANGLE + SwiftShader).
const GPU_ARGS = process.env.USE_GL === 'egl'
  ? ['--use-gl=egl', '--enable-gpu', '--disable-gpu-sandbox']
  : ['--use-gl=angle', '--use-angle=swiftshader', '--in-process-gpu'];

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
