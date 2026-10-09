//! Rate limits, and the client address they are keyed on (D-060).
//!
//! Decided on the tokio thread, before a request reaches a worker loop: a
//! client over its limit must not cost a loop anything, or the limit protects
//! nothing that `max_concurrency` does not already.
//!
//! Each limit is a generic cell rate algorithm: per key, one number, the time
//! at which the key's budget is next fully spent. A request is allowed when
//! adding one interval to that time stays within `burst` intervals of now.
//! That is a token bucket refilled continuously, with no timer and no
//! per-key state beyond a `u64`.

use std::collections::HashMap;
use std::hash::{BuildHasher, BuildHasherDefault, Hasher, RandomState};
use std::net::{IpAddr, Ipv6Addr, SocketAddr};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use hyper::header::{HeaderMap, HeaderName, HeaderValue, RETRY_AFTER};
use hyper::{Response, StatusCode};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::metrics::Refusal;
use crate::request::Request;
use crate::server::Out;

/// Power of two, so the shard is the key's top bits. Enough that tokio threads
/// rarely meet on one lock.
const SHARDS: usize = 64;

/// A shard is swept when it has grown past this, and then again each time it
/// doubles from what the sweep left, so a sweep's cost is spread over at least
/// as many inserts as it visits entries.
const SWEEP_FLOOR: usize = 64;

/// Keys are already uniformly random `u64`s from a seeded hash; hashing them
/// again for the map would be work for nothing.
#[derive(Default)]
struct Passthrough(u64);

impl Hasher for Passthrough {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, _: &[u8]) {
        unreachable!("only u64 keys")
    }
    fn write_u64(&mut self, n: u64) {
        self.0 = n;
    }
}

struct Shard {
    /// Key to the time, in nanoseconds since `Limiter::start`, at which its
    /// budget is next fully spent. A time already past is the same as no
    /// entry, which is what makes sweeping safe.
    spent: HashMap<u64, u64, BuildHasherDefault<Passthrough>>,
    sweep_at: usize,
}

enum Key {
    Client,
    /// A header's value, such as an API key. A request without the header is
    /// keyed on its client instead.
    Header(HeaderName),
}

pub struct Limit {
    interval: u64,
    tolerance: u64,
    key: Key,
    /// Seeded per process, so nobody can choose keys that collide.
    seed: RandomState,
    start: Instant,
    shards: Box<[Mutex<Shard>]>,
}

impl Limit {
    fn new(interval: u64, burst: u64, header: Option<&str>) -> PyResult<Self> {
        let key = match header {
            None => Key::Client,
            Some(name) => Key::Header(
                HeaderName::from_bytes(name.as_bytes())
                    .map_err(|_| PyValueError::new_err(format!("bad header name {name:?}")))?,
            ),
        };
        Ok(Self {
            interval: interval.max(1),
            tolerance: interval.max(1).saturating_mul(burst.max(1)),
            key,
            seed: RandomState::new(),
            start: Instant::now(),
            shards: (0..SHARDS)
                .map(|_| {
                    Mutex::new(Shard {
                        spent: HashMap::default(),
                        sweep_at: SWEEP_FLOOR,
                    })
                })
                .collect(),
        })
    }

    fn key(&self, client: Option<IpAddr>, headers: &HeaderMap) -> u64 {
        let mut hasher = self.seed.build_hasher();
        match &self.key {
            Key::Header(name) if headers.contains_key(name) => {
                hasher.write_u8(1);
                for value in headers.get_all(name) {
                    hasher.write(value.as_bytes());
                    hasher.write_u8(0);
                }
            }
            _ => {
                hasher.write_u8(0);
                match client.map(grouped) {
                    Some(IpAddr::V4(v4)) => hasher.write(&v4.octets()),
                    Some(IpAddr::V6(v6)) => hasher.write(&v6.octets()),
                    // A request with no address at all, built by hand.
                    None => {}
                }
            }
        }
        hasher.finish()
    }

