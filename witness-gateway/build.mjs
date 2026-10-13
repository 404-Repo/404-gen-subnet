import {mkdir, rm, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
import {wasmBytes} from '@polkadot/wasm-crypto-wasm';

await rm('dist', {recursive: true, force: true});
await mkdir('.wrangler/types', {recursive: true});
for (const [name, entry, exports] of [
  ['gateway', 'worker', 'default, MinerAdmission'],
  ['membership', 'membership', 'default'],
]) {
  const directory = `dist/${name}`;
  await mkdir(directory, {recursive: true});
  await writeFile(`${directory}/crypto.wasm`, wasmBytes);
  await writeFile(`${directory}/${entry}.d.ts`, `export {${exports}} from "../../src/${entry}";\n`);
  await build({
    entryPoints: [`src/${entry}.ts`], outfile: `${directory}/${entry}.mjs`,
    bundle: true, format: 'esm', platform: 'browser', target: 'es2022',
    external: ['./crypto.wasm', 'cloudflare:workers'],
    plugins: [{name: 'static-wasm', setup(builder) {
      builder.onResolve({filter: /^@polkadot\/wasm-crypto-init$/}, () => ({
        path: fileURLToPath(import.meta.resolve('@polkadot/wasm-crypto-init/none')),
      }));
    }}],
  });
}
