//go:build cgo

package main

import (
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
)

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
