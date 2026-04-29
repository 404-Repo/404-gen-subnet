/**
 * Integration tests against a live render-service-js endpoint.
 *
 * Run:  node test/endpoint.test.mjs <URL> <API_KEY>
 *       npm run test:endpoint -- <URL> <API_KEY>
 *
 * Example:
 *   node test/endpoint.test.mjs https://2ff7m1heqfvsxi.api.runpod.ai rpa_xxx
 *
 * Tests both successful renders and rejection of every prohibited pattern.
 */

const [url, apiKey] = process.argv.slice(2);

if (!url || !apiKey) {
  console.error('Usage: node test/endpoint.test.mjs <URL> <API_KEY>');
  process.exit(1);
}

const BASE = url.replace(/\/$/, '');
const HEADERS = {
  'Authorization': `Bearer ${apiKey}`,
  'Content-Type': 'application/json',
};

let passed = 0;
let failed = 0;

async function test(name, { path = '/render/grid', source, expectedStatus, expectedRule }) {
  try {
    const resp = await fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: HEADERS,
      body: JSON.stringify({ source }),
      signal: AbortSignal.timeout(30_000),
    });

    const status = resp.status;
    const contentType = resp.headers.get('content-type') || '';

    if (status !== expectedStatus) {
      let detail = '';
      if (contentType.includes('json')) {
        const body = await resp.json();
        detail = ` — ${JSON.stringify(body).slice(0, 200)}`;
      }
      failed++;
      console.error(`FAIL [${status}] ${name} — expected ${expectedStatus}${detail}`);
      return;
    }

    if (expectedRule) {
      const body = await resp.json();
      const allFailures = body.failures || [];
      const matchesRule = allFailures.some(f => f.rule === expectedRule);
      if (matchesRule) {
        passed++;
      } else {
        failed++;
        console.error(`FAIL [${status}] ${name} — expected rule ${expectedRule}, got: ${JSON.stringify(body).slice(0, 300)}`);
      }
      return;
    }

    if (expectedStatus === 200) {
      if (contentType.includes('image/png')) {
        const buf = await resp.arrayBuffer();
        if (buf.byteLength > 100) {
          passed++;
        } else {
          failed++;
          console.error(`FAIL [${status}] ${name} — PNG too small (${buf.byteLength} bytes)`);
        }
        return;
      }
      if (contentType.includes('json')) {
        const body = await resp.json();
        if (body.images?.length > 0 || body.status) {
          passed++;
        } else {
          failed++;
          console.error(`FAIL [${status}] ${name} — unexpected JSON: ${JSON.stringify(body).slice(0, 200)}`);
        }
        return;
      }
    }

    passed++;
  } catch (err) {
    failed++;
    console.error(`FAIL ${name} — ${err.message}`);
  }
}

console.log(`Testing ${BASE}\n`);

// ─── Health ──────────────────────────────────────────────────────────────────

console.log('=== Health ===');

const pingResp = await fetch(`${BASE}/ping`, {
  headers: { 'Authorization': `Bearer ${apiKey}` },
  signal: AbortSignal.timeout(120_000),
});
if (pingResp.status === 200) {
  passed++;
  console.log('PASS ping');
} else {
  failed++;
  console.error(`FAIL ping — ${pingResp.status}`);
}

const healthResp = await fetch(`${BASE}/health`, {
  headers: { 'Authorization': `Bearer ${apiKey}` },
  signal: AbortSignal.timeout(10_000),
});
if (healthResp.status === 200) {
  passed++;
  console.log('PASS health');
} else {
  failed++;
  console.error(`FAIL health — ${healthResp.status}`);
}

// ─── Successful renders ──────────────────────────────────────────────────────

console.log('\n=== Successful renders ===');

await test('grid — basic cube', {
  source: `export default function(THREE) {
    return new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.5, 0.5),
      new THREE.MeshStandardMaterial({ color: 0x2194ce })
    );
  }`,
  expectedStatus: 200,
});

await test('grid — PBR sphere with roughness/metalness', {
  source: `export default function(THREE) {
    return new THREE.Mesh(
      new THREE.SphereGeometry(0.4, 64, 64),
      new THREE.MeshStandardMaterial({ color: 0xff4444, roughness: 0.3, metalness: 0.7 })
    );
  }`,
  expectedStatus: 200,
});

