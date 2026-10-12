// Forward just one ClientHello until handshake/probe disclosure is accepted.
// In particular, do not guess that a miner's first encrypted record is Finished.
export class ClientHelloGate {
  pending = Buffer.alloc(0);
  released = 0;

  receive(bytes) {
    if (this.released) return;
    this.pending = Buffer.concat([this.pending, bytes]);
    if (this.pending.length < 5) return;
    const size = 5 + this.pending.readUInt16BE(3);
    if (this.pending[0] !== 22 || size < 9 || size > 16645) throw new Error('Expected ClientHello');
    if (this.pending.length < size) return;
    const record = this.pending.subarray(0, size);
    if (record[5] !== 1 || record.readUIntBE(6, 3) !== size - 9) throw new Error('Expected one ClientHello');
    this.released = size;
    this.pending = Buffer.alloc(0);
    return record;
  }
}

// Usage comes only from the captured provider response, never a miner report.
// Failed/incomplete generations can still spend tokens. Missing usage is unknown.
export function responseUsage(body) {
  const terminal = body.toString('utf8').split(/\r?\n\r?\n/).flatMap(event => {
    const data = event.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
    try {
      const value = JSON.parse(data);
      return ['response.completed', 'response.failed', 'response.incomplete'].includes(value.type) ? [value] : [];
    }
    catch { return []; }
  });
  if (terminal.length !== 1 || terminal[0].type !== `response.${terminal[0].response?.status}`) return null;
  const usage = terminal[0].response.usage;
  if (!['input_tokens', 'output_tokens', 'total_tokens'].every(key => Number.isSafeInteger(usage?.[key]) && usage[key] >= 0)) return null;
  if (usage.input_tokens + usage.output_tokens !== usage.total_tokens) return null;
  return { input_tokens: usage.input_tokens, output_tokens: usage.output_tokens, total_tokens: usage.total_tokens };
}

// Only a complete, authenticated provider HTTP response can reach this check.
// Invalid requests/resources, auth/access failures and rate limits are rejections,
// not reported zero usage. Timeouts, other errors and lost replies remain unknown.
export function providerRejected(status) {
  return [400, 401, 403, 404, 429].includes(status);
}
