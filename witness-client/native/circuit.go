package main

// The circuit proves a bounded part of RFC 8439 ChaCha20, and credential
// syntax. Groth16 and integer gadgets come from gnark. Payloads never enter it.
import (
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/std/hash/mimc"
	"github.com/consensys/gnark/std/lookup/logderivlookup"
	"github.com/consensys/gnark/std/math/cmp"
	"github.com/consensys/gnark/std/math/uints"
	"math/big"
)

const ChunkSize = 256
const MaxToken = 4096

type CredentialCircuit struct {
	Key         [32]frontend.Variable
	Commitment  frontend.Variable            `gnark:",public"`
	Nonce       [12]frontend.Variable        `gnark:",public"`
	Counter     frontend.Variable            `gnark:",public"`
	Kind        frontend.Variable            `gnark:",public"` // 0: MAC key, 1: token, 2: KeyUpdate
	Length      frontend.Variable            `gnark:",public"`
	TokenLength frontend.Variable            `gnark:",public"`
	Ciphertext  [ChunkSize]frontend.Variable `gnark:",public"`
	MACKey      [32]frontend.Variable        `gnark:",public"`
}

func chachaBlock(u *uints.BinaryField[uints.U32], key [8]uints.U32, nonce [3]uints.U32, counter uints.U32) []uints.U8 {
	initial := [16]uints.U32{uints.NewU32(0x61707865), uints.NewU32(0x3320646e), uints.NewU32(0x79622d32), uints.NewU32(0x6b206574)}
	copy(initial[4:12], key[:])
	initial[12] = counter
	copy(initial[13:], nonce[:])
	s := initial
	qr := func(a, b, c, d int) {
		s[a] = u.Add(s[a], s[b])
		s[d] = u.Lrot(u.Xor(s[d], s[a]), 16)
		s[c] = u.Add(s[c], s[d])
		s[b] = u.Lrot(u.Xor(s[b], s[c]), 12)
		s[a] = u.Add(s[a], s[b])
		s[d] = u.Lrot(u.Xor(s[d], s[a]), 8)
		s[c] = u.Add(s[c], s[d])
		s[b] = u.Lrot(u.Xor(s[b], s[c]), 7)
	}
	for i := 0; i < 10; i++ {
		qr(0, 4, 8, 12)
		qr(1, 5, 9, 13)
		qr(2, 6, 10, 14)
		qr(3, 7, 11, 15)
		qr(0, 5, 10, 15)
		qr(1, 6, 11, 12)
		qr(2, 7, 8, 13)
		qr(3, 4, 9, 14)
	}
	out := make([]uints.U8, 0, 64)
	for i := range s {
		out = append(out, u.UnpackLSB(u.Add(s[i], initial[i]))...)
	}
	return out
}

func (c *CredentialCircuit) Define(api frontend.API) error {
	u, err := uints.NewBinaryField[uints.U32](api)
	if err != nil {
		return err
	}
	h, err := mimc.NewMiMC(api)
	if err != nil {
		return err
	}
	var key [8]uints.U32
	var nonce [3]uints.U32
	for i := range key {
		key[i] = u.PackLSB(u.ByteValueOf(c.Key[4*i]), u.ByteValueOf(c.Key[4*i+1]), u.ByteValueOf(c.Key[4*i+2]), u.ByteValueOf(c.Key[4*i+3]))
	}
	// Fixed-size, domain-separated commitment. No variable-length hash input.
	h.Write(4041301)
	for i := 0; i < 2; i++ {
		var packed frontend.Variable = 0
		for j := 0; j < 16; j++ {
			packed = api.Add(api.Mul(packed, 256), c.Key[i*16+j])
		}
		h.Write(packed)
	}
	api.AssertIsEqual(h.Sum(), c.Commitment)
	for i := range nonce {
		nonce[i] = u.PackLSB(u.ByteValueOf(c.Nonce[4*i]), u.ByteValueOf(c.Nonce[4*i+1]), u.ByteValueOf(c.Nonce[4*i+2]), u.ByteValueOf(c.Nonce[4*i+3]))
	}
	mac := api.IsZero(c.Kind)
	auth := api.IsZero(api.Sub(c.Kind, 1))
	update := api.IsZero(api.Sub(c.Kind, 2))
	api.AssertIsEqual(api.Add(mac, auth, update), 1)
	api.AssertIsLessOrEqual(c.Length, ChunkSize)
	api.AssertIsDifferent(c.Length, 0)
	api.AssertIsLessOrEqual(c.TokenLength, MaxToken)
	api.AssertIsLessOrEqual(64, c.TokenLength)
	api.AssertIsLessOrEqual(c.Counter, MaxToken/64+1)
	api.AssertIsEqual(api.Mul(mac, c.Counter), 0)
	api.AssertIsEqual(api.Mul(update, api.Sub(c.Counter, 1)), 0)
	api.AssertIsEqual(api.Mul(mac, api.Sub(c.Length, 32)), 0)
	api.AssertIsEqual(api.Mul(update, api.Sub(c.Length, 6)), 0)
	api.AssertIsEqual(api.Mul(auth, api.IsZero(c.Counter)), 0)
	table := logderivlookup.New(api)
	compare := cmp.NewBoundedComparator(api, big.NewInt(MaxToken+ChunkSize), false)
	for b := 0; b < 256; b++ {
		valid := 0
		if b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z' || b >= '0' && b <= '9' || b == '-' || b == '_' || b == '.' || b == '~' || b == '+' || b == '/' || b == '=' {
			valid = 1
		}
		table.Insert(valid)
	}
	fixedUpdate := []byte{24, 0, 0, 1, 0, 22}
	for block := 0; block < ChunkSize/64; block++ {
		stream := chachaBlock(u, key, nonce, u.ValueOf(api.Add(c.Counter, block)))
		for j := 0; j < 64; j++ {
			i := block*64 + j
			ciphertext := u.ByteValueOf(c.Ciphertext[i])
			plaintext := u.Bytes.Xor(ciphertext, stream[j])
			p := u.Value(plaintext)
			active := compare.IsLess(i, c.Length)
			api.AssertIsEqual(api.Mul(api.Sub(1, active), c.Ciphertext[i]), 0)
			if i < 32 {
				api.AssertIsEqual(api.Mul(mac, api.Sub(p, c.MACKey[i])), 0)
			}
			if i < 6 {
				api.AssertIsEqual(api.Mul(update, api.Sub(p, fixedUpdate[i])), 0)
			}
			position := api.Select(auth, api.Add(api.Mul(api.Sub(c.Counter, 1), 64), i), 0)
			last := api.IsZero(api.Sub(position, c.TokenLength))
			enabled := api.Mul(auth, active)
			api.AssertIsEqual(api.Mul(enabled, compare.IsLess(c.TokenLength, position)), 0)
			api.AssertIsEqual(api.Mul(enabled, last, api.Sub(p, 23)), 0)
			allowed := table.Lookup(p)[0]
			api.AssertIsEqual(api.Mul(enabled, api.Sub(1, last), api.Sub(allowed, 1)), 0)
		}
	}
	for i := range c.MACKey {
		u.ByteValueOf(c.MACKey[i])
		api.AssertIsEqual(api.Mul(api.Sub(1, mac), c.MACKey[i]), 0)
	}
	return nil
}
