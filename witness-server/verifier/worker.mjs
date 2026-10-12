import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const DEFAULT_BINARY = fileURLToPath(new URL('../../witness-client/native/privateauth', import.meta.url));
export const DEFAULT_PARAMS = fileURLToPath(new URL('params', import.meta.url));

export function runProof(mode, input, { binary = DEFAULT_BINARY, params = DEFAULT_PARAMS, timeout = 120000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(binary, [mode, params], { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, GOMAXPROCS: '2', GOMEMLIMIT: '768MiB' } });
    let output = Buffer.alloc(0), error = Buffer.alloc(0), failure;
    const stop = reason => { failure ??= reason; child.kill('SIGKILL'); };
    const timer = setTimeout(() => stop(new Error('Credential proof timed out')), timeout);
    child.stdout.on('data', data => { output = Buffer.concat([output, data]); if (output.length > 128 * 1024) stop(new Error('Proof output limit')); });
    child.stderr.on('data', data => { if (error.length < 4096) error = Buffer.concat([error, data]); });
    child.once('error', reason => { clearTimeout(timer); reject(reason); });
    child.once('close', code => {
      clearTimeout(timer);
      if (failure) return reject(failure);
      if (code !== 0) return reject(new Error(`Credential ${mode} failed: ${error.toString().slice(0, 200)}`));
      try { resolve(JSON.parse(output.toString())); } catch { reject(new Error('Invalid proof result')); }
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(input));
  });
}

export async function makeCredentialProof(privateRecords, options) {
  if (privateRecords.length !== 2) throw new Error('Expected token and KeyUpdate');
  const [token, update] = [...privateRecords].sort((a, b) => a.index - b.index);
  if (token.number !== 0 || update.number !== 1 || token.key !== update.key || token.nonce !== update.nonce || update.index !== token.index + 1) throw new Error('Private epoch mismatch');
  const bundle = await runProof('prove', { key: token.key, nonce: token.nonce, token_record: token.record, update_record: update.record }, options);
  return { nonce: token.nonce, bundle };
}
