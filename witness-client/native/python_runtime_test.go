//go:build cgo

package main

import (
	"bytes"
	"encoding/json"
	"runtime"
	"runtime/debug"
	"testing"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
)

func TestPythonProverResourceLimits(t *testing.T) {
	const mib = 1024 * 1024
	for _, tc := range []struct {
		name                      string
		initialCPUs, wantCPUs     int
		cpuEnv, memoryEnv         string
		initialMemory, wantMemory int64
	}{
		{"default", 8, 4, "", "", 1 << 62, 768 * mib},
		{"single_cpu", 1, 1, "", "", 1 << 62, 768 * mib},
		{"limited_runtime", 2, 2, "", "", 1 << 62, 768 * mib},
		{"explicit_cpu", 2, 2, "2", "", 1 << 62, 768 * mib},
		{"zero_cpu", 8, 4, "0", "", 1 << 62, 768 * mib},
		{"negative_cpu", 8, 4, "-1", "", 1 << 62, 768 * mib},
		{"invalid_cpu", 8, 4, "invalid", "", 1 << 62, 768 * mib},
		{"overflow_cpu", 8, 4, "2147483648", "", 1 << 62, 768 * mib},
		{"explicit_memory", 8, 4, "", "2GiB", 2048 * mib, 2048 * mib},
		{"explicit_both", 8, 8, "8", "2GiB", 2048 * mib, 2048 * mib},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("GOMAXPROCS", tc.cpuEnv)
			t.Setenv("GOMEMLIMIT", tc.memoryEnv)
			// Model the runtime's startup settings, including a smaller CPU
			// quota or explicit environment limits, before library initialization.
			previousCPUs := runtime.GOMAXPROCS(tc.initialCPUs)
			previousMemory := debug.SetMemoryLimit(tc.initialMemory)
			t.Cleanup(func() {
				runtime.GOMAXPROCS(previousCPUs)
				debug.SetMemoryLimit(previousMemory)
			})
			configurePythonProverRuntime()
			if got := runtime.GOMAXPROCS(0); got != tc.wantCPUs {
				t.Fatalf("GOMAXPROCS = %d, want %d", got, tc.wantCPUs)
			}
			if got := debug.SetMemoryLimit(-1); got != tc.wantMemory {
				t.Fatalf("Go memory limit = %d, want %d", got, tc.wantMemory)
			}
		})
	}
}

func TestPythonProverResourceLimitsOnCacheMiss(t *testing.T) {
	t.Setenv("GOMAXPROCS", "")
	t.Setenv("GOMEMLIMIT", "")
	previousCPUs := runtime.GOMAXPROCS(0)
	previousMemory := debug.SetMemoryLimit(-1)
	t.Cleanup(func() {
		runtime.GOMAXPROCS(previousCPUs)
		debug.SetMemoryLimit(previousMemory)
	})
	directory := copyParameters(t)
	pythonProver.Lock()
	defer pythonProver.Unlock()
	if err := loadPythonProver(directory, nil); err != nil {
		t.Fatal(err)
	}
	runtime.GOMAXPROCS(8)
	debug.SetMemoryLimit(2 << 30)
	if err := loadPythonProver(directory, nil); err != nil {
		t.Fatal(err)
	}
	if runtime.GOMAXPROCS(0) != 8 || debug.SetMemoryLimit(-1) != 2<<30 {
		t.Fatal("cached parameters reapplied runtime defaults")
	}
	if err := loadPythonProver(t.TempDir(), nil); err == nil {
		t.Fatal("missing parameters accepted")
	}
	if runtime.GOMAXPROCS(0) != 4 || debug.SetMemoryLimit(-1) != 768*1024*1024 {
		t.Fatal("cache miss did not apply runtime defaults before reading parameters")
	}
}

func TestPythonProverConcurrentProofs(t *testing.T) {
	vk := groth16.NewVerifyingKey(ecc.BN254)
	if err := readObject(publishedParameters+"/verifying.bin", vk); err != nil {
		t.Fatal(err)
	}
	inputs := []Input{
		makeInput(bytes.Repeat([]byte{'a'}, 64)),
		makeInput(bytes.Repeat([]byte{'b'}, 257)),
	}
	type result struct {
		index int
		raw   []byte
		err   error
	}
	results := make(chan result, len(inputs))
	start := make(chan struct{})
	for index, in := range inputs {
		go func() {
			<-start
			raw, _ := json.Marshal(in)
			out, err := pythonProof(publishedParameters, raw)
			results <- result{index, out, err}
		}()
	}
	close(start)
	completed := make([]result, len(inputs))
	for i := range completed {
		completed[i] = <-results
	}
	for _, out := range completed {
		if out.err != nil {
			t.Fatal(out.err)
		}
		var decoded struct{ Bundle *Bundle }
		if err := json.Unmarshal(out.raw, &decoded); err != nil {
			t.Fatal(err)
		}
		public := inputs[out.index]
		public.Key, public.Bundle = "", decoded.Bundle
		if err := verify(public, vk); err != nil {
			t.Fatalf("concurrent exchange %d: %v", out.index, err)
		}
		other := inputs[1-out.index]
		other.Key, other.Bundle = "", decoded.Bundle
		if verify(other, vk) == nil {
			t.Fatal("proof accepted for a different concurrent exchange")
		}
	}
}
