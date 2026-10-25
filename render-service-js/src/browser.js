/**
 * Browser lifecycle management.
 *
 * Launches headless Chromium on startup with SwiftShader (software
 * rendering). Keeps the browser alive across requests. The render pool
 * manages per-context cleanup and terminates the browser if cleanup
 * cannot finish within the rendering deadline.
 */

import puppeteer from 'puppeteer';

let browser = null;
let launching = null;
let browserController = null;

const CHROME_ARGS = [
  '--no-sandbox',
  '--disable-setuid-sandbox',
  '--disable-dev-shm-usage',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
  '--disable-renderer-backgrounding',
  '--enable-webgl',
  '--use-gl=angle',
  '--use-angle=swiftshader',
  '--in-process-gpu',
];

export function isBrowserHealthy() {
  return browser != null && browser.connected;
}

export async function ensureBrowser() {
  if (browser && browser.connected) return browser;
  if (!launching) {
    launching = (async () => {
      if (browser) {
        try { await browser.close(); } catch {}
      }
      const controller = new AbortController();
      browser = await puppeteer.launch({
        headless: 'new',
        args: CHROME_ARGS,
        protocolTimeout: 30_000,
        signal: controller.signal,
      });
      browserController = controller;
      return browser;
    })().finally(() => { launching = null; });
  }
  return launching;
}

export function terminateBrowser(target) {
  if (target !== browser) return;
  browserController.abort();
  void browser.disconnect();
}

export async function closeBrowser() {
  if (launching) {
    try { await launching; } catch {}
  }
  if (browser) {
    try { await browser.close(); } catch {}
    browser = null;
  }
}
