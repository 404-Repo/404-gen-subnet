/** FIFO admission for the complete validation and render job. */
export class QueueRejected extends Error {
  constructor(reason) {
    super(reason);
    this.reason = reason;
  }
}

export class JobQueue {
  constructor({ capacity = 8, waitMs = 120000 } = {}) {
    this.capacity = capacity;
    this.waitMs = waitMs;
    this.active = false;
    this.waiting = [];
  }

  async run(job) {
    const arrived = performance.now();
    if (this.active) {
      if (this.waiting.length >= this.capacity) throw new QueueRejected('queue_full');
      await new Promise((resolve, reject) => {
        const entry = { resolve, timer: null };
        entry.timer = setTimeout(() => {
          const index = this.waiting.indexOf(entry);
          if (index >= 0) this.waiting.splice(index, 1);
          reject(new QueueRejected('queue_wait_timeout'));
        }, this.waitMs);
        this.waiting.push(entry);
      });
    } else {
      this.active = true;
    }
    const started = performance.now();
    try {
      return await job(started - arrived);
    } finally {
      console.log(JSON.stringify({ event: 'render_job', wait_ms: started - arrived,
        job_ms: performance.now() - started, queued: this.waiting.length }));
      const next = this.waiting.shift();
      if (next) {
        clearTimeout(next.timer);
        next.resolve();
      } else {
        this.active = false;
      }
    }
  }
}
