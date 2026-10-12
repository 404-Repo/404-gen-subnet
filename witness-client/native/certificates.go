package main

import (
	"crypto/x509"
	"encoding/asn1"
	"encoding/pem"
	"errors"
)

type CertificateInput struct {
	Host  string   `json:"host"`
	Chain []string `json:"chain"`
	Roots []string `json:"roots"`
}

func parseCertificate(s string) (*x509.Certificate, error) {
	block, rest := pem.Decode([]byte(s))
	if block == nil || block.Type != "CERTIFICATE" || len(rest) != 0 {
		return nil, errors.New("invalid certificate PEM")
	}
	return x509.ParseCertificate(block.Bytes)
}

func verifyCertificates(in CertificateInput) error {
	if in.Host == "" || len(in.Host) > 253 || len(in.Chain) == 0 || len(in.Chain) > 16 || len(in.Roots) == 0 || len(in.Roots) > 512 {
		return errors.New("invalid certificate input")
	}
	roots, intermediates := x509.NewCertPool(), x509.NewCertPool()
	for _, raw := range in.Roots {
		cert, err := parseCertificate(raw)
		if err != nil {
			return err
		}
		roots.AddCert(cert)
	}
	var leaf *x509.Certificate
	for i, raw := range in.Chain {
		cert, err := parseCertificate(raw)
		if err != nil {
			return err
		}
		if i == 0 {
			leaf = cert
		} else {
			intermediates.AddCert(cert)
		}
	}
	// TLS 1.3 authenticates the leaf with a signature. Verify checks EKU, but
	// crypto/x509 intentionally leaves the leaf's KeyUsage to the application.
	for _, ext := range leaf.Extensions {
		if ext.Id.Equal(asn1.ObjectIdentifier{2, 5, 29, 15}) && leaf.KeyUsage&x509.KeyUsageDigitalSignature == 0 {
			return errors.New("server certificate does not permit signatures")
		}
	}
	_, err := leaf.Verify(x509.VerifyOptions{Roots: roots, Intermediates: intermediates,
		DNSName: in.Host, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}})
	return err
}