await test('grid — group with multiple meshes', {
  source: `export default function(THREE) {
    const g = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial({ color: 0x44aa88 });
    for (let i = 0; i < 3; i++) {
      const m = new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.15, 0.15), mat);
      m.position.set((i - 1) * 0.25, 0, 0);
      g.add(m);
    }
    return g;
  }`,
  expectedStatus: 200,
});

await test('grid — instanced mesh', {
  source: `export default function(THREE) {
    const geo = new THREE.SphereGeometry(0.05, 16, 16);
    const mat = new THREE.MeshStandardMaterial({ color: 0xffaa00 });
    const im = new THREE.InstancedMesh(geo, mat, 25);
    const dummy = new THREE.Object3D();
    for (let i = 0; i < 25; i++) {
      dummy.position.set((i % 5 - 2) * 0.15, Math.floor(i / 5) * 0.15 - 0.3, 0);
      dummy.updateMatrix();
      im.setMatrixAt(i, dummy.matrix);
    }
    return im;
  }`,
  expectedStatus: 200,
});

await test('grid — studio lighting (default)', {
  path: '/render/grid?lighting=studio',
  source: `export default function(THREE) {
    return new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5), new THREE.MeshStandardMaterial({ color: 0x2194ce }));
  }`,
  expectedStatus: 200,
});

await test('grid — follow lighting', {
  path: '/render/grid?lighting=follow',
  source: `export default function(THREE) {
    return new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5), new THREE.MeshStandardMaterial({ color: 0x2194ce }));
  }`,
  expectedStatus: 200,
});

await test('grid — custom bg_color', {
  path: '/render/grid?bg_color=ffffff',
  source: `export default function(THREE) {
    return new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5), new THREE.MeshStandardMaterial({ color: 0x2194ce }));
  }`,
  expectedStatus: 200,
});

await test('individual views', {
  path: '/render?thetas=45',
  source: `export default function(THREE) {
    return new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5), new THREE.MeshStandardMaterial({ color: 0x2194ce }));
  }`,
  expectedStatus: 200,
});

// ─── Structural rejections ───────────────────────────────────────────────────

console.log('\n=== Structural rejections ===');

