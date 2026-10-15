# Native credential prover benchmark

`prover.py` builds the native C ABI from an explicit checkout and generates
synthetic, valid TLS credential records locally. It never calls a provider or
gateway. Use the client's Python environment (including `cryptography`), a native
Go 1.27.1 toolchain, a C compiler, and the published pinned parameters. Go modules
must already be cached: builds use `GOTOOLCHAIN=local` and `GOPROXY=off`.

From the repository root, with `go` on `PATH`:

```sh
python witness-client/benchmarks/prover.py \
  --source "$PWD" --output /private/tmp/witness-prover-matrix \
  --rounds 2 --repeats 2
```

The output directory must be new. Keep raw results and generated builds outside
the checkout. The default matrix is 2/4/8 CPUs and 768/2048/4096 MiB Go soft memory
limits, with 164-byte API-sized and 1,522-byte subscription-sized credentials.
These produce four and nine component proofs, respectively. Only request settings
the host can safely support; the benchmark rejects CPU counts above the host's
logical CPU count. Cases run sequentially at reduced priority (`nice +10`), in a
deterministically shuffled order each round. Other local activity still affects
measurements; retain the load and CPU data and repeat shortlisted settings.

Each case starts a new process, times library loading and cold parameter
initialization separately, then times repeated proofs with the authenticated
parameters cached. “Cold” means a fresh process, with a warm filesystem cache;
the script does not purge OS caches. Each proof uses fresh random TLS keys and
nonces and the normal cryptographic prover randomness. Every result is passed
to a separately built, **unchanged** native verifier. No keys, credentials, TLS
records, or proof bundles are written to result files.

`runtime_metrics.go` is benchmark-only code. The script copies production sources
to the output directory, adds a runtime metrics ABI, and inserts one resource
override call immediately before parameter reads in its temporary `python.go`.
This applies the selected settings to both initialization and proving without
changing the production checkout, circuit, proofs, parameter pins, or verifier.
The bridge is never part of a normal client build. Use `--settings default` to
keep production resource settings. A control against an independently rebuilt,
uninstrumented C library is also supported:

```sh
go -C witness-client/native build -trimpath -buildmode=c-shared \
  -o /private/tmp/witness-control.dylib .
python witness-client/benchmarks/prover.py \
  --source "$PWD" --output /private/tmp/witness-prover-control \
  --library /private/tmp/witness-control.dylib --settings default
```

Use `.so` on Linux. Verify the control's provenance using `go version -m` and its
SHA-256; the script cannot infer an external library's source identity. Compare
the instrumented default with this control before interpreting the matrix.

`manifest.json` records the source commit, per-file source and temporary build
hashes, library/verifier SHA-256, actual Go build metadata, pinned parameter
hashes, hardware, toolchain, runtime environment, and workload. `samples.jsonl`
records wall time, process user/system CPU, peak process RSS, load averages,
native stage timers, Go heap/runtime memory, allocation totals, GC cycles and
pauses, GC CPU, and mark-assist CPU. RSS is the process high-water mark, including
Python and initialization; it is not the Go memory limit or an isolated peak for
each proof. Runtime metric snapshots are outside timed calls. `summary.json`
reports medians/ranges and separates the first proof after initialization from
later repeats. All warm proof timings include the C ABI and JSON conversion.

For credential boundaries, run `--lengths 64 255 256 257 4096` with a shortlisted
setting. Existing native tests cover rejected lengths, token syntax, tampering,
parameter integrity, bounded decoding, and public-input binding. Run the native
test suite and appropriate race checks separately from timing runs. These local
measurements do not measure end-to-end witness latency.
