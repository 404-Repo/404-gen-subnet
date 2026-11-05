//go:build cgo

package main

import (
	"bytes"
	"strconv"
	"testing"
	"time"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
)

func resetPythonProver(t *testing.T) {
	t.Helper()
	pythonProver.Lock()
	defer pythonProver.Unlock()
	if p := pythonProver.pool; p != nil {
		p.Lock()
		for p.initializing {
			changed := p.changed
			p.Unlock()
			<-changed
			p.Lock()
		}
		p.Unlock()
	}
	pythonProver.pool, pythonProver.directory = nil, ""
}

func TestPythonProverContextLimit(t *testing.T) {
	for _, value := range []string{"", "1", "2", "3", "4", "8", "0", "-1", "9", "invalid", "99999999999999999999"} {
		t.Run(value, func(t *testing.T) {
			t.Setenv("WITNESS_PROVER_CONTEXTS", value)
			got, err := pythonProverContextLimit()
			want, _ := strconv.Atoi(value)
			if value == "" {
				want = 1
			}
			if want >= 1 && want <= 8 {
				if err != nil || got != want {
					t.Fatalf("limit = %d, %v; want %d", got, err, want)
				}
			} else if err == nil {
				t.Fatal("invalid context limit accepted")
			}
		})
	}
}

func TestPythonProofPoolLazyBound(t *testing.T) {
	ccs, err := frontend.Compile(ecc.BN254.ScalarField(), r1cs.NewBuilder, &unrelatedCircuit{})
	if err != nil {
		t.Fatal(err)
	}
	key, _, err := groth16.Setup(ccs)
	if err != nil {
		t.Fatal(err)
	}
	var circuit, proving bytes.Buffer
	if _, err := ccs.WriteTo(&circuit); err != nil {
		t.Fatal(err)
	}
	if _, err := key.WriteTo(&proving); err != nil {
		t.Fatal(err)
	}
	for _, limit := range []int{1, 2, 4, 8} {
		t.Run(strconv.Itoa(limit), func(t *testing.T) {
			first, err := newPythonProofContext(circuit.Bytes(), proving.Bytes(), nil)
			if err != nil {
				t.Fatal(err)
			}
			p := newPythonProofPool(first, circuit.Bytes(), proving.Bytes(), limit)
			if p.count != 1 || p.initializing || len(p.available) != 1 {
				t.Fatal("initialization must prepare exactly one context")
			}
			held, err := p.acquire()
			if err != nil || held != first || p.count != 1 || p.initializing {
				t.Fatal("single request must use the first context without growing")
			}
			p.release(held)
			type result struct {
				context *pythonProofContext
				err     error
			}
			results := make(chan result, limit+1)
			for i := 0; i <= limit; i++ {
				go func() {
					context, err := p.acquire()
					results <- result{context, err}
				}()
			}
			var leased []*pythonProofContext
			for i := 0; i < limit; i++ {
				select {
				case r := <-results:
					if r.err != nil {
						t.Fatal(r.err)
					}
					for _, previous := range leased {
						if previous.ccs == r.context.ccs || previous.key == r.context.key {
							t.Fatal("concurrent contexts share gnark objects")
						}
					}
					leased = append(leased, r.context)
				case <-time.After(10 * time.Second):
					t.Fatal("pool did not grow to satisfy demand")
				}
			}
			select {
			case <-results:
				t.Fatal("pool exceeded its context limit")
			case <-time.After(20 * time.Millisecond):
			}
			p.release(leased[0])
			select {
			case r := <-results:
				if r.err != nil || r.context != leased[0] {
					t.Fatal("queued request did not reuse the released context")
				}
				p.release(r.context)
			case <-time.After(10 * time.Second):
				t.Fatal("queued request stayed blocked after release")
			}
			for _, context := range leased[1:] {
				p.release(context)
			}
			if p.count != limit || p.initializing || len(p.available) != limit {
				t.Fatal("pool did not retain the bounded contexts")
			}
		})
	}
}

func TestPythonProofPoolInitializationFailure(t *testing.T) {
	p := newPythonProofPool(&pythonProofContext{}, []byte("invalid"), nil, 2)
	first, err := p.acquire()
	if err != nil {
		t.Fatal(err)
	}
	defer p.release(first)
	results := make(chan error, 3)
	for i := 0; i < cap(results); i++ {
		go func() { _, err := p.acquire(); results <- err }()
	}
	for i := 0; i < cap(results); i++ {
		select {
		case err := <-results:
			if err == nil {
				t.Fatal("context initialization failure was hidden")
			}
		case <-time.After(10 * time.Second):
			t.Fatal("initialization failure left a caller blocked")
		}
	}
}

func TestPythonProofPoolServesOlderWaitersBeforeNewCalls(t *testing.T) {
	first := &pythonProofContext{}
	p := newPythonProofPool(first, nil, nil, 1)
	held, err := p.acquire()
	if err != nil {
		t.Fatal(err)
	}
	order := make(chan int, 5)
	release := make([]chan struct{}, 5)
	for i := range release {
		release[i] = make(chan struct{})
	}
	request := func(i int) {
		context, err := p.acquire()
		if err != nil || context != first {
			order <- -1
			return
		}
		order <- i
		<-release[i]
		p.release(context)
	}
	for i := 0; i < 4; i++ {
		go request(i)
		deadline := time.Now().Add(time.Second)
		for {
			p.Lock()
			queued := len(p.waiting)
			p.Unlock()
			if queued == i+1 {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("caller did not enter the queue")
			}
			time.Sleep(time.Millisecond)
		}
	}
	p.release(held)
	go request(4)
	for want := 0; want < 5; want++ {
		select {
		case got := <-order:
			if got != want {
				t.Fatalf("caller %d bypassed caller %d", got, want)
			}
			close(release[got])
		case <-time.After(time.Second):
			t.Fatal("queued caller did not receive the released context")
		}
	}
}
