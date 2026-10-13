/** Public protocol and persisted records shared with the Python client/worker. */
export type Json = null | boolean | number | string | Json[] | {[key: string]: Json};
export type Role = 'miner' | 'worker';
export type JobState = 'submitting' | 'queued' | 'uncertain' | 'connected' | 'finished' | 'retired';
export type GatewayEnv = GatewayBindings;

export interface Exchange {
  operation: 'exchange';
  hotkey: string;
  receiver: string;
  exchange_id: string;
  deadline: number;
  provider: 'api' | 'codex';
  signature: string;
  replace?: string;
}

export interface GatewayRequest {
  hotkey: string;
  nonce: string;
  requestId: string;
  exchange?: Exchange;
  object?: 'status.json' | 'response.sse';
  connect?: boolean;
  release?: boolean;
  origin?: string;
}

export interface Job {
  digest: string;
  state: JobState;
  deadline: number;
  tokenHash: string;
  claimed: boolean;
  minerAttached: boolean;
  runpodId?: string;
  session?: {[key: string]: Json};
}

export interface Connection {
  requestId: string;
  role: Role;
  bytes: number;
  messages: number;
}

export interface WorkerConnection {requestId: string; token: string}
export interface Rate {at: number; tokens: number}
export interface Membership {
  version: 1;
  network: 'finney';
  netuid: number;
  block: number;
  block_hash: string;
  block_time: number;
  fetched_at: number;
  hotkeys: string[];
}

export class Rejection extends Error {
  constructor(readonly status: number, message: string, readonly exchangeId?: string) {
    super(message);
  }
}
