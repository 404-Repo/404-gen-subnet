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

// Public test certificates from https://emudhra.we-have-a-problem.com/chains.zip
// (dv-g3b-aaa chain) and roots.json. The intermediate's SPKI SHA-256 is
// ebed6475c2c2d80fcb199f3fe8dd6e6b3ce60b77eb15d4e42c3dec0c6f5b5105,
// one of the distrusted keys in GO-2026-6809. No network is used.
func TestCertificateDistrustedIntermediate(t *testing.T) {
	const leafPEM = `-----BEGIN CERTIFICATE-----
MIICQDCCAcagAwIBAgIUU3mgjls/8ZJ8upmukdlSQR1IF/UwCgYIKoZIzj0EAwIw
PzELMAkGA1UEBhMCTkwxEzARBgNVBAoMCmVNdWRocmEgQlYxGzAZBgNVBAMMEkVN
IERWIFRMUyBDQSAtIEczQjAeFw0yNjEwMjAwMjIyNDFaFw0yNzA1MDMwMjI3NDFa
MDMxMTAvBgNVBAMMKGR2LWczYi1hYWEtZW11ZGhyYS53ZS1oYXZlLWEtcHJvYmxl
bS5jb20wWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATK3sfcOxxtM2Vdq9Qbw/GR
NsHlJcnqINEInXO3fkWhw/FYT80hkqGk0jSN/EKib3YZJsr5GyfkYcpEiA2rwMgm
o4GrMIGoMAwGA1UdEwEB/wQCMAAwDgYDVR0PAQH/BAQDAgeAMBMGA1UdJQQMMAoG
CCsGAQUFBwMBMDMGA1UdEQQsMCqCKGR2LWczYi1hYWEtZW11ZGhyYS53ZS1oYXZl
LWEtcHJvYmxlbS5jb20wHQYDVR0OBBYEFDggfJFz1eeToKmzFBSYkMCvpAu0MB8G
A1UdIwQYMBaAFOIeL+ccGTkOVUtiwSAkVzUkiiUUMAoGCCqGSM49BAMCA2gAMGUC
MDqDDOt7K313QIH0rhn/ZKnzFu8+/9W8OlTuFtDfVCTRDfRmqdyfsCfLsEk7xqHq
uQIxAJqyLERABmU98U46OCuT5bp5h0ytuSEIGM7orTpKci5S1u1B/AOmwv162U7R
TcVPHA==
-----END CERTIFICATE-----
`
	const intermediatePEM = `-----BEGIN CERTIFICATE-----
MIIDzjCCAragAwIBAgIRAKAk/fS6Zlx4XOSgIEg4WLcwDQYJKoZIhvcNAQELBQAw
ezELMAkGA1UEBhMCR0IxGzAZBgNVBAgMEkdyZWF0ZXIgTWFuY2hlc3RlcjEQMA4G
A1UEBwwHU2FsZm9yZDEaMBgGA1UECgwRQ29tb2RvIENBIExpbWl0ZWQxITAfBgNV
BAMMGEFBQSBDZXJ0aWZpY2F0ZSBTZXJ2aWNlczAeFw0yNTA1MTQwMDAwMDBaFw0y
ODEyMzEyMzU5NTlaMD8xCzAJBgNVBAYTAk5MMRMwEQYDVQQKDAplTXVkaHJhIEJW
MRswGQYDVQQDDBJFTSBEViBUTFMgQ0EgLSBHM0IwdjAQBgcqhkjOPQIBBgUrgQQA
IgNiAATnntnASpdzxHNeUREdqFrCvV5kqzdLgOEsvCA47B5PDjuBL8mFnh5nr7BO
L4vJf3SFumWqkR7opORbYtXMA9Vr5nizw4y03JN8okYOI4RTyJKTXSnUT5HhVga2
PM7atGijggE1MIIBMTAfBgNVHSMEGDAWgBSgEQojPpbxB+zirynvgqV/0DCktDAd
BgNVHQ4EFgQU4h4v5xwZOQ5VS2LBICRXNSSKJRQwDgYDVR0PAQH/BAQDAgEGMBIG
A1UdEwEB/wQIMAYBAf8CAQAwHQYDVR0lBBYwFAYIKwYBBQUHAwEGCCsGAQUFBwMC
MDEGA1UdIAQqMCgwCAYGZ4EMAQIBMA0GCysGAQQBg44hAQABMA0GCysGAQQBg44h
AQJkMEMGA1UdHwQ8MDowOKA2oDSGMmh0dHA6Ly9jcmwuY29tb2RvY2EuY29tL0FB
QUNlcnRpZmljYXRlU2VydmljZXMuY3JsMDQGCCsGAQUFBwEBBCgwJjAkBggrBgEF
BQcwAYYYaHR0cDovL29jc3AuY29tb2RvY2EuY29tMA0GCSqGSIb3DQEBCwUAA4IB
AQBzQ1IoqpdotM6gFrR4Q3YrKifOLeaj5XXunldpVE/vZn2TxC2y0s8oYiLR5ICE
wqVXSB7N6BM1Mp+Nif7Jbhb2IswAfSR4Iik8+B0iRmi7lYoNlycCgHjmsjOmZaxM
QkPmjXAJbG5f4V41OsvJZmS7lI3r0IuvEDVX3GAZ13on6IImSs9ybNpHQ7NCqkMQ
7wH4uAO1dtpFycgJSdHaCOhHjrox2BTYGLdpUrYwY3EOAjlZ8UGgsxs7mG+xquMl
bN1LFURwfnkVMP3T/DBIZ/ys2+lNFyxK3FlArrV4/000uTnVB1lO8oGORYdXoT5V
GUyRCEIDhF5TBUuKF4E3kf1h
-----END CERTIFICATE-----
`
	const rootPEM = `-----BEGIN CERTIFICATE-----
MIIEMjCCAxqgAwIBAgIBATANBgkqhkiG9w0BAQUFADB7MQswCQYDVQQGEwJHQjEb
MBkGA1UECAwSR3JlYXRlciBNYW5jaGVzdGVyMRAwDgYDVQQHDAdTYWxmb3JkMRow
GAYDVQQKDBFDb21vZG8gQ0EgTGltaXRlZDEhMB8GA1UEAwwYQUFBIENlcnRpZmlj
YXRlIFNlcnZpY2VzMB4XDTA0MDEwMTAwMDAwMFoXDTI4MTIzMTIzNTk1OVowezEL
MAkGA1UEBhMCR0IxGzAZBgNVBAgMEkdyZWF0ZXIgTWFuY2hlc3RlcjEQMA4GA1UE
BwwHU2FsZm9yZDEaMBgGA1UECgwRQ29tb2RvIENBIExpbWl0ZWQxITAfBgNVBAMM
GEFBQSBDZXJ0aWZpY2F0ZSBTZXJ2aWNlczCCASIwDQYJKoZIhvcNAQEBBQADggEP
ADCCAQoCggEBAL5AnfRu4ep2hxxNRUSOvkbIgwadwSr+GB+O5AL686tdUIoWMQua
BtDFcCLNSS1UY8y2bmhGC1Pqy0wkwLxyTurxFa70VJoSCsN6sjNg4tqJVfMiWPPe
3M/vg4aijJRPn2jymJBGhCfHdr/jzDUsi14HZGWCwEiwqJH5YZ92IFCokcdmtet4
YgNW8IoaE+oxox6gmf049vYnMlhvB/VruPsUK6+3qszWY19zjNoFmag4qMsXeDZR
rOme9Hg6jc8P2ULimAyrL58OAd7vn5lJ8S3frHRNG5i1R8XlKdH5kBjHYpy+g8cm
ez6KJcfA3Z3mNWgQIJ2P2N7Sw4ScDV7oL8kCAwEAAaOBwDCBvTAdBgNVHQ4EFgQU
oBEKIz6W8Qfs4q8p74Klf9AwpLQwDgYDVR0PAQH/BAQDAgEGMA8GA1UdEwEB/wQF
MAMBAf8wewYDVR0fBHQwcjA4oDagNIYyaHR0cDovL2NybC5jb21vZG9jYS5jb20v
QUFBQ2VydGlmaWNhdGVTZXJ2aWNlcy5jcmwwNqA0oDKGMGh0dHA6Ly9jcmwuY29t
b2RvLm5ldC9BQUFDZXJ0aWZpY2F0ZVNlcnZpY2VzLmNybDANBgkqhkiG9w0BAQUF
AAOCAQEACFb8AvCb6P+k+tZ7xkSAzk/ExfYAWMymtrwUSWgEdujm7l3sAg9g1o1Q
GE8mTgHj5rCl7r+8dFRBv/38ErjHT1r0iWAFf2C3BUrz9vHCv8S5dIa2LX1rzNLz
Rt0vxuBqw8M0Ayx9lt1awg6nCpnBBYurDC/zXDrPbDdVCYfeU0BsWO/8tqtlbgT2
G9w84FoVxp7Z8VlIMCFlA2zs6SFz7JsDoeA3raAVGI/6ugLOpyypEBMs1OUIJqsi
l2D4kF501KKaU73yqWjgom7C12yxow+ev+to51byrvLjKzg6CYG1a4XXvi3tPxq3
smPi9WIsgtRqAEFQ8TmDn5XpNpaYbg==
-----END CERTIFICATE-----
`
	in := CertificateInput{
		Host:  "dv-g3b-aaa-emudhra.we-have-a-problem.com",
		Chain: []string{leafPEM}, Roots: []string{leafPEM},
		TimeUnixNano: strconv.FormatInt(time.Date(2026, time.October, 21, 0, 0, 0, 0, time.UTC).UnixNano(), 10),
	}
	// Direct trust in the leaf checks its hostname, usage and frozen validity
	// time without passing through the distrusted intermediate.
	if err := verifyCertificates(in); err != nil {
		t.Fatalf("valid leaf rejected: %v", err)
	}
	in.Chain = []string{leafPEM, intermediatePEM}
	in.Roots = []string{rootPEM}
	if err := verifyCertificates(in); err == nil {
		t.Fatal("certificate chain with a distrusted intermediate accepted")
	}
}
