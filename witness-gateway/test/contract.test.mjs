import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {execFile, spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {promisify} from 'node:util';
import {Miniflare} from 'miniflare';
import {cryptoWaitReady, encodeAddress, sr25519PairFromSeed} from '@polkadot/util-crypto';
import {runtimeOptions} from './runtime-options.mjs';

test('Python transports connect over HTTPS/WSS and WitnessClient recovers evidence after final-status failure', {timeout: 90_000}, async () => {
  await cryptoWaitReady();
  const directory = await mkdtemp(join(tmpdir(), '404-python-contract-'));
  const certificate = join(directory, 'cert.pem'), key = join(directory, 'key.pem');
  let mf;
  const children = [], workers = [];
  function python(mode, data) {
    const child = spawn(process.env.PYTHON ?? 'python3', ['test/runtime_contract.py', mode], {
      env: {...process.env, SSL_CERT_FILE: certificate}, stdio: ['pipe', 'pipe', 'pipe'],
    });
    children.push(child);
    return new Promise((resolve, reject) => {
      let stderr = '';
      child.stdout.resume();
      child.stderr.on('data', chunk => {stderr += chunk;});
      child.on('error', reject);
      child.on('exit', code => code === 0 ? resolve() : reject(new Error(`Python ${mode}: ${stderr}`)));
      child.stdin.end(JSON.stringify(data));
    });
  }
  try {
    await promisify(execFile)('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes',
      '-keyout', key, '-out', certificate, '-days', '1', '-subj', '/CN=localhost',
      '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost']);
    const seed = createHash('sha256').update('python-wire-contract').digest();
    const hotkey = encodeAddress(sr25519PairFromSeed(seed).publicKey, 42);
    let dispatches = 0;
    mf = new Miniflare({...runtimeOptions(), https: true, httpsKeyPath: key, httpsCertPath: certificate,
      host: '127.0.0.1', port: 0, durableObjects: {MINERS: {className: 'MinerAdmission', useSQLite: true}},
      kvNamespaces: ['REGISTRY'], r2Buckets: ['TRANSCRIPTS'],
      bindings: {ROUND: 'test-0', NETWORK: 'finney', NETUID: '17', ROUND_END: String(Math.floor(Date.now() / 1000) + 120),
        RUNPOD_ENDPOINT: 'fixture', RUNPOD_API_KEY: 'fixture-only', MAX_JOBS_PER_MINER: '2'},
      outboundService: async request => {
        assert.equal(new URL(request.url).origin, 'https://api.runpod.ai');
        if (request.method === 'GET') return Response.json({status: 'IN_PROGRESS'});
        const {input} = await request.json(); dispatches++;
        assert.equal(input.round, 'test-0');
        const bucket = await mf.getR2Bucket('TRANSCRIPTS');
        const prefix = `rounds/test-0/${input.hotkey}/${input.exchange_id}/`;
        await bucket.put(prefix + 'status.json', JSON.stringify({status: 'failed', error_type: 'OSError'}));
        await bucket.put(prefix + 'response.sse', 'data: ' + JSON.stringify({type: 'response.completed', response: {
          status: 'completed', output: [{type: 'message', content: [{type: 'output_text', text: 'recorded fixture'}]}],
          usage: {input_tokens: 3, output_tokens: 1, total_tokens: 4},
        }}) + '\n\n');
        await bucket.put(prefix + 'manifest.json', JSON.stringify({
          prefix: prefix.slice(0, -1), round: input.round, hotkey: input.hotkey, exchange_id: input.exchange_id,
          verified_at: Date.now() / 1000, provider_status: 200, provider_rejected: false,
          usage: {input_tokens: 3, output_tokens: 1, total_tokens: 4},
        }));
        const running = python('worker', input);
        running.catch(() => {}); workers.push(running);
        return Response.json({id: 'fixture-job'});
      },
    });
    const origin = String(await mf.ready).replace(/\/$/, '');
    const kv = await mf.getKVNamespace('REGISTRY');
    await kv.put('membership.json', JSON.stringify({version: 1, network: 'finney', netuid: 17,
      block: 100, fetched_at: Date.now(), block_time: Date.now(), hotkeys: [hotkey]}));
    await python('miner', {seed: '0x' + seed.toString('hex'), origin});
    await Promise.all(workers);
    assert.equal(dispatches, 1);
  } finally {
    for (const child of children) if (child.exitCode === null) child.kill();
    await mf?.dispose();
    await rm(directory, {recursive: true, force: true});
  }
});
