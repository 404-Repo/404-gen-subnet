//go:build cgo

package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	groth16bn254 "github.com/consensys/gnark/backend/groth16/bn254"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
)

const publishedParameters = "../../witness-server/verifier/params"

func copyParameters(t *testing.T) string {
	t.Helper()
	directory := t.TempDir()
	for _, name := range []string{"circuit.bin", "proving.bin", "verifying.bin"} {
		data, err := os.ReadFile(filepath.Join(publishedParameters, name))
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(filepath.Join(directory, name), data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	return directory
}

type unrelatedCircuit struct {
	X frontend.Variable
	Y frontend.Variable `gnark:",public"`
}

func (c *unrelatedCircuit) Define(api frontend.API) error {
	api.AssertIsEqual(api.Mul(c.X, c.X), c.Y)
	return nil
}

func TestPythonProverRejectsModifiedKeys(t *testing.T) {
	for _, change := range []string{"zero_delta", "unrelated_circuit"} {
		t.Run(change, func(t *testing.T) {
			directory := copyParameters(t)
			var key groth16.ProvingKey
			if change == "zero_delta" {
				original := &groth16bn254.ProvingKey{}
				if err := readObject(filepath.Join(directory, "proving.bin"), original); err != nil {
					t.Fatal(err)
				}
				original.G1.Delta.SetInfinity()
				original.G2.Delta.SetInfinity()
				key = original
			} else {
				ccs, err := frontend.Compile(ecc.BN254.ScalarField(), r1cs.NewBuilder, &unrelatedCircuit{})
				if err != nil {
					t.Fatal(err)
				}
				key, _, err = groth16.Setup(ccs)
				if err != nil {
					t.Fatal(err)
				}
			}
			var encoded bytes.Buffer
			if _, err := key.WriteTo(&encoded); err != nil {
				t.Fatal(err)
			}
			// These are parseable gnark keys, not merely damaged file headers.
			if _, err := groth16.NewProvingKey(ecc.BN254).ReadFrom(bytes.NewReader(encoded.Bytes())); err != nil {
				t.Fatal(err)
			}
			if change == "zero_delta" && int64(encoded.Len()) != provingParameter.size {
				t.Fatal("test must exercise the hash check, not the size check")
			}
			if err := os.WriteFile(filepath.Join(directory, "proving.bin"), encoded.Bytes(), 0600); err != nil {
				t.Fatal(err)
			}
			raw, _ := json.Marshal(makeInput(bytes.Repeat([]byte{'a'}, 64)))
			// Bypass Python initialization: the proof entry point must also reject
			// this before gnark launches any prover goroutines.
			_, err := pythonProof(directory, raw)
			if err == nil || !strings.Contains(err.Error(), "Invalid proof parameters: proving.bin") {
				t.Fatalf("unexpected result: %v", err)
			}
			if pythonProver.directory == directory {
				t.Fatal("untrusted parameters entered the cache")
			}
		})
	}
}

func TestPythonProverUsesAuthenticatedSnapshot(t *testing.T) {
	directory := copyParameters(t)
	pythonProver.Lock()
	err := loadPythonProver(directory)
	pythonProver.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	// A replacement after initialization cannot reach the prover. It uses the
	// authenticated in-memory objects, including when disk files disappear.
	for _, name := range []string{"circuit.bin", "proving.bin", "verifying.bin"} {
		if err := os.Remove(filepath.Join(directory, name)); err != nil {
			t.Fatal(err)
		}
	}
	in := makeInput(bytes.Repeat([]byte{'a'}, 64))
	raw, _ := json.Marshal(in)
	result, err := pythonProof(directory, raw)
	if err != nil {
		t.Fatal(err)
	}
	var out struct{ Bundle *Bundle }
	if err = json.Unmarshal(result, &out); err != nil {
		t.Fatal(err)
	}
	in.Key, in.Bundle = "", out.Bundle
	vk := groth16.NewVerifyingKey(ecc.BN254)
	if err = readObject(filepath.Join(publishedParameters, "verifying.bin"), vk); err != nil {
		t.Fatal(err)
	}
	if err = verify(in, vk); err != nil {
		t.Fatal(err)
	}
}
