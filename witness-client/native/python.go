//go:build cgo

package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"sync"
	"time"
	"unsafe"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/constraint"
	"github.com/consensys/gnark/logger"
)

// The process shares a bounded pool for the pinned parameter set.
var pythonProver struct {
	sync.Mutex
	directory string
	pool      *pythonProofPool
}

var pythonLoggerOnce sync.Once

type proofTimings struct {
	DurationMS       float64            `json:"duration_ms"`
	StepsMS          map[string]float64 `json:"steps_ms"`
	ParametersCached bool               `json:"parameters_cached"`
}

func newProofTimings() *proofTimings {
	return &proofTimings{StepsMS: make(map[string]float64)}
}

func (t *proofTimings) step(name string, started time.Time) {
	if t != nil {
		t.StepsMS[name] += float64(time.Since(started).Nanoseconds()) / 1e6
	}
}

// Caller holds pythonProver's mutex. Only authenticated bytes enter the cache.
func loadPythonProver(directory string, timings *proofTimings) error {
	if pythonProver.directory == directory && pythonProver.pool != nil {
		if timings != nil {
			timings.ParametersCached = true
		}
		return nil
	}
	configurePythonProverRuntime()
	pythonLoggerOnce.Do(logger.Disable)
	started := time.Now()
	_, verifyingError := verifyingParameter.read(directory)
	timings.step("parameter_read_verify", started)
	if err := verifyingError; err != nil {
		return err
	}
	started = time.Now()
	circuit, err := circuitParameter.read(directory)
	timings.step("parameter_read_verify", started)
	if err != nil {
		return err
	}
	started = time.Now()
	proving, err := provingParameter.read(directory)
	timings.step("parameter_read_verify", started)
	if err != nil {
		return err
	}
	if pythonProver.pool == nil {
		limit, err := pythonProverContextLimit()
		if err != nil {
			return err
		}
		first, err := newPythonProofContext(circuit, proving, timings)
		if err != nil {
			return err
		}
		pythonProver.pool = newPythonProofPool(first, circuit, proving, limit)
	}
	// All accepted directories contain the same pinned bytes, so clients with
	// different paths reuse this pool instead of multiplying its resource limit.
	pythonProver.directory = directory
	return nil
}

type pythonProofContext struct {
	ccs constraint.ConstraintSystem
	key groth16.ProvingKey
}

func newPythonProofContext(circuit, proving []byte, timings *proofTimings) (*pythonProofContext, error) {
	ccs, key := groth16.NewCS(ecc.BN254), groth16.NewProvingKey(ecc.BN254)
	started := time.Now()
	if _, err := ccs.ReadFrom(bytes.NewReader(circuit)); err != nil {
		return nil, errors.New("cannot load pinned circuit")
	}
	timings.step("circuit_deserialize", started)
	started = time.Now()
	if _, err := key.ReadFrom(bytes.NewReader(proving)); err != nil {
		return nil, errors.New("cannot load pinned proving key")
	}
	timings.step("proving_key_deserialize", started)
	return &pythonProofContext{ccs: ccs, key: key}, nil
}

func cachedPythonProver(directory string, timings *proofTimings) (*pythonProofPool, error) {
	wait := time.Now()
	pythonProver.Lock()
	timings.step("mutex_wait", wait)
	defer pythonProver.Unlock()
	if err := loadPythonProver(directory, timings); err != nil {
		return nil, err
	}
	return pythonProver.pool, nil
}

func pythonProof(directory string, raw []byte) ([]byte, error) {
	started := time.Now()
	timings := newProofTimings()
	var in Input
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(&in); err != nil {
		return nil, errors.New("invalid proof input")
	}
	if d.Decode(new(any)) != io.EOF {
		return nil, errors.New("trailing proof input")
	}
	pool, err := cachedPythonProver(directory, timings)
	if err != nil {
		return nil, err
	}
	wait := time.Now()
	context, err := pool.acquire()
	timings.step("mutex_wait", wait)
	if err != nil {
		return nil, err
	}
	defer pool.release(context)
	proofStarted := time.Now()
	bundle, err := prove(in, context.ccs, context.key)
	timings.step("proof_generate", proofStarted)
	if err != nil {
		return nil, err
	}
	timings.DurationMS = float64(time.Since(started).Nanoseconds()) / 1e6
	return json.Marshal(map[string]any{"bundle": bundle, "timings": timings})
}

//export WitnessProofABI
func WitnessProofABI() C.int { return 2 }

//export WitnessProofInit
func WitnessProofInit(directory *C.char) (result *C.char) {
	started := time.Now()
	timings := newProofTimings()
	defer func() {
		if recover() != nil {
			result = C.CString(`{"error":"proof initialization failed"}`)
		}
	}()
	if directory == nil {
		return C.CString(`{"error":"missing proof parameters directory"}`)
	}
	if _, err := cachedPythonProver(C.GoString(directory), timings); err != nil {
		out, _ := json.Marshal(map[string]string{"error": err.Error()})
		return C.CString(string(out))
	}
	timings.DurationMS = float64(time.Since(started).Nanoseconds()) / 1e6
	out, _ := json.Marshal(map[string]any{"proof_key_sha256": verifyingParameter.sha256, "timings": timings})
	return C.CString(string(out))
}

//export WitnessProof
func WitnessProof(directory *C.char, input *C.char, length C.int) (result *C.char) {
	// Never unwind a Go panic across Python's foreign-function boundary.
	defer func() {
		if recover() != nil {
			result = C.CString(`{"error":"proof generation failed"}`)
		}
	}()
	if directory == nil || input == nil || length < 1 || length > 128*1024 {
		return C.CString(`{"error":"invalid proof input size"}`)
	}
	raw := C.GoBytes(unsafe.Pointer(input), length)
	defer clear(raw)
	out, err := pythonProof(C.GoString(directory), raw)
	if err != nil {
		out, _ = json.Marshal(map[string]string{"error": err.Error()})
	}
	return C.CString(string(out))
}

//export WitnessProofFree
func WitnessProofFree(value *C.char) { C.free(unsafe.Pointer(value)) }
