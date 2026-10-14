/** Public admission and private allocation for queue-based witness workers. */
import {DurableObject} from 'cloudflare:workers';
import {measure, timing} from './timing';
import {UUID, authenticate, exchangePayload} from './auth';
import {Rejection, type Connection, type GatewayEnv, type GatewayRequest, type Job,
  type Json, type Rate, type Role, type WorkerConnection} from './models';
import {boundedText, failure, isRecord, json, nowMs, safeInteger, sha256, withStateLock} from './runtime';

function requireOpenRound(env: GatewayEnv): number {
  const end = Number(env.ROUND_END || '0');
  if (!safeInteger(end) || nowMs() / 1000 >= end) throw new Rejection(403, 'round_closed');
  return end;
}

async function completedStatus(env: GatewayEnv, input: GatewayRequest, prefix: string): Promise<Response | null> {
  // The manifest is written after verification and every evidence upload.
  const manifest = await env.TRANSCRIPTS.get(prefix + '/manifest.json');
  if (!manifest) return null;
  const integerStatuses = new WeakSet<object>();
  const data: unknown = JSON.parse(await boundedText(manifest.body, 64 * 1024),
    function(key, value: unknown, context?: {source?: string}) {
      // Python distinguishes integer tokens from decimal/exponent numbers.
      if (key === 'provider_status' && typeof value === 'number'
        && /^-?\d+$/.test(context?.source || '')) integerStatuses.add(this);
      return value;
    });
  if (!isRecord(data)) throw new Rejection(503, 'invalid_evidence_manifest');
  const verifiedAt = data.verified_at, providerStatus = data.provider_status;
  if (data.prefix !== prefix || data.round !== env.ROUND || data.hotkey !== input.hotkey
    || data.exchange_id !== input.requestId || typeof verifiedAt !== 'number'
    || !Number.isFinite(verifiedAt) || verifiedAt <= 0 || typeof providerStatus !== 'number'
    || !integerStatuses.has(data) || !Number.isInteger(providerStatus) || providerStatus < 100 || providerStatus > 599
    || typeof data.provider_rejected !== 'boolean') throw new Rejection(503, 'invalid_evidence_manifest');
  const usage = data.usage ?? null;
  return json({status: 'issued', authorized: true, usage, provider_status: providerStatus,
    usage_state: data.provider_rejected ? 'rejected' : usage !== null ? 'recorded' : 'unknown',
    evidence_expires: Math.trunc((verifiedAt + 7 * 86400) * 1000)});
}

export default {
  async fetch(request: Request, env: GatewayEnv): Promise<Response> {
    const started = performance.now();
    let exchangeId: string | null = null, outcome = 'ok';
    try {
      requireOpenRound(env);
      const url = new URL(request.url);
      const worker = /^\/workers\/([a-z0-9][a-z0-9-]{0,63})\/([1-9A-HJ-NP-Za-km-z]{48})\/([0-9a-f-]{36})(?![\s\S])/.exec(url.pathname);
      if (worker) {
        if (request.method !== 'GET' || url.search || worker[1] !== env.ROUND || !UUID.test(worker[3])
          || (request.headers.get('Upgrade') || '').toLowerCase() !== 'websocket') throw new Rejection(404, 'unknown_route');
        const authorization = request.headers.get('Authorization') || '';
        const token = authorization.startsWith('Bearer ') ? authorization.slice(7) : authorization;
        if (!/^[0-9a-f]{64}(?![\s\S])/.test(token)) throw new Rejection(401, 'invalid_worker_token');
        return await env.MINERS.getByName(`${env.ROUND}/${worker[2]}`).fetch(new Request('https://internal/worker', {
          headers: {Upgrade: 'websocket', 'x-witness-input': JSON.stringify({requestId: worker[3], token})},
        }));
      }
      const authStarted = performance.now();
      const input = await authenticate(request, env);
      exchangeId = input.requestId;
      timing(exchangeId, 'authenticate', authStarted);
      if (input.connect && (request.headers.get('Upgrade') || '').toLowerCase() !== 'websocket') {
        throw new Rejection(426, 'websocket_required');
      }
      const stub = env.MINERS.getByName(`${env.ROUND}/${input.hotkey}`);
      const grant = await stub.fetch(new Request('https://internal/miner', input.connect
        ? {headers: {Upgrade: 'websocket', 'x-witness-input': JSON.stringify(input)}}
        : {method: 'POST', body: JSON.stringify(input)}));
      if (!input.object || !grant.ok) return grant;
      await grant.body?.cancel();
      const prefix = `rounds/${env.ROUND}/${input.hotkey}/${input.requestId}`;
      if (input.object === 'status.json') {
        const completed = await completedStatus(env, input, prefix);
        if (completed) return completed;
      }
      const evidence = await env.TRANSCRIPTS.get(`${prefix}/${input.object}`);
      if (!evidence) throw new Rejection(404, 'object_not_ready');
      if (evidence.size > 16 * 1024 * 1024) {
        await evidence.body.cancel();
        throw new Rejection(413, 'object_too_large');
      }
      return new Response(evidence.body, {headers: {
        'content-type': input.object === 'status.json' ? 'application/json' : 'application/octet-stream',
        'cache-control': 'no-store', 'content-length': String(evidence.size),
      }});
    } catch (error) {outcome = 'error'; return failure(error);}
    finally {timing(exchangeId, 'request', started, outcome);}
  },
} satisfies ExportedHandler<GatewayEnv>;

