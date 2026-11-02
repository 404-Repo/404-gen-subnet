package main

import (
	"crypto/sha256"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// These are the members of the published v1 archive with SHA-256
// c5e2264bdf3fefd50ec20d3384c388e8dc709a0e7968ee840f32fbf1386fc244.
// The circuit and proving key are part of the credential secrecy boundary.
type pinnedParameter struct {
	name   string
	size   int64
	sha256 string
}

var circuitParameter = pinnedParameter{"circuit.bin", 10378176, "451b8ebe434e1b9cee5982a46f4630d3d3647fe49c11f2ec6d88b8731617f9c7"}
var provingParameter = pinnedParameter{"proving.bin", 35392269, "31bd198d1325f526f5f1add2272bf73ffaff23c9e2d0ac7463c0f5e88c8701e7"}
var verifyingParameter = pinnedParameter{"verifying.bin", 12400, "6478059e4ad0d39e8c157c76cea9c6dd777c2ab5143cf8694876094facf39b16"}

func (p pinnedParameter) read(directory string) ([]byte, error) {
	fail := func(reason string) ([]byte, error) {
		return nil, fmt.Errorf("Invalid proof parameters: %s (%s); install the published v1 set", p.name, reason)
	}
	f, err := os.Open(filepath.Join(directory, p.name))
	if err != nil {
		return fail("cannot open file")
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() != p.size {
		return fail("unexpected file size or type")
	}
	data, err := io.ReadAll(io.LimitReader(f, p.size+1))
	if err != nil || int64(len(data)) != p.size {
		return fail("incomplete or oversized file")
	}
	if fmt.Sprintf("%x", sha256.Sum256(data)) != p.sha256 {
		return fail("SHA-256 mismatch")
	}
	// Deserialize these authenticated bytes, never reopen the path after hashing.
	return data, nil
}
