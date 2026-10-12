import {mkdir, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
import {wasmBytes} from '@polkadot/wasm-crypto-wasm';

const directory = fileURLToPath(new URL('./dist/', import.meta.url));
await mkdir(directory, {recursive: true});
await writeFile(`${directory}crypto.wasm`, wasmBytes);
await build({
  entryPoints: [fileURLToPath(new URL('./crypto.mjs', import.meta.url))],
  outfile: `${directory}crypto.mjs`, bundle: true, format: 'esm',
  platform: 'browser', target: 'es2022', external: ['./crypto.wasm'],
  plugins: [{name: 'static-wasm', setup(builder) {
    builder.onResolve({filter: /^@polkadot\/wasm-crypto-init$/}, () => ({
      path: fileURLToPath(import.meta.resolve('@polkadot/wasm-crypto-init/none')),
    }));
  }}],
});
