// Benchmark-only support: copied into a temporary build, never shipped in the client.
package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"encoding/json"
	"os"
	"runtime"
	"runtime/debug"
	"runtime/metrics"
	"strconv"
)

// Called after the production defaults, before reading any parameters. The
// benchmark adds this call to its temporary copy of python.go only.
func benchmarkConfigure() {
	if value := os.Getenv("WITNESS_BENCH_CPUS"); value != "" {
		n, err := strconv.Atoi(value)
		if err != nil || n < 1 || n > runtime.NumCPU() {
			panic("invalid benchmark CPU count")
		}
		runtime.GOMAXPROCS(n)
	}
	if value := os.Getenv("WITNESS_BENCH_MEMORY"); value != "" {
		n, err := strconv.ParseInt(value, 10, 64)
		if err != nil || n < 1 {
			panic("invalid benchmark memory limit")
		}
		debug.SetMemoryLimit(n)
	}
}

//export WitnessBenchmarkSnapshot
func WitnessBenchmarkSnapshot() *C.char {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	names := []string{
		"/cpu/classes/gc/total:cpu-seconds",
		"/cpu/classes/gc/mark/assist:cpu-seconds",
		"/cpu/classes/user:cpu-seconds",
	}
	samples := make([]metrics.Sample, len(names))
	for i, name := range names {
		samples[i].Name = name
	}
	metrics.Read(samples)
	values := make(map[string]float64)
	for _, s := range samples {
		values[s.Name] = s.Value.Float64()
	}
	data, _ := json.Marshal(map[string]any{
		"gomaxprocs":         runtime.GOMAXPROCS(0),
		"memory_limit_bytes": debug.SetMemoryLimit(-1),
		"go_version":         runtime.Version(), "goarch": runtime.GOARCH,
		"heap_alloc_bytes": m.HeapAlloc, "heap_inuse_bytes": m.HeapInuse,
		"go_memory_bytes":   m.Sys - m.HeapReleased,
		"total_alloc_bytes": m.TotalAlloc, "gc_cycles": m.NumGC,
		"gc_pause_ns": m.PauseTotalNs, "cpu_seconds": values,
	})
	return C.CString(string(data))
}
