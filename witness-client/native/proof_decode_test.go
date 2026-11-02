package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"runtime"
	"testing"

	curve "github.com/consensys/gnark-crypto/ecc/bn254"
	groth16bn254 "github.com/consensys/gnark/backend/groth16/bn254"
)

func TestBoundedProofDecoder(t *testing.T) {
	// Infinity points suffice for decoding tests; real cryptographic verification
	// remains covered by TestProofRoundTrip using the actual credential circuit.
	p := &groth16bn254.Proof{Commitments: make([]curve.G1Affine, 1)}
	for _, uncompressed := range []bool{false, true} {
		t.Run(fmt.Sprintf("uncompressed=%v", uncompressed), func(t *testing.T) {
			var buf bytes.Buffer
			var err error
			pointBytes := 2*curve.SizeOfG1AffineCompressed + curve.SizeOfG2AffineCompressed
			if uncompressed {
				_, err = p.WriteRawTo(&buf)
				pointBytes *= 2
			} else {
				_, err = p.WriteTo(&buf)
			}
			if err != nil {
				t.Fatal(err)
			}
			raw := buf.Bytes()
			if _, err = decodeProof(raw, 1); err != nil {
				t.Fatal(err)
			}
			for _, count := range []uint32{0, 2, 1024, 1 << 20, 0xffffffff} {
				bad := bytes.Clone(raw)
				binary.BigEndian.PutUint32(bad[pointBytes:pointBytes+4], count)
				var before, after runtime.MemStats
				runtime.ReadMemStats(&before)
				_, err = decodeProof(bad, 1)
				runtime.ReadMemStats(&after)
				if err == nil {
					t.Fatalf("accepted commitment count %d", count)
				}
				if allocated := after.TotalAlloc - before.TotalAlloc; allocated > 1024*1024 {
					t.Fatalf("count %d allocated %d bytes", count, allocated)
				}
			}
			for _, bad := range [][]byte{raw[:pointBytes], raw[:len(raw)-1], append(bytes.Clone(raw), 0), make([]byte, 4097)} {
				if _, err := decodeProof(bad, 1); err == nil {
					t.Fatal("accepted malformed proof")
				}
			}
		})
	}
}