    /// Spend one request of the key's budget, or say how long until there is
    /// one to spend.
    pub fn take(&self, client: Option<IpAddr>, headers: &HeaderMap) -> Result<(), Duration> {
        let key = self.key(client, headers);
        let now = self.start.elapsed().as_nanos() as u64;
        let shard = &self.shards[(key >> (64 - SHARDS.trailing_zeros())) as usize];
        // Held for a map lookup and nothing else: never across an await, and
        // never while attached to the interpreter long enough to meet a
        // stop-the-world pause, since nothing in here calls into Python.
        let mut shard = shard.lock().unwrap_or_else(|e| e.into_inner());
        let spent = shard.spent.get(&key).copied().unwrap_or(now).max(now);
        let next = spent + self.interval;
        if next - now > self.tolerance {
            return Err(Duration::from_nanos(next - self.tolerance - now));
        }
        shard.spent.insert(key, next);
        if shard.spent.len() >= shard.sweep_at {
            // Without this, a client cycling through addresses or header
            // values grows the map for as long as it keeps going. An entry
            // whose time has passed carries no information, so dropping it
            // changes no answer.
            shard.spent.retain(|_, spent| *spent > now);
            shard.sweep_at = (shard.spent.len() * 2).max(SWEEP_FLOOR);
        }
        Ok(())
    }

    fn tracked(&self) -> usize {
        self.shards
            .iter()
            .map(|s| s.lock().unwrap_or_else(|e| e.into_inner()).spent.len())
            .sum()
    }
}

/// An IPv6 client is limited by its /64, the block one host or one customer
/// is normally given: keyed on the whole address, one machine could rotate
/// through billions of them. An IPv4 address written as IPv6 is IPv4.
fn grouped(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
            Some(v4) => IpAddr::V4(v4),
            None => {
                let s = v6.segments();
                IpAddr::V6(Ipv6Addr::new(s[0], s[1], s[2], s[3], 0, 0, 0, 0))
            }
        },
        v4 => v4,
    }
}

/// The answer to a request over its limit.
pub fn refused(wait: Duration) -> Response<Out> {
    let mut response =
        crate::problem::response(StatusCode::TOO_MANY_REQUESTS, Some("rate limit exceeded"));
    // Whole seconds, rounded up: a client that waits what it was told must
    // find a request available.
    let seconds = wait.as_nanos().div_ceil(1_000_000_000).max(1);
    if let Ok(value) = HeaderValue::from_str(&seconds.to_string()) {
        response.headers_mut().insert(RETRY_AFTER, value);
    }
    response.extensions_mut().insert(Refusal::Limited);
    response
}

/// One limit's budget, shared by every route and server that holds it.
///
/// Built from Python by `oxbrook.RateLimit`; the server takes the inner
/// `Arc` when it starts, so a tokio thread never touches the Python object.
#[pyclass(frozen, name = "Limiter", module = "oxbrook._core")]
pub struct Limiter {
    pub inner: Arc<Limit>,
}

#[pymethods]
impl Limiter {
    #[new]
    #[pyo3(signature = (interval_ns, burst, header = None))]
    fn py_new(interval_ns: u64, burst: u64, header: Option<String>) -> PyResult<Self> {
        Ok(Self {
            inner: Arc::new(Limit::new(interval_ns, burst, header.as_deref())?),
        })
    }

    /// Spend one request for this request's key: None when allowed, else the
    /// seconds until one is. The path an MCP tool call takes, which never
    /// passes through the server's own check.
    fn take(&self, request: &Request) -> Option<f64> {
        self.inner
            .take(request.client, &request.headers)
            .err()
            .map(|wait| wait.as_secs_f64())
    }

    /// Keys currently tracked, for tests of the sweep.
    fn _tracked(&self) -> usize {
        self.inner.tracked()
    }
}

