package main

import (
	"encoding/hex"
	"encoding/json"
	"testing"
	"time"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
)

func TestProofRoundTrip(t *testing.T) {
	ccs, e := frontend.Compile(ecc.BN254.ScalarField(), r1cs.NewBuilder, &CredentialCircuit{})
	if e != nil {
		t.Fatal(e)
	}
	pk, vk, e := groth16.Setup(ccs)
	if e != nil {
		t.Fatal(e)
	}
	token := make([]byte, 1522)
	for i := range token {
		token[i] = 'a'
	}
	in := makeInput(token)
	start := time.Now()
	b, e := prove(in, ccs, pk)
	if e != nil {
		t.Fatal(e)
	}
	t.Log("prove", time.Since(start))
	pub := in
	pub.Key = ""
	pub.Bundle = b
	start = time.Now()
	if e = verify(pub, vk); e != nil {
		t.Fatal(e)
	}
	t.Log("verify", time.Since(start))
	raw, _ := json.Marshal(b)
	t.Log("bundle_bytes", len(raw), "proofs", len(b.Proofs))
	clone := func() Input { raw, _ := json.Marshal(pub); var v Input; json.Unmarshal(raw, &v); return v }
	for _, name := range []string{"ciphertext", "nonce", "commitment", "mac", "missing", "reorder", "trailing", "different_exchange"} {
		t.Run(name, func(t *testing.T) {
			v := clone()
			switch name {
			case "ciphertext":
				r, _ := hex.DecodeString(v.TokenRecord)
				r[7] ^= 1
				v.TokenRecord = hex.EncodeToString(r)
			case "nonce":
				r, _ := hex.DecodeString(v.Nonce)
				r[0] ^= 1
				v.Nonce = hex.EncodeToString(r)
			case "commitment":
				v.Bundle.Commitment = "0000000000000000000000000000000000000000000000000000000000000001"
			case "mac":
				v.Bundle.MACKeys[0] = "0000000000000000000000000000000000000000000000000000000000000000"
			case "missing":
				v.Bundle.Proofs = v.Bundle.Proofs[1:]
			case "reorder":
				v.Bundle.Proofs[0], v.Bundle.Proofs[1] = v.Bundle.Proofs[1], v.Bundle.Proofs[0]
			case "trailing":
				v.Bundle.Proofs[0] += "AA=="
			case "different_exchange":
				other := makeInput(token)
				v.TokenRecord = other.TokenRecord
				v.UpdateRecord = other.UpdateRecord
				v.Nonce = other.Nonce
			}
			if verify(v, vk) == nil {
				t.Fatal("altered traffic/proof accepted")
			}
		})
	}
}
