package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"encoding/pem"
	"math/big"
	"strconv"
	"testing"
	"time"
)

func TestCertificateValidation(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	var serial int64
	issue := func(template, parent *x509.Certificate) *x509.Certificate {
		serial++
		template.SerialNumber = big.NewInt(serial)
		if parent == nil {
			parent = template
		}
		der, err := x509.CreateCertificate(rand.Reader, template, parent, &key.PublicKey, key)
		if err != nil {
			t.Fatal(err)
		}
		cert, err := x509.ParseCertificate(der)
		if err != nil {
			t.Fatal(err)
		}
		return cert
	}
	encode := func(cert *x509.Certificate) string {
		return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw}))
	}
	template := func(name string, ca bool) *x509.Certificate {
		c := &x509.Certificate{Subject: pkix.Name{CommonName: name},
			NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
			BasicConstraintsValid: true, IsCA: ca, KeyUsage: x509.KeyUsageDigitalSignature}
		if ca {
			c.KeyUsage |= x509.KeyUsageCertSign
		} else {
			c.DNSNames = []string{name}
			c.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		}
		return c
	}
	root := issue(template("test root", true), nil)
	for _, name := range []string{"valid", "non_CA_issuer", "issuer_without_cert_sign", "expired", "historical_expired",
		"wrong_host", "CN_cannot_override_SAN", "client_only_EKU", "leaf_without_signatures",
		"unknown_critical_extension", "name_constraint", "path_length", "untrusted_root", "missing_issuer"} {
		t.Run(name, func(t *testing.T) {
			intermediate := template("intermediate", true)
			leaf := template("chatgpt.com", false)
			switch name {
			case "non_CA_issuer":
				intermediate.IsCA = false
			case "issuer_without_cert_sign":
				intermediate.KeyUsage = x509.KeyUsageDigitalSignature
			case "expired", "historical_expired":
				leaf.NotAfter = time.Now().Add(-time.Minute)
			case "wrong_host":
				leaf.DNSNames = []string{"miner.example"}
				leaf.Subject.CommonName = "miner.example"
			case "CN_cannot_override_SAN":
				leaf.DNSNames = []string{"miner.example"}
			case "client_only_EKU":
				leaf.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
			case "leaf_without_signatures":
				leaf.KeyUsage = x509.KeyUsageKeyEncipherment
			case "unknown_critical_extension":
				leaf.ExtraExtensions = []pkix.Extension{{Id: asn1.ObjectIdentifier{1, 2, 3, 4}, Critical: true, Value: []byte{5, 0}}}
			case "name_constraint":
				intermediate.PermittedDNSDomainsCritical = true
				intermediate.PermittedDNSDomains = []string{"miner.example"}
			case "path_length":
				intermediate.MaxPathLenZero = true
			}
			issuer := issue(intermediate, root)
			chain := []string{encode(issuer)}
			if name == "path_length" {
				issuer = issue(template("extra intermediate", true), issuer)
				chain = append([]string{encode(issuer)}, chain...)
			}
			chain = append([]string{encode(issue(leaf, issuer))}, chain...)
			roots := []string{encode(root)}
			if name == "untrusted_root" {
				roots = []string{encode(issue(template("other root", true), nil))}
			}
			if name == "missing_issuer" {
				chain = chain[:1]
			}
			in := CertificateInput{Host: "chatgpt.com", Chain: chain, Roots: roots}
			if name == "historical_expired" {
				in.TimeUnixNano = strconv.FormatInt(time.Now().Add(-10*time.Minute).UnixNano(), 10)
			}
			err := verifyCertificates(in)
			if (err == nil) != (name == "valid" || name == "historical_expired") {
				t.Fatalf("unexpected validation result: %v", err)
			}
		})
	}
}
