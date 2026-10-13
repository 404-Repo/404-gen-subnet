import {readdirSync} from 'node:fs';
import {resolve} from 'node:path';

export function runtimeOptions() {
  const directory = resolve('dist/python');
  return {
    modulesRoot: directory,
    modules: [{type: 'PythonModule', path: `${directory}/worker.py`},
      ...readdirSync(directory).filter(file => file.endsWith('.py') && file !== 'worker.py')
        .map(file => ({type: 'PythonModule', path: `${directory}/${file}`})),
      {type: 'ESModule', path: `${directory}/crypto.mjs`},
      {type: 'CompiledWasm', path: `${directory}/crypto.wasm`}],
    compatibilityDate: '2025-10-11', compatibilityFlags: ['python_workers', 'python_workers_314', 'python_dedicated_snapshot'],
  };
}