export class MinerAdmission extends DurableObject<GatewayEnv> {
  async job(requestId: string): Promise<Job | undefined> {
    return this.ctx.storage.get<Job>(`job/${requestId}`);
  }
  async saveJob(requestId: string, job: Job): Promise<void> {
    await this.ctx.storage.put(`job/${requestId}`, job);
  }
  async fetch(request: Request): Promise<Response> {
    try {
      if (new URL(request.url).pathname === '/worker') {
        const worker: WorkerConnection = JSON.parse(request.headers.get('x-witness-input') || '');
        return await withStateLock(this.ctx, () => this.attachWorker(worker));
      }
      const raw = request.method === 'GET' ? request.headers.get('x-witness-input') : await request.text();
      const input: GatewayRequest = JSON.parse(raw || '');
      const token = crypto.randomUUID().replaceAll('-', '') + crypto.randomUUID().replaceAll('-', '');
      const digest = input.exchange ? await sha256(exchangePayload(input.exchange)) : '';
      const tokenHash = await sha256(token);
      const result = await measure(input.requestId, 'admission',
        () => withStateLock(this.ctx, () => this.reserve(input, digest, tokenHash)));
      if (result instanceof Response) {
        if (!input.exchange && !input.object && !input.connect && !input.release) return await this.refreshStatus(input);
        return result;
      }
      await this.dispatch(input, result, token);
      return this.response(input, (await this.job(input.requestId))!);
    } catch (error) {return failure(error);}
  }
  async refreshStatus(input: GatewayRequest): Promise<Response> {
    const job = (await this.job(input.requestId))!;
    if (job.runpodId && !job.claimed && ['submitting', 'queued', 'uncertain'].includes(job.state)) {
      const remote = await this.runpod('/status/' + encodeURIComponent(job.runpodId));
      if (typeof remote.status === 'string' && ['COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'].includes(remote.status)) {
        await withStateLock(this.ctx, () => this.retireUnclaimed(input.requestId));
      }
    }
    return this.response(input, (await this.job(input.requestId))!);
  }
  async retireUnclaimed(requestId: string): Promise<void> {
    const latest = (await this.job(requestId))!;
    if (!latest.claimed) await this.retire(requestId);
  }
  response(input: GatewayRequest, job: Job): Response {
    return json({round: this.env.ROUND, hotkey: input.hotkey, exchange_id: input.requestId,
      queue_state: job.state, deadline: job.deadline, session: job.session ?? null},
    ['submitting', 'uncertain'].includes(job.state) ? 202 : 200);
  }
  async reserve(input: GatewayRequest, digest: string, tokenHash: string): Promise<Response | Job> {
    const storage = this.ctx.storage, now = nowMs(), roundEnd = requireOpenRound(this.env);
    const nonceKey = `nonce/${input.nonce}`;
    for (const [key, expiry] of await storage.list<number>({prefix: 'nonce/'})) {
      if (expiry < now) await storage.delete(key);
    }
    if (await storage.get(nonceKey)) throw new Rejection(401, 'hotkey_auth_replayed');
    const rate = await storage.get<Rate>('rate') || {at: now, tokens: 20};
    const tokens = Math.min(20, rate.tokens + Math.max(0, now - rate.at) / 1000 * 2);
    if (tokens < 1) throw new Rejection(429, 'request_rate_limited');
    await storage.put({[nonceKey]: Number(BigInt(input.nonce) / 1_000_000n) + 10_001,
      rate: {at: now, tokens: tokens - 1}});
    const requestId = input.requestId, job = await this.job(requestId);
    if (!input.exchange) {
      if (!job) throw new Rejection(404, 'unknown_job');
      if (input.release) {await this.retire(requestId); return json({released: true});}
      if (input.connect) {
        if (job.state !== 'connected' || !job.session || job.minerAttached || now / 1000 >= job.deadline
          || await storage.get('current') !== requestId || !this.socket(requestId, 'worker')) {
          throw new Rejection(409, 'worker_not_available');
        }
        await this.saveJob(requestId, {...job, minerAttached: true});
        return this.accept(requestId, 'miner');
      }
      if (input.object) return json({allowed: true});
      return this.response(input, job);
    }
    if (job) {
      if (job.digest !== digest) throw new Rejection(409, 'exchange_id_conflict');
      return this.response(input, job);
    }
    const maximum = Number(this.env.MAX_JOBS_PER_MINER);
    if (!safeInteger(maximum) || maximum < 1) throw new Error('Invalid job limit');
    const count = await storage.get<number>('count') || 0;
    if (count >= maximum) throw new Rejection(429, 'job_limit_reached');
    const current = await storage.get<string>('current'), exchange = input.exchange;
    if (current && current !== exchange.replace) throw new Rejection(409, 'active_request_exists', current);
    if (exchange.replace && !await this.job(exchange.replace)) throw new Rejection(404, 'unknown_replacement');
    if (current) await this.retire(current);
    const reservation: Job = {digest, state: 'submitting', deadline: Math.min(roundEnd, exchange.deadline),
      tokenHash, claimed: false, minerAttached: false};
    // Persist the allowance charge and capability before external dispatch.
    await storage.put({[`job/${requestId}`]: reservation, count: count + 1, current: requestId});
    await storage.setAlarm(reservation.deadline * 1000);
    return reservation;
  }
  async dispatch(input: GatewayRequest, reservation: Job, token: string): Promise<void> {
    let runpodId: string | undefined;
    try {
      const lifetime = Math.max(1000, Math.min(604_800_000, reservation.deadline * 1000 - nowMs()));
      const remote = await measure(input.requestId, 'runpod_dispatch', () => this.runpod('/run', {input: {...input.exchange!, round: this.env.ROUND,
        expires: reservation.deadline, gateway: input.origin, token}, policy: {ttl: lifetime}}));
      if (typeof remote.id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}(?![\s\S])/.test(remote.id)) throw new Error('Invalid upstream ID');
      runpodId = remote.id;
    } catch {console.log(JSON.stringify({event: 'runpod_queue_uncertain', exchange_id: input.requestId}));}
    await measure(input.requestId, 'dispatch_persistence',
      () => withStateLock(this.ctx, () => this.saveDispatch(input.requestId, runpodId)));
  }
  async saveDispatch(requestId: string, runpodId: string | undefined): Promise<void> {
    const job = (await this.job(requestId))!;
    if (runpodId) job.runpodId = runpodId;
    // A claim or replacement may have completed while dispatch waited.
    if (job.state === 'submitting') job.state = runpodId ? 'queued' : 'uncertain';
    await this.saveJob(requestId, job);
  }
  async attachWorker(input: WorkerConnection): Promise<Response> {
    requireOpenRound(this.env);
    const requestId = input.requestId, job = await this.job(requestId);
    if (!job || job.tokenHash !== await sha256(input.token)) throw new Rejection(401, 'invalid_worker_token');
    if (job.claimed || job.state === 'retired' || nowMs() / 1000 >= job.deadline
      || await this.ctx.storage.get('current') !== requestId) throw new Rejection(409, 'worker_not_available');
    await this.saveJob(requestId, {...job, claimed: true, state: 'connected'});
    return this.accept(requestId, 'worker');
  }
  accept(requestId: string, role: Role): Response {
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server, [`${requestId}/${role}`]);
    server.serializeAttachment({requestId, role, bytes: 0, messages: 0} satisfies Connection);
    return new Response(null, {status: 101, webSocket: client});
  }
  socket(requestId: string, role: Role): WebSocket | undefined {
    return this.ctx.getWebSockets(`${requestId}/${role}`).find(socket => socket.readyState === 1);
  }
  async webSocketMessage(socket: WebSocket, message: string | ArrayBuffer): Promise<void> {
    const connection: Connection = socket.deserializeAttachment();
    try {await withStateLock(this.ctx, () => this.forwardMessage(socket, message, connection));}
    catch {await withStateLock(this.ctx, () => this.retire(connection.requestId));}
  }
  async forwardMessage(socket: WebSocket, message: string | ArrayBuffer, connection: Connection): Promise<void> {
    requireOpenRound(this.env);
    const requestId = connection.requestId, job = await this.job(requestId);
    if (!job || job.state !== 'connected' || nowMs() / 1000 >= job.deadline
      || await this.ctx.storage.get('current') !== requestId) throw new Error('Retired request');
    const length = typeof message === 'string' ? new TextEncoder().encode(message).byteLength : message.byteLength;
    connection.bytes += length;
    connection.messages++;
    if (length > (typeof message === 'string' ? 2 * 1024 * 1024 : 256 * 1024)
      || connection.bytes > 20 * 1024 * 1024 || connection.messages > 4096) throw new Error('Transport limit');
    socket.serializeAttachment(connection);
    if (typeof message === 'string') {
      const value: unknown = JSON.parse(message);
      if (!isRecord(value)) throw new Error('Invalid message');
      if (connection.role === 'worker' && value.type === 'ready') {
        const session = value.session;
        if (job.session || length > 4096 || !isRecord(session) || session.id !== requestId) throw new Error('Invalid session');
        await measure(requestId, 'worker_ready_persistence',
          () => this.saveJob(requestId, {...job, session: session as {[key: string]: Json}}));
        return;
      }
      if (connection.role === 'worker' && value.type === 'finished') {
        await this.saveJob(requestId, {...job, state: 'finished'});
        await this.ctx.storage.delete('current');
        return;
      }
      const allowed = connection.role === 'miner' ? ['handshake', 'authorize'] : ['status', 'end'];
      if (typeof value.type !== 'string' || !allowed.includes(value.type)) throw new Error('Invalid message');
    }
    const peer = this.socket(requestId, connection.role === 'miner' ? 'worker' : 'miner');
    if (!peer) throw new Error('Peer disconnected');
    peer.send(message);
  }
  async webSocketClose(socket: WebSocket, _code: number, _reason: string, _wasClean: boolean): Promise<void> {
    const connection: Connection = socket.deserializeAttachment();
    socket.close(1000, 'Exchange closed');
    await withStateLock(this.ctx, () => this.retire(connection.requestId));
  }
  async webSocketError(socket: WebSocket, _error: unknown): Promise<void> {
    await this.webSocketClose(socket, 1000, 'Exchange closed', false);
  }
  async retire(requestId: string): Promise<void> {
    const job = await this.job(requestId);
    if (job && job.state !== 'finished') await this.saveJob(requestId, {...job, state: 'retired'});
    if (await this.ctx.storage.get('current') === requestId) await this.ctx.storage.delete('current');
    for (const role of ['miner', 'worker'] as const) this.socket(requestId, role)?.close(1000, 'Exchange closed');
  }
  async alarm(): Promise<void> {
    await withStateLock(this.ctx, () => this.expireCurrent());
  }
  async expireCurrent(): Promise<void> {
    const current = await this.ctx.storage.get<string>('current');
    if (!current) return;
    const job = await this.job(current);
    if (job && nowMs() < job.deadline * 1000) await this.ctx.storage.setAlarm(job.deadline * 1000);
    else await this.retire(current);
  }
  async runpod(path: string, body?: unknown): Promise<Record<string, Json>> {
    if (!/^[a-zA-Z0-9_-]+(?![\s\S])/.test(this.env.RUNPOD_ENDPOINT) || !this.env.RUNPOD_API_KEY) throw new Error('Missing RunPod configuration');
    const response = await fetch(`https://api.runpod.ai/v2/${this.env.RUNPOD_ENDPOINT}${path}`, {
      method: body === undefined ? 'GET' : 'POST', redirect: 'manual', signal: AbortSignal.timeout(10_000),
      headers: {authorization: `Bearer ${this.env.RUNPOD_API_KEY}`, 'content-type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    if (!response.ok) {await response.body?.cancel(); throw new Error('RunPod unavailable');}
    const result: unknown = JSON.parse(await boundedText(response.body, 32768));
    if (!isRecord(result)) throw new Error('Invalid RunPod reply');
    return result as Record<string, Json>;
  }
}
