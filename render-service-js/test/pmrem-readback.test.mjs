import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ensureBrowser, closeBrowser } from '../src/browser.js';
import { getCachedPmrem } from '../src/pmrem-cache.js';
import { renderGrid } from '../src/renderer.js';
import { startStaticServer } from '../src/static-server.js';

const source = `export default function(THREE) {
  return new THREE.Mesh(new THREE.SphereGeometry(0.35, 24, 16),
    new THREE.MeshStandardMaterial({ color: 0xb0a060, metalness: 1, roughness: 0.3 }));
}`;
const options = { lighting: 'neutral', thetas: [30], phis: [0], imgSize: 128 };

// Exercise failures that WebGL reports without throwing a JavaScript exception.
function installReadbackFault(fault) {
  if (fault === 'unreadable-type') {
    const original = window.THREE.PMREMGenerator.prototype.fromScene;
    window.THREE.PMREMGenerator.prototype.fromScene = function (...args) {
      const target = original.apply(this, args);
      target.texture.type = window.THREE.UnsignedIntType;
      return target;
    };
    return;
  }
  const original = WebGL2RenderingContext.prototype.readPixels;
  WebGL2RenderingContext.prototype.readPixels = function (...args) {
    if (fault === 'context-loss') {
      this.getExtension('WEBGL_lose_context').loseContext();
    } else {
      args[2] = -1; // Invalid width: sets INVALID_VALUE and leaves pixels unwritten.
    }
    return original.apply(this, args);
  };
}

const server = await startStaticServer();
try {
  await ensureBrowser();
  const expected = await renderGrid(source, options);
  for (const fault of ['context-loss', 'gl-error', 'unreadable-type']) {
    await test(`${fault} rejects the shared bake and the next request rebuilds it`, async () => {
      await closeBrowser();
      const browser = await ensureBrowser();
      const createContext = browser.createBrowserContext;
      let bakes = 0;
      browser.createBrowserContext = async function (...args) {
        const context = await createContext.apply(this, args);
        const newPage = context.newPage.bind(context);
        context.newPage = async () => {
          const page = await newPage();
          const evaluate = page.evaluate.bind(page);
          page.evaluate = async (fn, ...params) => {
            if (fn.name === 'bakeInPage' && ++bakes === 1) {
              await evaluate(installReadbackFault, fault);
            }
            return evaluate(fn, ...params);
          };
          return page;
        };
        return context;
      };
      try {
        const pending = getCachedPmrem(browser, 'neutral');
        assert.equal(getCachedPmrem(browser, 'neutral'), pending);
        await assert.rejects(pending, Error);
        assert.equal(bakes, 1);
        assert.equal(browser.connected, true, 'A failed bake must not require a browser restart');
        assert.equal(browser.browserContexts().length, 1, 'The failed baking context must close');

        const actual = await renderGrid(source, options);
        assert.ok(actual.equals(expected), 'Recovery must produce byte-identical PNGs');
        assert.equal(bakes, 2, 'The failed cache entry must be rebuilt');
        assert.ok((await renderGrid(source, options)).equals(expected));
        assert.equal(bakes, 2, 'The successful retry must stay cached');
        assert.equal(browser.browserContexts().length, 1, 'All disposable contexts must close');
      } finally {
        browser.createBrowserContext = createContext;
      }
    });
  }
} finally {
  await closeBrowser();
  await new Promise((resolve) => server.close(resolve));
}
