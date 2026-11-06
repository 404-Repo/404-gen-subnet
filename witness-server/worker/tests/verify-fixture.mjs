import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { loadX509FromPem } from '../witness_pod/verifier/node_modules/@reclaimprotocol/tls/lib/index.js';
import { run } from '../witness_pod/verify.mjs';
const [mode, directory, params, provider] = process.argv.slice(2);
try {
  console.log(JSON.stringify(await run(mode, directory, params, provider, {
    host: 'localhost',
    rootCAs: [loadX509FromPem(readFileSync(join(directory, 'ca.pem'), 'utf8'))],
  })));
} catch (error) {
  if (mode !== 'finish' || error.message !== 'Witness verification rejected the exchange') throw error;
  console.error(error.message);
  process.exitCode = 2;
}
