//! TLS termination (D-044).
//!
//! rustls with the `ring` provider: no C toolchain beyond what the crate
//! already needs, and no system OpenSSL whose version differs per host. The
//! certificate is read once at startup, so a bad path or a key that does not
//! belong to the certificate stops the server before it binds rather than
//! failing every handshake afterwards.

use std::sync::Arc;

use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer};
use tokio_rustls::rustls::crypto::ring;
use tokio_rustls::rustls::ServerConfig;
use tokio_rustls::TlsAcceptor;

/// (certificate chain path, private key path)
pub type TlsTuple = (String, String);

pub fn acceptor((cert, key): &TlsTuple, http2: bool) -> Result<TlsAcceptor, String> {
    let chain = CertificateDer::pem_file_iter(cert)
        .and_then(|certs| certs.collect::<Result<Vec<_>, _>>())
        .map_err(|e| format!("reading the certificate {cert}: {e}"))?;
    if chain.is_empty() {
        return Err(format!("no certificate found in {cert}"));
    }
    let private =
        PrivateKeyDer::from_pem_file(key).map_err(|e| format!("reading the key {key}: {e}"))?;

    let mut config = ServerConfig::builder_with_provider(Arc::new(ring::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|e| format!("TLS configuration: {e}"))?
        .with_no_client_auth()
        // Checks that the key belongs to the leaf certificate, so swapped or
        // stale files are refused here and not at the first handshake.
        .with_single_cert(chain, private)
        .map_err(|e| format!("the key {key} does not fit the certificate {cert}: {e}"))?;

    // ALPN is how a TLS client learns HTTP/2 is available. Without `h2` here,
    // a browser never tries it, whatever the connection handler accepts.
    config.alpn_protocols = if http2 {
        vec![b"h2".to_vec(), b"http/1.1".to_vec()]
    } else {
        vec![b"http/1.1".to_vec()]
    };
    Ok(TlsAcceptor::from(Arc::new(config)))
}
