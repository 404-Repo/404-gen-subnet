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
	"runtime"
	"runtime/debug"
	"sync"
	"time"
	"unsafe"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/constraint"
	"github.com/consensys/gnark/logger"
)

// In-process C ABI for the Python client. Serialize proofs to bound RAM.
var pythonProver struct {
	sync.Mutex
	directory string
	ccs       constraint.ConstraintSystem
	key       groth16.ProvingKey
}

// Caller holds pythonProver's mutex. Only a complete, pinned pair enters the
// cache; subsequent proofs use those objects even if disk contents change.
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

func loadPythonProver(directory string, timings *proofTimings) error {
	if pythonProver.directory == directory && pythonProver.ccs != nil {
		if timings != nil {
			timings.ParametersCached = true
		}
		return nil
	}
	runtime.GOMAXPROCS(2)
	debug.SetMemoryLimit(768 * 1024 * 1024)
	started := time.Now()
	_, verifyingError := verifyingParameter.read(directory)
	timings.step("parameter_read_verify", started)
	if err := verifyingError; err != nil {
		return err
	}
	ccs, key := groth16.NewCS(ecc.BN254), groth16.NewProvingKey(ecc.BN254)
	started = time.Now()
	circuit, err := circuitParameter.read(directory)
	timings.step("parameter_read_verify", started)
	if err != nil {
		return err
	}
	started = time.Now()
	_, err = ccs.ReadFrom(bytes.NewReader(circuit))
	timings.step("circuit_deserialize", started)
	if err != nil {
		return errors.New("cannot load pinned circuit")
	}
	started = time.Now()
	proving, err := provingParameter.read(directory)
	timings.step("parameter_read_verify", started)
	if err != nil {
		return err
	}
	started = time.Now()
	_, err = key.ReadFrom(bytes.NewReader(proving))
	timings.step("proving_key_deserialize", started)
	if err != nil {
		return errors.New("cannot load pinned proving key")
	}
	pythonProver.directory, pythonProver.ccs, pythonProver.key = directory, ccs, key
	return nil
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
	wait := time.Now()
	pythonProver.Lock()
	timings.step("mutex_wait", wait)
	defer pythonProver.Unlock()
	logger.Disable()
	if err := loadPythonProver(directory, timings); err != nil {
		return nil, err
	}
	proofStarted := time.Now()
	bundle, err := prove(in, pythonProver.ccs, pythonProver.key)
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
	wait := time.Now()
	pythonProver.Lock()
	timings.step("mutex_wait", wait)
	defer pythonProver.Unlock()
	logger.Disable()
	if err := loadPythonProver(C.GoString(directory), timings); err != nil {
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
