import {copyFile, mkdir, readdir, rm, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
import {wasmBytes} from '@polkadot/wasm-crypto-wasm';

await rm('dist/python', {recursive: true, force: true});
await mkdir('dist/python', {recursive: true});
for (const file of await readdir('src')) {
  if (file.endsWith('.py')) await copyFile(`src/${file}`, `dist/python/${file}`);
}
await writeFile('dist/python/crypto.wasm', wasmBytes);
await build({
  entryPoints: ['src/crypto.mjs'], outfile: 'dist/python/crypto.mjs', bundle: true,
  format: 'esm', platform: 'browser', target: 'es2022', external: ['./crypto.wasm'],
  plugins: [{name: 'static-wasm', setup(builder) {
    builder.onResolve({filter: /^@polkadot\/wasm-crypto-init$/}, () => ({
      path: fileURLToPath(import.meta.resolve('@polkadot/wasm-crypto-init/none')),
    }));
  }}],
});
