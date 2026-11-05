//go:build cgo

package main

import (
	"errors"
	"sync"
)

// Each context exclusively owns its constraint system and proving key. The
// serialized parameters are immutable and authenticated before pool creation.
type pythonProofPool struct {
	sync.Mutex
	available        []*pythonProofContext
	waiting          []chan pythonProofLease
	count, limit     int
	initializing     bool
	initializeError  error
	changed          chan struct{}
	circuit, proving []byte
}

type pythonProofLease struct {
	context *pythonProofContext
	err     error
}

func newPythonProofPool(first *pythonProofContext, circuit, proving []byte, limit int) *pythonProofPool {
	p := &pythonProofPool{
		available: []*pythonProofContext{first}, count: 1, limit: limit,
		changed: make(chan struct{}),
	}
	if limit > 1 {
		p.circuit, p.proving = circuit, proving
	}
	return p
}

func (p *pythonProofPool) notify() {
	close(p.changed)
	p.changed = make(chan struct{})
}

func (p *pythonProofPool) acquire() (*pythonProofContext, error) {
	p.Lock()
	if p.initializeError != nil {
		err := p.initializeError
		p.Unlock()
		return nil, err
	}
	if n := len(p.available); n > 0 {
		context := p.available[n-1]
		p.available = p.available[:n-1]
		p.Unlock()
		return context, nil
	}
	ready := make(chan pythonProofLease, 1)
	p.waiting = append(p.waiting, ready)
	p.growIfNeeded()
	p.Unlock()
	lease := <-ready
	return lease.context, lease.err
}

// Caller holds the pool mutex. Only queued demand starts initialization.
func (p *pythonProofPool) growIfNeeded() {
	if len(p.waiting) > 0 && p.count < p.limit && !p.initializing {
		p.initializing = true
		go p.grow()
	}
}

// Caller holds the pool mutex. A released context belongs to the oldest waiter
// before a new arrival can acquire it.
func (p *pythonProofPool) handoff(context *pythonProofContext) {
	if len(p.waiting) == 0 {
		p.available = append(p.available, context)
		return
	}
	ready := p.waiting[0]
	p.waiting[0] = nil
	p.waiting = p.waiting[1:]
	ready <- pythonProofLease{context: context}
}

func (p *pythonProofPool) release(context *pythonProofContext) {
	p.Lock()
	defer p.Unlock()
	p.handoff(context)
}

func (p *pythonProofPool) grow() {
	var context *pythonProofContext
	var err error
	// A background initializer has its own panic boundary, since it cannot
	// unwind through the recovery in the calling C ABI entry point.
	func() {
		defer func() {
			if recover() != nil {
				err = errors.New("proof context initialization failed")
			}
		}()
		context, err = newPythonProofContext(p.circuit, p.proving, nil)
	}()
	p.Lock()
	defer p.Unlock()
	p.initializing = false
	if err != nil {
		p.initializeError = err
		for _, ready := range p.waiting {
			ready <- pythonProofLease{err: err}
		}
		p.waiting = nil
	} else {
		p.count++
		p.handoff(context)
		p.growIfNeeded()
	}
	p.notify()
}