await test('no export default', {
  source: `function generate(THREE) { return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'MISSING_DEFAULT_EXPORT',
});

await test('export default not a function', {
  source: `export default 42;`,
  expectedStatus: 422,
  expectedRule: 'MISSING_DEFAULT_EXPORT',
});

await test('multiple exports', {
  source: `export const foo = 1;\nexport default function(THREE) { return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'MULTIPLE_TOP_LEVEL_EXPORTS',
});

await test('async function', {
  source: `export default async function(THREE) { return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'ASYNC_NOT_ALLOWED',
});

// ─── Forbidden identifiers ──────────────────────────────────────────────────

console.log('\n=== Forbidden identifiers ===');

for (const id of [
  'eval', 'Function', 'setTimeout', 'setInterval',
  'fetch', 'XMLHttpRequest', 'WebSocket',
  'document', 'window', 'navigator',
  'localStorage', 'crypto', 'Date', 'performance',
  'Proxy', 'Reflect', 'Worker', 'SharedArrayBuffer',
  'globalThis', 'process', 'require',
]) {
  await test(`forbidden: ${id}`, {
    source: `export default function(THREE) { const x = ${id}; return new THREE.Group(); }`,
    expectedStatus: 422,
    expectedRule: 'FORBIDDEN_IDENTIFIER',
  });
}

await test('Math.random', {
  source: `export default function(THREE) { Math.random(); return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'FORBIDDEN_IDENTIFIER',
});

// ─── Dynamic import / import.meta ────────────────────────────────────────────

console.log('\n=== Dynamic import ===');

await test('dynamic import()', {
  source: `export default function(THREE) { import("data:text/javascript,1"); return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'FORBIDDEN_IDENTIFIER',
});

await test('import.meta', {
  source: `export default function(THREE) { const u = import.meta.url; return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'FORBIDDEN_IDENTIFIER',
});

await test('static import', {
  source: `import fs from "fs";\nexport default function(THREE) { return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'FORBIDDEN_IDENTIFIER',
});

// ─── Forbidden Three.js APIs ─────────────────────────────────────────────────

console.log('\n=== Forbidden Three.js APIs ===');

for (const api of [
  'AnimationMixer', 'SkinnedMesh',
  'MeshLambertMaterial', 'MeshPhongMaterial',
  'ShaderMaterial', 'GLTFLoader', 'TextureLoader', 'CanvasTexture',
]) {
  await test(`THREE.${api}`, {
    source: `export default function(THREE) { new THREE.${api}(); return new THREE.Group(); }`,
    expectedStatus: 422,
    expectedRule: 'FORBIDDEN_THREE_API',
  });
}

await test('THREE.MathUtils.seededRandom', {
  source: `export default function(THREE) { THREE.MathUtils.seededRandom(); return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'FORBIDDEN_THREE_API',
});

// ─── Computed property access ────────────────────────────────────────────────

console.log('\n=== Computed property access ===');

await test('computed THREE[name]', {
  source: `export default function(THREE) { const n = "Group"; return new THREE[n](); }`,
  expectedStatus: 422,
  expectedRule: 'COMPUTED_PROPERTY_ACCESS',
});

await test('computed Math[fn]', {
  source: `export default function(THREE) { Math["sin"](1); return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'COMPUTED_PROPERTY_ACCESS',
});

// ─── THREE at top level ──────────────────────────────────────────────────────

console.log('\n=== THREE at top level ===');

await test('THREE at top level', {
  source: `const x = THREE;\nexport default function(THREE) { return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'THREE_AT_TOP_LEVEL',
});

// ─── Post-execution validation ───────────────────────────────────────────────

console.log('\n=== Post-execution validation ===');

await test('empty scene (no geometry)', {
  source: `export default function(THREE) { return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'EMPTY_SCENE',
});

await test('bounding box out of range', {
  source: `export default function(THREE) {
    return new THREE.Mesh(new THREE.BoxGeometry(3, 3, 3), new THREE.MeshStandardMaterial());
  }`,
  expectedStatus: 422,
  expectedRule: 'BOUNDING_BOX_OUT_OF_RANGE',
});

await test('bounding box offset out of range', {
  source: `export default function(THREE) {
    const m = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), new THREE.MeshStandardMaterial());
    m.position.set(5, 0, 0);
    return m;
  }`,
  expectedStatus: 422,
  expectedRule: 'BOUNDING_BOX_OUT_OF_RANGE',
});

await test('draw call limit exceeded (>200)', {
  source: `export default function(THREE) {
    const g = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial();
    for (let i = 0; i < 210; i++) {
      g.add(new THREE.Mesh(new THREE.BoxGeometry(0.01, 0.01, 0.01), mat));
    }
    return g;
  }`,
  expectedStatus: 422,
  expectedRule: 'DRAW_CALL_LIMIT_EXCEEDED',
});

await test('depth limit exceeded (>32)', {
  source: `export default function(THREE) {
    let parent = new THREE.Group();
    const root = parent;
    for (let i = 0; i < 35; i++) {
      const child = new THREE.Group();
      parent.add(child);
      parent = child;
    }
    parent.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), new THREE.MeshStandardMaterial()));
    return root;
  }`,
  expectedStatus: 422,
  expectedRule: 'DEPTH_LIMIT_EXCEEDED',
});

await test('instance limit exceeded (>50000)', {
  source: `export default function(THREE) {
    const geo = new THREE.BoxGeometry(0.05, 0.05, 0.05);
    const mat = new THREE.MeshStandardMaterial();
    const im = new THREE.InstancedMesh(geo, mat, 60000);
    im.count = 60000;
    return im;
  }`,
  expectedStatus: 422,
  expectedRule: 'INSTANCE_LIMIT_EXCEEDED',
});

await test('generate throws', {
  source: `export default function(THREE) { throw new Error("boom"); }`,
  expectedStatus: 422,
  expectedRule: 'EXECUTION_THREW',
});

await test('non-Object3D return', {
  source: `export default function(THREE) { return 42; }`,
  expectedStatus: 422,
  expectedRule: 'INVALID_RETURN_TYPE',
});

await test('null return', {
  source: `export default function(THREE) { return null; }`,
  expectedStatus: 422,
  expectedRule: 'INVALID_RETURN_TYPE',
});

await test('vertex limit exceeded (>250k)', {
  source: `export default function(THREE) {
    return new THREE.Mesh(
      new THREE.SphereGeometry(0.4, 500, 500),
      new THREE.MeshStandardMaterial()
    );
  }`,
  expectedStatus: 422,
  expectedRule: 'VERTEX_LIMIT_EXCEEDED',
});

await test('texture data exceeded (>4 MB)', {
  source: `export default function(THREE) {
    const data = new Uint8Array(5 * 1024 * 1024);
    for (let i = 0; i < data.length; i++) data[i] = (i * 7) & 255;
    const tex = new THREE.DataTexture(data, 1280, 1024, THREE.RGBAFormat);
    const mat = new THREE.MeshStandardMaterial({ map: tex });
    return new THREE.Mesh(new THREE.PlaneGeometry(0.5, 0.5), mat);
  }`,
  expectedStatus: 422,
  expectedRule: 'TEXTURE_DATA_EXCEEDED',
});

await test('execution timeout (infinite loop)', {
  source: `export default function(THREE) { while(true) {} }`,
  expectedStatus: 422,
  expectedRule: 'TIMEOUT_EXCEEDED',
});

// ─── Identifier allowlist ────────────────────────────────────────────────────

console.log('\n=== Identifier allowlist ===');

await test('unknown global (IDENTIFIER_NOT_ALLOWED)', {
  source: `export default function(THREE) { const x = someUnknownGlobal; return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'IDENTIFIER_NOT_ALLOWED',
});

await test('console not in allowlist', {
  source: `export default function(THREE) { console.log("hi"); return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'IDENTIFIER_NOT_ALLOWED',
});

// ─── Parse errors ────────────────────────────────────────────────────────────

console.log('\n=== Parse errors ===');

await test('syntax error', {
  source: `export default function(THREE { return new THREE.Group(); }`,
  expectedStatus: 422,
  expectedRule: 'PARSE_ERROR',
});

await test('file size > 1 MB', {
  source: `export default function(THREE) { return new THREE.Group(); }` + ' '.repeat(1024 * 1024),
  expectedStatus: 422,
  expectedRule: 'FILE_SIZE_EXCEEDED',
});

// ─── Literal budget ──────────────────────────────────────────────────────────

console.log('\n=== Literal budget ===');

// Constructor / codegen bypass

console.log('\n=== Constructor / codegen bypass ===');

await test('constructor bypass via array', {
  source: `export default function(THREE) {
    const fn = [].filter.constructor;
    fn("return this")();
    return new THREE.Group();
  }`,
  expectedStatus: 422,
  expectedRule: 'FORBIDDEN_PROPERTY_ACCESS',
});

await test('constructor bypass via string', {
  source: `export default function(THREE) {
    const F = "".constructor.constructor;
    return new THREE.Group();
  }`,
  expectedStatus: 422,
  expectedRule: 'FORBIDDEN_PROPERTY_ACCESS',
});

await test('__proto__ access', {
  source: `export default function(THREE) {
    const p = {}.__proto__;
    return new THREE.Group();
  }`,
  expectedStatus: 422,
  expectedRule: 'FORBIDDEN_PROPERTY_ACCESS',
});

await test('literal budget exceeded (>50 KB)', {
  source: `export default function(THREE) {
    const s = "${'x'.repeat(51 * 1024)}";
    return new THREE.Group();
  }`,
  expectedStatus: 422,
  expectedRule: 'LITERAL_BUDGET_EXCEEDED',
});

// ─── Summary ─────────────────────────────────────────────────────────────────

console.log(`\npassed: ${passed}`);
console.log(`failed: ${failed}`);
console.log(`total:  ${passed + failed}`);
process.exit(failed > 0 ? 1 : 0);
