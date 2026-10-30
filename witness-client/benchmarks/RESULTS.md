# Native prover resource tuning — 2026-10-15

Measurements use a native Apple M2 Max (12 cores, 64 GiB), macOS 26.6.2,
Go 1.27.1 `darwin/arm64`, and Python 3.13.0. All records are synthetic. The
published circuit and proving/verifying parameters, cryptographic randomness,
component proof order, and verifier are unchanged.

Each case starts a fresh Python process at `nice +10`, with a warm filesystem
cache. Parameter hashing/deserialization is measured separately from repeated
proofs using the authenticated in-process cache. Warm timings include the native
C ABI and JSON conversion. Peak RSS includes Python, the Go runtime, and cold
initialization; the Go memory setting is a soft limit on Go-managed memory.

Background activity on the host varied substantially. All samples, including
contention outliers, remain in the medians and ranges. These are local prover
measurements; end-to-end witness latency was not remeasured.

## Matched production libraries

This comparison uses independently rebuilt, **uninstrumented** C ABI libraries:
baseline `1661ed6554af498833b8e2ac60bac11f7f1feede` and optimized
`83d650662cd3aa9a65cdafa894305e0994b37a6d`. Both libraries report their respective
clean Git revision, Go 1.27.1, and `GOARCH=arm64` in `go version -m`. Build flags
are `-trimpath -buildmode=c-shared`. Both use a 768 MiB Go soft limit; the CPU
setting changes from 2 to the new default of 4 on this host.

For each workload and version, three fresh processes each produced three warm
bundles. Workloads and adjacent before/after pairs were shuffled each round with
seed `404 + round`. No other benchmark or test from this run overlapped these
measurements. All 36 bundles (234 component proofs) passed the **same baseline
verifier binary**. Warm medians use nine samples per cell; cold initialization
uses six samples per version. RSS is the maximum process high-water mark for
the corresponding condition, rounded to the nearest MiB.

| Condition | Before, 2 CPUs | After, 4 CPUs | Peak RSS, before → after (MiB) |
| --- | --- | --- | --- |
| Cold parameter initialization | 6.84 s | 3.58 s | 196 → 195 |
| Warm, 164 bytes / 4 components | 5.39 s | 2.90 s | 480 → 476 |
| Warm, 1,522 bytes / 9 components | 12.36 s | 6.49 s | 476 → 495 |

Warm proof wall time fell **46.2%** for the short credential and **47.5%** for
the long credential. Process CPU work stayed similar: median user+system time
was 10.62 → 10.77 CPU seconds for short credentials and 24.10 → 24.12 for long
credentials. Later warm proofs, excluding the first after initialization, had
medians of 5.46 → 2.91 s and 12.33 → 6.48 s, respectively. The latency gain comes
from allowing the existing prover to use more CPUs, without changing its work
or the proof protocol. Long-credential peak RSS increased by about 19 MiB.

Artifact SHA-256 values:

- Baseline: `a0ad363b87ea1b9a89555f8c1f0573ca589f36da6da2098af8351447d324233f`.
- Optimized: `e96b9f404822cc7dc469a3a38c7ba4180bd46ff4ae7fc4abecf9cf1ecbf3efa7`.
- Unchanged verifier: `6f6e5502b687fc1cdf54b9b792d5a841b07070537425811b305b2ed4232ec17a`.

## Resource matrix

The matrix uses two shuffled rounds per setting and workload, with two warm
proofs per process: 72 verified bundles containing 468 component proofs. The
164-byte credential produces four components; 1,522 bytes produces nine.
Times below are warm medians of four samples per cell. RSS is the maximum
process high-water mark across both workloads and rounds at that setting.

| Go CPUs | Go limit (MiB) | 164 bytes (s) | 1,522 bytes (s) | Peak RSS (MiB) |
| --- | --- | --- | --- | --- |
| 2 | 768 | 8.46 | 14.51 | 509 |
| 2 | 2,048 | 8.27 | 16.36 | 510 |
| 2 | 4,096 | 6.32 | 13.15 | 488 |
| 4 | 768 | 3.30 | 9.16 | 514 |
| 4 | 2,048 | 3.63 | 7.35 | 509 |
| 4 | 4,096 | 3.99 | 7.64 | 472 |
| 8 | 768 | 2.48 | 4.91 | 463 |
| 8 | 2,048 | 2.69 | 4.90 | 462 |
| 8 | 4,096 | 2.67 | 5.40 | 490 |

GC CPU cost was small: the median share at every setting was below 0.3% of
process CPU time. Go-managed memory observed after calls stayed below 475 MiB.
Higher memory limits therefore did not address a measured GC or memory-pressure
bottleneck. For example, two long proofs at 4 CPUs/768 MiB took 27.66 and 7.78 s
while using 28.00 and 25.79 CPU seconds; their observed Go memory was 353 and
363 MiB. This illustrates the effect of competing host work on wall time.

The selected default is **at most four Go CPUs and 768 MiB**, respecting a
smaller initial runtime CPU limit. Four CPUs improve latency while preserving
headroom on larger hosts. Eight CPUs are an explicit option for hosts with
available CPU capacity. `GOMAXPROCS` and `GOMEMLIMIT`, set before starting Python,
retain the standard Go runtime settings instead of the library defaults.

## Reproduction

See [README.md](README.md) for prerequisites, provenance checks, runtime metrics,
and the control-library workflow. The matrix used source commit `f63778b`, whose
production native sources match baseline `1661ed6`, with `--rounds 2 --repeats 2`
and the default matrix/workloads. Benchmark-only runtime instrumentation is
added to a temporary source copy; it is absent from normal client libraries.
To reproduce an interleaved resource comparison, use
`--settings 2:768,4:768 --rounds 3 --repeats 3`. To compare uninstrumented
artifacts, use `--library` and `--settings default` with separately rebuilt
libraries; the matched comparison above alternated those worker processes.

An independently rebuilt, uninstrumented baseline at `1661ed6` had warm medians
of 6.30 s (164 bytes) and 14.57 s (1,522 bytes), over six samples each. The
instrumented 2-CPU smoke sample was 6.16 s for 164 bytes, within the control's
6.01–7.02 s range. The matched production-library comparison is reported below.

## Verification

- The full native Go suite passed, including credential syntax and lengths,
  traffic/proof tampering, exchange binding, modified proving-key rejection,
  authenticated parameter caching, and bounded proof decoding.
- An 8-CPU race run passed the cached-prover, resource-policy, bounded-decoder,
  and concurrent-caller checks. The race run used a 2 GiB Go soft limit for
  instrumentation overhead; its timings are excluded from performance results.
- All 275 Python client tests and all 22 local TLS protocol fixtures passed with
  `cryptography 50.0.2`. TLS fixtures cover both authentication modes, provider
  failures, and miner disconnection, using synthetic credentials and local
  servers. These compatibility tests used 2 CPUs/768 MiB.
- The clean optimized library produced and verified credentials of 64, 255,
  256, 257, and 4,096 bytes with the same baseline verifier: five bundles and
  38 components (4, 4, 5, 5, and 20). The native boundary tests also reject
  63/4,097-byte credentials and forbidden token bytes.

The resource policy tests preserve explicit startup settings and smaller initial
runtime CPU limits. Concurrent calls still serialize through the existing mutex;
each resulting bundle verifies for its own exchange and is rejected for the
other exchange. No component-proof parallelism or proof reuse was introduced.
