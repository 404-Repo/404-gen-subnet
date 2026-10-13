// Local test entry point; never included in the deployed bundle.
import {MinerAdmission as Admission} from './worker.mjs';
export {default} from './worker.mjs';

export class MinerAdmission extends Admission {
  constructor(ctx, env) {super(ctx, env); this.instance = crypto.randomUUID();}
  async fetch(request) {
    if (request.url !== 'https://internal/test-state') return super.fetch(request);
    const connections = [];
    for (const role of ['miner', 'worker']) {
      for (const socket of this.ctx.getWebSockets(`${request.headers.get('x-test-id')}/${role}`)) {
        const attachment = socket.deserializeAttachment();
        if (request.method === 'POST' && role === 'miner') {
          attachment.messages = 4095;
          socket.serializeAttachment(attachment);
        }
        connections.push(attachment);
      }
    }
    return Response.json({instance: this.instance, connections});
  }
}