/// Which peers are proxies whose `X-Forwarded-For` is believed.
pub enum Proxies {
    /// The peer is the client.
    None,
    /// This many proxies stand in front, whatever their addresses: the client
    /// is that many entries from the right of `X-Forwarded-For`.
    Hops(usize),
    /// Peers in these networks are proxies, and so are entries in them.
    Networks(Vec<(IpAddr, u8)>),
}

/// (hops, networks as (address, prefix length)); (0, []) is none.
pub type ProxiesTuple = (usize, Vec<(String, u8)>);

impl Proxies {
    pub fn build((hops, networks): ProxiesTuple) -> Result<Self, String> {
        if hops > 0 {
            return Ok(Proxies::Hops(hops));
        }
        if networks.is_empty() {
            return Ok(Proxies::None);
        }
        networks
            .into_iter()
            .map(|(addr, len)| {
                addr.parse::<IpAddr>()
                    .map(|ip| (ip, len))
                    .map_err(|_| format!("bad proxy address {addr:?}"))
            })
            .collect::<Result<_, _>>()
            .map(Proxies::Networks)
    }

    fn trusts(networks: &[(IpAddr, u8)], ip: IpAddr) -> bool {
        let ip = match ip {
            IpAddr::V6(v6) => v6.to_ipv4_mapped().map_or(ip, IpAddr::V4),
            v4 => v4,
        };
        networks.iter().any(|&(net, len)| match (net, ip) {
            (IpAddr::V4(n), IpAddr::V4(a)) => {
                let mask = u32::MAX.checked_shl(32 - u32::from(len)).unwrap_or(0);
                u32::from(n) & mask == u32::from(a) & mask
            }
            (IpAddr::V6(n), IpAddr::V6(a)) => {
                let mask = u128::MAX.checked_shl(128 - u32::from(len)).unwrap_or(0);
                u128::from(n) & mask == u128::from(a) & mask
            }
            _ => false,
        })
    }

    /// The client's address: the peer's, unless the peer is a trusted proxy,
    /// in which case the nearest `X-Forwarded-For` entry no trusted proxy
    /// wrote. Read from the right, because each proxy appends: everything to
    /// the left of the entries a trusted proxy wrote is whatever the client
    /// chose to send.
    pub fn client(&self, peer: IpAddr, headers: &HeaderMap) -> IpAddr {
        if matches!(self, Proxies::None) {
            return peer;
        }
        let entries: Vec<&str> = headers
            .get_all("x-forwarded-for")
            .iter()
            .filter_map(|v| v.to_str().ok())
            .flat_map(|v| v.split(','))
            .map(str::trim)
            .collect();
        match self {
            Proxies::None => peer,
            // Fewer entries than proxies means the request did not come
            // through all of them, and the entries cannot be trusted.
            Proxies::Hops(hops) => entries
                .len()
                .checked_sub(*hops)
                .and_then(|i| parse(entries[i]))
                .unwrap_or(peer),
            Proxies::Networks(networks) => {
                if !Self::trusts(networks, peer) {
                    return peer;
                }
                let mut client = peer;
                for entry in entries.iter().rev() {
                    // An entry that does not parse ends the walk: nothing
                    // left of it can be placed.
                    let Some(ip) = parse(entry) else { break };
                    client = ip;
                    if !Self::trusts(networks, ip) {
                        break;
                    }
                }
                client
            }
        }
    }
}

/// An address as proxies write it: bare, or with a port, IPv6 in brackets.
fn parse(entry: &str) -> Option<IpAddr> {
    entry
        .parse::<IpAddr>()
        .ok()
        .or_else(|| entry.parse::<SocketAddr>().ok().map(|s| s.ip()))
        .or_else(|| {
            entry
                .strip_prefix('[')
                .and_then(|e| e.strip_suffix(']'))
                .and_then(|e| e.parse().ok())
        })
}
