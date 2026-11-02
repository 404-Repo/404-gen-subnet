package main

import (
	"bytes"
	"crypto/subtle"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"math/big"

	"github.com/consensys/gnark-crypto/ecc"
	curve "github.com/consensys/gnark-crypto/ecc/bn254"
	nativeMimc "github.com/consensys/gnark-crypto/ecc/bn254/fr/mimc"
	"github.com/consensys/gnark/backend/groth16"
	groth16bn254 "github.com/consensys/gnark/backend/groth16/bn254"
	"github.com/consensys/gnark/constraint"
	"github.com/consensys/gnark/frontend"
	"golang.org/x/crypto/chacha20"
	"golang.org/x/crypto/poly1305"
)

type Input struct {
	Key          string  `json:"key,omitempty"`
	Nonce        string  `json:"nonce"`
	TokenRecord  string  `json:"token_record"` // observed TLS record, including header/tag
	UpdateRecord string  `json:"update_record"`
	Bundle       *Bundle `json:"bundle,omitempty"`
}
type Bundle struct {
	Commitment string    `json:"commitment"`
	MACKeys    [2]string `json:"mac_keys"`
	Proofs     []string  `json:"proofs"`
}

func decode(s string, n int) ([]byte, error) {
	b, e := hex.DecodeString(s)
	if e != nil || len(b) != n {
		return nil, errors.New("invalid hex length")
	}
	return b, nil
}
func commitment(key []byte) string {
	h := nativeMimc.NewMiMC()
	for _, v := range []*big.Int{big.NewInt(4041301), new(big.Int).SetBytes(key[:16]), new(big.Int).SetBytes(key[16:])} {
		var b [32]byte
		v.FillBytes(b[:])
		h.Write(b[:])
	}
	return hex.EncodeToString(h.Sum(nil))
}
func stream(key, nonce []byte, counter uint32, n int) []byte {
	c, e := chacha20.NewUnauthenticatedCipher(key, nonce)
	if e != nil {
		panic(e)
	}
	c.SetCounter(counter)
	out := make([]byte, n)
	c.XORKeyStream(out, out)
	return out
}
func nonceAt(nonce []byte, index uint64) []byte {
	v := append([]byte(nil), nonce...)
	var seq [8]byte
	binary.BigEndian.PutUint64(seq[:], index)
	for i := range seq {
		v[4+i] ^= seq[i]
	}
	return v
}

func record(s string) ([]byte, error) {
	b, e := hex.DecodeString(s)
	if e != nil || len(b) < 22 || len(b) > MaxToken+22 {
		return nil, errors.New("invalid private record size")
	}
	if !bytes.Equal(b[:3], []byte{23, 3, 3}) || int(binary.BigEndian.Uint16(b[3:5])) != len(b)-5 {
		return nil, errors.New("invalid private record header")
	}
	return b, nil
}
func authenticate(record, key []byte) bool {
	var polyKey [32]byte
	copy(polyKey[:], key)
	ciphertext := record[5 : len(record)-16]
	data := make([]byte, 0, 32+len(ciphertext)+32)
	data = append(data, record[:5]...)
	data = append(data, make([]byte, 11)...)
	data = append(data, ciphertext...)
	data = append(data, make([]byte, (16-len(ciphertext)%16)%16)...)
	var lengths [16]byte
	binary.LittleEndian.PutUint64(lengths[:8], 5)
	binary.LittleEndian.PutUint64(lengths[8:], uint64(len(ciphertext)))
	data = append(data, lengths[:]...)
	var tag [16]byte
	poly1305.Sum(&tag, data, &polyKey)
	return subtle.ConstantTimeCompare(tag[:], record[len(record)-16:]) == 1
}

// The verifier constructs every public input from its own observed records.
// The miner supplies no mask, offsets, counters, record lengths or token length.
func assignments(in Input, b *Bundle) ([]CredentialCircuit, error) {
	nonce, e := decode(in.Nonce, 12)
	if e != nil {
		return nil, e
	}
	token, e := record(in.TokenRecord)
	if e != nil {
		return nil, e
	}
	update, e := record(in.UpdateRecord)
	if e != nil {
		return nil, e
	}
	tokenLength := len(token) - 22
	if tokenLength < 64 || tokenLength > MaxToken || len(update) != 27 {
		return nil, errors.New("invalid credential/key-update length")
	}
	digest, e := decode(b.Commitment, 32)
	if e != nil {
		return nil, e
	}
	d := new(big.Int).SetBytes(digest)
	if d.Cmp(ecc.BN254.ScalarField()) >= 0 {
		return nil, errors.New("noncanonical commitment")
	}
	key := make([]byte, 32)
	if in.Key != "" {
		key, e = decode(in.Key, 32)
		if e != nil {
			return nil, e
		}
		if commitment(key) != b.Commitment {
			return nil, errors.New("key commitment mismatch")
		}
	}
	makeAssignment := func(kind, counter, length int, nonce, ct, macKey []byte) CredentialCircuit {
		c := CredentialCircuit{Kind: kind, Counter: counter, Length: length, TokenLength: tokenLength, Commitment: d}
		for i := range c.Key {
			c.Key[i] = key[i]
		}
		for i := range c.Nonce {
			c.Nonce[i] = nonce[i]
		}
		for i := range c.Ciphertext {
			c.Ciphertext[i] = 0
			if i < len(ct) {
				c.Ciphertext[i] = ct[i]
			}
		}
		for i := range c.MACKey {
			c.MACKey[i] = 0
			if i < len(macKey) {
				c.MACKey[i] = macKey[i]
			}
		}
		return c
	}
	var out []CredentialCircuit
	for index, rec := range [][]byte{token, update} {
		n := nonceAt(nonce, uint64(index))
		mac, e := decode(b.MACKeys[index], 32)
		if e != nil {
			return nil, e
		}
		if !authenticate(rec, mac) {
			return nil, errors.New("private record authentication failed")
		}
		out = append(out, makeAssignment(0, 0, 32, n, nil, mac))
		ct := rec[5 : len(rec)-16]
		for offset := 0; offset < len(ct); offset += ChunkSize {
			end := offset + ChunkSize
			if end > len(ct) {
				end = len(ct)
			}
			kind := 1
			if index == 1 {
				kind = 2
			}
			out = append(out, makeAssignment(kind, 1+offset/64, end-offset, n, ct[offset:end], nil))
		}
	}
	return out, nil
}

