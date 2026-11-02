import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { loadX509FromPem } from '../../verifier/node_modules/@reclaimprotocol/tls/lib/index.js';
import { run } from '../verify.mjs';
const [mode, directory, params, provider] = process.argv.slice(2);
console.log(JSON.stringify(await run(mode, directory, params, provider, {
  host: 'localhost',
  rootCAs: [loadX509FromPem(readFileSync(join(directory, 'ca.pem'), 'utf8'))],
})));
