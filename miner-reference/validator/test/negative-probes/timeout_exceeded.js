// @expectedRule TIMEOUT_EXCEEDED
// Infinite loop — the worker's terminate() preempts at the 5-second wall-clock
// cap, regardless of hardware speed. Deterministic across all CPUs.
export default function generate(THREE) {
  let i = 0;
  while (true) i++;
}
