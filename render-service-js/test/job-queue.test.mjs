import assert from 'node:assert/strict';
import { JobQueue, QueueRejected } from '../src/job-queue.js';

const queue = new JobQueue({ capacity: 1, waitMs: 20 });
let release;
let active = 0;
let peak = 0;
const first = queue.run(async () => {
  peak = Math.max(peak, ++active);
  await new Promise(resolve => { release = resolve; });
  --active;
});
const expired = queue.run(() => assert.fail('expired job ran'));
await assert.rejects(queue.run(() => assert.fail('overflow ran')), QueueRejected);
await assert.rejects(expired, /queue_wait_timeout/);
release();
await first;
await assert.rejects(queue.run(() => { throw new Error('failed validation'); }), /failed validation/);
await Promise.all([1, 2].map(() => queue.run(async () => {
  peak = Math.max(peak, ++active);
  await new Promise(resolve => setTimeout(resolve, 2));
  --active;
})));
assert.equal(peak, 1);
console.log('Complete job serialization, overflow, expiry and failure recovery passed');
