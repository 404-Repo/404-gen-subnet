/** Configure Debian Chromium's software renderer for the container's CPU budget. */
import { readFileSync, writeFileSync } from 'node:fs';
import { availableParallelism } from 'node:os';

let cpuLimit = availableParallelism();
try {
  const [quota, period] = readFileSync('/sys/fs/cgroup/cpu.max', 'utf8').trim().split(/\s+/);
  if (quota !== 'max') cpuLimit = Math.min(cpuLimit, Math.max(1, Math.floor(Number(quota) / Number(period))));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}

// SwiftShader reads this beside its shared library, independently of Chromium's cwd.
// Its default uses visible host CPUs, which can exceed a container's CPU quota.
writeFileSync('/usr/lib/chromium/SwiftShader.ini', `[Processor]\nThreadCount=${Math.min(16, cpuLimit)}\n`);
