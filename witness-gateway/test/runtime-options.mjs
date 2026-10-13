import {resolve} from 'node:path';

export function runtimeOptions(entrypoint = 'worker.mjs') {
  const directory = resolve(entrypoint === 'membership.mjs' ? 'dist/membership' : 'dist/gateway');
  return {
    modulesRoot: directory,
    modules: [{type: 'ESModule', path: `${directory}/${entrypoint}`},
      {type: 'CompiledWasm', path: `${directory}/crypto.wasm`}],
    compatibilityDate: '2025-10-11', compatibilityFlags: [],
  };
}
