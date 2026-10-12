import { rootCertificates } from 'node:tls';
import { runProof } from './worker.mjs';

// Trust roots come from the local runtime/operator, never from the TLS peer.
// Go's path validator checks CA constraints, usages, names and critical extensions.
export async function verifyServerCertificates(certificates, host, rootCAs = [], options) {
  if (!certificates?.length || certificates.length > 16) throw new Error('Invalid certificate chain length');
  const chain = certificates.map(cert => cert.serialiseToPem());
  if (chain.reduce((n, pem) => n + Buffer.byteLength(pem), 0) > 192 * 1024) throw new Error('Certificate chain too large');
  await runProof('certificate', { host, chain, roots: [...rootCertificates, ...rootCAs.map(cert => cert.serialiseToPem())] },
    { ...options, timeout: Math.min(options?.timeout ?? 10000, 10000) });
}
