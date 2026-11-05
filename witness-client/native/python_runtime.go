//go:build cgo

package main

import (
	"errors"
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
)

func pythonProverContextLimit() (int, error) {
	value := os.Getenv("WITNESS_PROVER_CONTEXTS")
	if value == "" {
		return 1, nil
	}
	n, err := strconv.Atoi(value)
	if err != nil || n < 1 || n > 8 {
		return 0, errors.New("WITNESS_PROVER_CONTEXTS must be an integer from 1 through 8")
	}
	return n, nil
}

// Keep headroom on larger hosts and respect the runtime's CPU quota.
// Explicit Go runtime environment settings are read at process startup.
func configurePythonProverRuntime() {
	if cpus, err := strconv.ParseInt(os.Getenv("GOMAXPROCS"), 10, 32); err != nil || cpus <= 0 {
		runtime.GOMAXPROCS(min(4, runtime.GOMAXPROCS(0)))
	}
	if os.Getenv("GOMEMLIMIT") == "" {
		debug.SetMemoryLimit(768 * 1024 * 1024)
	}
}