func prove(in Input, ccs constraint.ConstraintSystem, pk groth16.ProvingKey) (*Bundle, error) {
	key, e := decode(in.Key, 32)
	if e != nil {
		return nil, e
	}
	nonce, e := decode(in.Nonce, 12)
	if e != nil {
		return nil, e
	}
	b := &Bundle{Commitment: commitment(key)}
	for i := range b.MACKeys {
		b.MACKeys[i] = hex.EncodeToString(stream(key, nonceAt(nonce, uint64(i)), 0, 32))
	}
	list, e := assignments(in, b)
	if e != nil {
		return nil, e
	}
	for i := range list {
		witness, e := frontend.NewWitness(&list[i], ecc.BN254.ScalarField())
		if e != nil {
			return nil, e
		}
		proof, e := groth16.Prove(ccs, pk, witness)
		if e != nil {
			return nil, errors.New("private credential constraints failed")
		}
		var buf bytes.Buffer
		if _, e = proof.WriteTo(&buf); e != nil {
			return nil, e
		}
		b.Proofs = append(b.Proofs, base64.StdEncoding.EncodeToString(buf.Bytes()))
	}
	return b, nil
}

// gnark's generic slice decoder allocates from an untrusted uint32 before
// reading its elements. Read the count ourselves and compare with the pinned
// verification key before allocating. Point decoding retains subgroup checks.
func decodeProof(raw []byte, expectedCommitments int) (*groth16bn254.Proof, error) {
	if len(raw) > 4096 || expectedCommitments < 0 || expectedCommitments > len(raw)/curve.SizeOfG1AffineCompressed {
		return nil, errors.New("invalid proof encoding")
	}
	reader := bytes.NewReader(raw)
	decoder := curve.NewDecoder(reader)
	p := new(groth16bn254.Proof)
	for _, point := range []any{&p.Ar, &p.Bs, &p.Krs} {
		if err := decoder.Decode(point); err != nil {
			return nil, errors.New("invalid proof point")
		}
	}
	var count uint32
	if err := binary.Read(reader, binary.BigEndian, &count); err != nil || uint64(count) != uint64(expectedCommitments) {
		return nil, errors.New("invalid proof commitment count")
	}
	if reader.Len() < (expectedCommitments+1)*curve.SizeOfG1AffineCompressed {
		return nil, errors.New("truncated proof commitments")
	}
	p.Commitments = make([]curve.G1Affine, expectedCommitments)
	for i := range p.Commitments {
		if err := decoder.Decode(&p.Commitments[i]); err != nil {
			return nil, errors.New("invalid proof commitment")
		}
	}
	if err := decoder.Decode(&p.CommitmentPok); err != nil || reader.Len() != 0 {
		return nil, errors.New("invalid proof ending")
	}
	return p, nil
}

func verify(in Input, vk groth16.VerifyingKey) error {
	if in.Key != "" {
		return errors.New("verifier must never receive a private key")
	}
	if in.Bundle == nil {
		return errors.New("missing credential proofs")
	}
	list, e := assignments(in, in.Bundle)
	if e != nil {
		return e
	}
	if len(in.Bundle.Proofs) != len(list) {
		return errors.New("missing/extra credential proof")
	}
	for i := range list {
		raw, e := base64.StdEncoding.DecodeString(in.Bundle.Proofs[i])
		if e != nil || len(raw) > 4096 {
			return errors.New("invalid proof encoding")
		}
		p, e := decodeProof(raw, len(vk.(*groth16bn254.VerifyingKey).CommitmentKeys))
		if e != nil {
			return errors.New("invalid proof")
		}
		pub, e := frontend.NewWitness(&list[i], ecc.BN254.ScalarField(), frontend.PublicOnly())
		if e != nil {
			return e
		}
		if e = groth16.Verify(p, vk, pub); e != nil {
			return fmt.Errorf("credential proof %d failed", i)
		}
	}
	return nil
}
