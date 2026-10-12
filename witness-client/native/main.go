package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
	"github.com/consensys/gnark/logger"
)

func writeObject(path string, v io.WriterTo) error {
	f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if e != nil {
		return e
	}
	_, e = v.WriteTo(f)
	ce := f.Close()
	if e != nil {
		return e
	}
	return ce
}
func readObject(path string, v io.ReaderFrom) error {
	f, e := os.Open(path)
	if e != nil {
		return e
	}
	defer f.Close()
	_, e = v.ReadFrom(f)
	if e != nil {
		return e
	}
	var extra [1]byte
	n, _ := f.Read(extra[:])
	if n != 0 {
		return errors.New("trailing setup bytes")
	}
	return nil
}
func fileHash(path string) (string, error) {
	f, e := os.Open(path)
	if e != nil {
		return "", e
	}
	defer f.Close()
	h := sha256.New()
	if _, e = io.Copy(h, f); e != nil {
		return "", e
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func setup(directory string) error {
	if e := os.Mkdir(directory, 0700); e != nil {
		return e
	}
	start := time.Now()
	ccs, e := frontend.Compile(ecc.BN254.ScalarField(), r1cs.NewBuilder, &CredentialCircuit{})
	if e != nil {
		return e
	}
	pk, vk, e := groth16.Setup(ccs)
	if e != nil {
		return e
	}
	for name, obj := range map[string]io.WriterTo{"circuit.bin": ccs, "proving.bin": pk, "verifying.bin": vk} {
		if e = writeObject(filepath.Join(directory, name), obj); e != nil {
			return e
		}
	}
	digest, e := fileHash(filepath.Join(directory, "verifying.bin"))
	if e != nil {
		return e
	}
	manifest := map[string]any{"version": 1, "scheme": "groth16-bn254-chacha20-mimc-token-v1", "max_token": MaxToken, "chunk_size": ChunkSize, "constraints": ccs.GetNbConstraints(), "verifying_key_sha256": digest, "setup_seconds": time.Since(start).Seconds()}
	data, _ := json.MarshalIndent(manifest, "", "  ")
	if e = os.WriteFile(filepath.Join(directory, "manifest.json"), append(data, '\n'), 0600); e != nil {
		return e
	}
	fmt.Println(string(data))
	return nil
}

func run() error {
	logger.Disable()
	runtime.GOMAXPROCS(2)
	if len(os.Args) != 3 {
		return errors.New("usage: privateauth setup|prove|verify|certificate DIRECTORY (JSON on stdin)")
	}
	mode, dir := os.Args[1], os.Args[2]
	if mode == "setup" {
		return setup(dir)
	}
	if mode != "prove" && mode != "verify" && mode != "certificate" {
		return errors.New("invalid mode")
	}
	limit := 128 * 1024
	if mode == "certificate" {
		limit = 1024 * 1024 // Includes the local runtime's public trust-root bundle.
	}
	raw, e := io.ReadAll(io.LimitReader(os.Stdin, int64(limit+1)))
	if e != nil || len(raw) > limit {
		return errors.New("input too large")
	}
	var in Input
	var certInput CertificateInput
	var input any = &in
	if mode == "certificate" {
		input = &certInput
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if e = decoder.Decode(input); e != nil {
		return errors.New("invalid input JSON")
	}
	if decoder.Decode(new(any)) != io.EOF {
		return errors.New("trailing input")
	}
	start := time.Now()
	if mode == "certificate" {
		if e = verifyCertificates(certInput); e != nil {
			return e
		}
		return json.NewEncoder(os.Stdout).Encode(map[string]any{"verified": true})
	}
	if mode == "verify" {
		vk := groth16.NewVerifyingKey(ecc.BN254)
		if e = readObject(filepath.Join(dir, "verifying.bin"), vk); e != nil {
			return e
		}
		if e = verify(in, vk); e != nil {
			return e
		}
		return json.NewEncoder(os.Stdout).Encode(map[string]any{"verified": true, "seconds": time.Since(start).Seconds()})
	}
	ccs := groth16.NewCS(ecc.BN254)
	if e = readObject(filepath.Join(dir, "circuit.bin"), ccs); e != nil {
		return e
	}
	pk := groth16.NewProvingKey(ecc.BN254)
	if e = readObject(filepath.Join(dir, "proving.bin"), pk); e != nil {
		return e
	}
	b, e := prove(in, ccs, pk)
	if e != nil {
		return e
	}
	return json.NewEncoder(os.Stdout).Encode(b)
}

func main() {
	if e := run(); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
