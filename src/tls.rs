//! TLS termination (D-044), and picking up a renewed certificate (D-072).
//!
//! rustls with the `ring` provider: no C toolchain beyond what the crate
//! already needs, and no system OpenSSL whose version differs per host. The
//! certificate is read once at startup, so a bad path or a key that does not
//! belong to the certificate stops the server before it binds rather than
//! failing every handshake afterwards.
//!
//! After that the files are looked at every `poll`: when either has changed,
//! the pair is read again and, if it is a valid pair, used for every
//! handshake from then on. Connections already open keep the certificate they
//! were made with. A pair that does not load (a renewal caught between
//! writing the certificate and the key, say) leaves the old one in place and
//! is tried again at the next look, so a renewal is never a restart and never
//! an outage.

use std::fmt;
use std::fs;
use std::sync::{Arc, RwLock};
use std::time::{Duration, SystemTime};

use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer};
use tokio_rustls::rustls::crypto::{ring, CryptoProvider};
use tokio_rustls::rustls::server::{ClientHello, ResolvesServerCert};
use tokio_rustls::rustls::sign::CertifiedKey;
use tokio_rustls::rustls::ServerConfig;
use tokio_rustls::TlsAcceptor;

/// (certificate chain path, private key path, seconds between looks at them)
pub type TlsTuple = (String, String, f64);

/// What a file looked like when last read: a renewal changes one of these.
type Stamp = Option<(SystemTime, u64)>;

/// The certificate's stamp and the key's.
type Stamps = (Stamp, Stamp);

/// The pair in use, and the last pair that failed to load, so a broken pair
/// is reported once rather than at every look.
struct Seen {
    used: Stamps,
    failed: Option<Stamps>,
}

fn stamp(path: &str) -> Stamp {
    let meta = fs::metadata(path).ok()?;
    Some((meta.modified().ok()?, meta.len()))
}

fn load(cert: &str, key: &str, provider: &CryptoProvider) -> Result<CertifiedKey, String> {
    let chain = CertificateDer::pem_file_iter(cert)
        .and_then(|certs| certs.collect::<Result<Vec<_>, _>>())
        .map_err(|e| format!("reading the certificate {cert}: {e}"))?;
    if chain.is_empty() {
        return Err(format!("no certificate found in {cert}"));
    }
    let private =
        PrivateKeyDer::from_pem_file(key).map_err(|e| format!("reading the key {key}: {e}"))?;
    // Checks that the key belongs to the leaf certificate, so swapped or stale
    // files are refused here and not at the first handshake.
    CertifiedKey::from_der(chain, private, provider)
        .map_err(|e| format!("the key {key} does not fit the certificate {cert}: {e}"))
}

/// The certificate every new handshake gets, replaced when the files change.
pub struct Certificate {
    cert: String,
    key: String,
    provider: Arc<CryptoProvider>,
    current: RwLock<Arc<CertifiedKey>>,
    seen: RwLock<Seen>,
    poll: Duration,
}

impl fmt::Debug for Certificate {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Certificate")
            .field("cert", &self.cert)
            .finish()
    }
}

impl ResolvesServerCert for Certificate {
    fn resolve(&self, _hello: ClientHello<'_>) -> Option<Arc<CertifiedKey>> {
        // A read lock held for a refcount bump; the writer holds it as long.
        Some(
            self.current
                .read()
                .unwrap_or_else(|e| e.into_inner())
                .clone(),
        )
    }
}

impl Certificate {
    /// Look at the files and load them again if they changed. Blocking file
    /// IO: run off the tokio threads. True when a new pair is now in use.
    fn refresh(&self) -> bool {
        let stamps = (stamp(&self.cert), stamp(&self.key));
        {
            let seen = self.seen.read().unwrap_or_else(|e| e.into_inner());
            if seen.used == stamps || seen.failed == Some(stamps) {
                return false;
            }
        }
        match load(&self.cert, &self.key, &self.provider) {
            Ok(loaded) => {
                *self.current.write().unwrap_or_else(|e| e.into_inner()) = Arc::new(loaded);
                *self.seen.write().unwrap_or_else(|e| e.into_inner()) = Seen {
                    used: stamps,
                    failed: None,
                };
                eprintln!("oxbrook: loaded the renewed TLS certificate {}", self.cert);
                true
            }
            Err(e) => {
                self.seen.write().unwrap_or_else(|e| e.into_inner()).failed = Some(stamps);
                eprintln!("oxbrook: keeping the current TLS certificate: {e}");
                false
            }
        }
    }

    /// Looks at the files every `poll` for as long as the runtime lives.
    pub async fn watch(self: Arc<Self>) {
        let mut ticks = tokio::time::interval(self.poll);
        ticks.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        ticks.tick().await;
        loop {
            ticks.tick().await;
            let this = self.clone();
            let _ = tokio::task::spawn_blocking(move || this.refresh()).await;
        }
    }
}

pub fn acceptor(
    (cert, key, poll): &TlsTuple,
    http2: bool,
) -> Result<(TlsAcceptor, Arc<Certificate>), String> {
    let provider = Arc::new(ring::default_provider());
    // Stamped before reading, so a change made while reading is seen at the
    // first look rather than missed.
    let stamps = (stamp(cert), stamp(key));
    let loaded = load(cert, key, &provider)?;
    let certificate = Arc::new(Certificate {
        cert: cert.clone(),
        key: key.clone(),
        provider: provider.clone(),
        current: RwLock::new(Arc::new(loaded)),
        seen: RwLock::new(Seen {
            used: stamps,
            failed: None,
        }),
        poll: Duration::from_secs_f64(poll.max(0.01)),
    });

    let mut config = ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| format!("TLS configuration: {e}"))?
        .with_no_client_auth()
        .with_cert_resolver(certificate.clone());

    // ALPN is how a TLS client learns HTTP/2 is available. Without `h2` here,
    // a browser never tries it, whatever the connection handler accepts.
    config.alpn_protocols = if http2 {
        vec![b"h2".to_vec(), b"http/1.1".to_vec()]
    } else {
        vec![b"http/1.1".to_vec()]
    };
    Ok((TlsAcceptor::from(Arc::new(config)), certificate))
}
