package main

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"testing"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
	"golang.org/x/crypto/chacha20poly1305"
)

func makeInput(token []byte) Input {
	return makePlainInput(append(append([]byte(nil), token...), 23), []byte{24, 0, 0, 1, 0, 22})
}
func makePlainInput(token, update []byte) Input {
	key := make([]byte, 32)
	nonce := make([]byte, 12)
	rand.Read(key)
	rand.Read(nonce)
	cipher, _ := chacha20poly1305.New(key)
	seal := func(data []byte, index uint64) string {
		header := []byte{23, 3, 3, 0, 0}
		binary.BigEndian.PutUint16(header[3:], uint16(len(data)+16))
		return hex.EncodeToString(append(header, cipher.Seal(nil, nonceAt(nonce, index), data, header)...))
	}
	return Input{Key: hex.EncodeToString(key), Nonce: hex.EncodeToString(nonce), TokenRecord: seal(token, 0), UpdateRecord: seal(update, 1)}
}

func TestFramingAndBoundaryConstraints(t *testing.T) {
	ccs, e := frontend.Compile(ecc.BN254.ScalarField(), r1cs.NewBuilder, &CredentialCircuit{})
	if e != nil {
		t.Fatal(e)
	}
	solve := func(in Input) error {
		key, _ := decode(in.Key, 32)
		nonce, _ := decode(in.Nonce, 12)
		b := &Bundle{Commitment: commitment(key)}
		for i := range b.MACKeys {
			b.MACKeys[i] = hex.EncodeToString(stream(key, nonceAt(nonce, uint64(i)), 0, 32))
		}
		list, e := assignments(in, b)
		if e != nil {
			return e
		}
		for i := range list {
			w, e := frontend.NewWitness(&list[i], ecc.BN254.ScalarField())
			if e != nil {
				return e
			}
			if e = ccs.IsSolved(w); e != nil {
				return e
			}
		}
		return nil
	}
	alphabet := []byte("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~+/=")
	for _, length := range []int{64, 255, 256, 257, 4096} {
		token := make([]byte, length)
		for i := range token {
			token[i] = alphabet[i%len(alphabet)]
		}
		if e = solve(makeInput(token)); e != nil {
			t.Fatalf("valid length %d: %v", length, e)
		}
	}
	for _, length := range []int{63, 4097} {
		if solve(makeInput(make([]byte, length))) == nil {
			t.Fatalf("invalid length %d accepted", length)
		}
	}
	for _, bad := range []byte{0, 9, 10, 13, 22, 23, 32, 34, 58, 59, 92, 127, 128, 255} {
		token := make([]byte, 257)
		for i := range token {
			token[i] = 'a'
		}
		token[256] = bad
		if solve(makeInput(token)) == nil {
			t.Fatalf("forbidden token byte %d accepted", bad)
		}
	}
	token := make([]byte, 64)
	for i := range token {
		token[i] = 'a'
	}
	for _, suffix := range [][]byte{{22}, {21}, {23, 0}, {23, 0, 0}, {23, 23}} {
		if solve(makePlainInput(append(append([]byte(nil), token...), suffix...), []byte{24, 0, 0, 1, 0, 22})) == nil {
			t.Fatal("hidden inner type/padding accepted")
		}
	}
	for i := 0; i < 6; i++ {
		update := []byte{24, 0, 0, 1, 0, 22}
		update[i] ^= 1
		if solve(makePlainInput(append(append([]byte(nil), token...), 23), update)) == nil {
			t.Fatalf("changed KeyUpdate byte %d accepted", i)
		}
	}
}
func TestCredentialConstraints(t *testing.T) {
	ccs, e := frontend.Compile(ecc.BN254.ScalarField(), r1cs.NewBuilder, &CredentialCircuit{})
	if e != nil {
		t.Fatal(e)
	}
	t.Log("constraints", ccs.GetNbConstraints())
	for _, invalid := range []bool{false, true} {
		token := make([]byte, 300)
		for i := range token {
			token[i] = 'A'
		}
		if invalid {
			token[123] = '\r'
		}
		in := makeInput(token)
		key, _ := decode(in.Key, 32)
		nonce, _ := decode(in.Nonce, 12)
		b := &Bundle{Commitment: commitment(key)}
		for i := range b.MACKeys {
			b.MACKeys[i] = hex.EncodeToString(stream(key, nonceAt(nonce, uint64(i)), 0, 32))
		}
		list, e := assignments(in, b)
		if e != nil {
			t.Fatal(e)
		}
		for i := range list {
			w, e := frontend.NewWitness(&list[i], ecc.BN254.ScalarField())
			if e != nil {
				t.Fatal(e)
			}
			e = ccs.IsSolved(w)
			if invalid && i == 1 {
				if e == nil {
					t.Fatal("CRLF injection accepted")
				}
			} else if e != nil {
				t.Fatalf("valid assignment %d: %v", i, e)
			}
		}
	}
}
