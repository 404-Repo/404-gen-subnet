import assert from 'node:assert/strict';
import { test } from 'node:test';
import { getCachedPmrem } from '../src/pmrem-cache.js';

function fakeBrowser({ failures = 0 } = {}) {
  const calls = { contexts: 0, closed: 0, modes: [] };
  return {
    calls,
    async createBrowserContext() {
      calls.contexts++;
      return {
        async newPage() {
          return {
            async goto() {},
            async waitForFunction() {},
            async setOfflineMode() {},
            async evaluate(fn, mode) {
              calls.modes.push(mode);
              if (failures-- > 0) throw new Error('bake failed');
              return { width: 768, height: 1024, data: mode };
            },
          };
        },
        async close() { calls.closed++; },
      };
    },
  };
}

test('concurrent and later requests share one bake per lighting mode', async () => {
  const browser = fakeBrowser();
  const first = getCachedPmrem(browser, 'neutral');
  assert.equal(first, getCachedPmrem(browser, 'neutral'));
  const data = await first;
  assert.equal(await getCachedPmrem(browser, 'neutral'), data);
  await getCachedPmrem(browser, 'studio');
  assert.deepEqual(browser.calls, { contexts: 2, closed: 2, modes: ['neutral', 'studio'] });
});

test('follow lighting needs no environment or browser context', async () => {
  const browser = fakeBrowser();
  assert.equal(await getCachedPmrem(browser, 'follow'), null);
  assert.equal(browser.calls.contexts, 0);
});

test('a failed bake closes its context and can be retried', async () => {
  const browser = fakeBrowser({ failures: 1 });
  await assert.rejects(getCachedPmrem(browser, 'neutral'), /bake failed/);
  assert.equal(browser.calls.closed, 1);
  assert.equal((await getCachedPmrem(browser, 'neutral')).data, 'neutral');
  assert.equal(browser.calls.contexts, 2);
  assert.equal(browser.calls.closed, 2);
});

test('a replacement browser gets a newly baked environment', async () => {
  const before = fakeBrowser();
  const after = fakeBrowser();
  assert.notEqual(await getCachedPmrem(before, 'neutral'), await getCachedPmrem(after, 'neutral'));
  assert.equal(before.calls.contexts, 1);
  assert.equal(after.calls.contexts, 1);
});
