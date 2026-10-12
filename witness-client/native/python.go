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
func loadPythonProver(directory string) error {
	if pythonProver.directory == directory && pythonProver.ccs != nil {
		return nil
	}
	runtime.GOMAXPROCS(2)
	debug.SetMemoryLimit(768 * 1024 * 1024)
	if _, err := verifyingParameter.read(directory); err != nil {
		return err
	}
	ccs, key := groth16.NewCS(ecc.BN254), groth16.NewProvingKey(ecc.BN254)
	circuit, err := circuitParameter.read(directory)
	if err != nil {
		return err
	}
	if _, err = ccs.ReadFrom(bytes.NewReader(circuit)); err != nil {
		return errors.New("cannot load pinned circuit")
	}
	proving, err := provingParameter.read(directory)
	if err != nil {
		return err
	}
	if _, err = key.ReadFrom(bytes.NewReader(proving)); err != nil {
		return errors.New("cannot load pinned proving key")
	}
	pythonProver.directory, pythonProver.ccs, pythonProver.key = directory, ccs, key
	return nil
}

func pythonProof(directory string, raw []byte) ([]byte, error) {
	var in Input
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(&in); err != nil {
		return nil, errors.New("invalid proof input")
	}
	if d.Decode(new(any)) != io.EOF {
		return nil, errors.New("trailing proof input")
	}
	pythonProver.Lock()
	defer pythonProver.Unlock()
	logger.Disable()
	if err := loadPythonProver(directory); err != nil {
		return nil, err
	}
	bundle, err := prove(in, pythonProver.ccs, pythonProver.key)
	if err != nil {
		return nil, err
	}
	return json.Marshal(map[string]any{"bundle": bundle})
}

//export WitnessProofABI
func WitnessProofABI() C.int { return 2 }

//export WitnessProofInit
func WitnessProofInit(directory *C.char) (result *C.char) {
	defer func() {
		if recover() != nil {
			result = C.CString(`{"error":"proof initialization failed"}`)
		}
	}()
	if directory == nil {
		return C.CString(`{"error":"missing proof parameters directory"}`)
	}
	pythonProver.Lock()
	defer pythonProver.Unlock()
	logger.Disable()
	if err := loadPythonProver(C.GoString(directory)); err != nil {
		out, _ := json.Marshal(map[string]string{"error": err.Error()})
		return C.CString(string(out))
	}
	out, _ := json.Marshal(map[string]string{"proof_key_sha256": verifyingParameter.sha256})
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
