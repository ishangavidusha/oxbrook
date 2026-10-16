//! Which origins may open a WebSocket.
//!
//! Browsers do not apply CORS to WebSockets. A page on any site can open a
//! socket to this server, and the browser attaches the user's cookies to the
//! handshake, so a socket authenticated by cookie is usable by every site the
//! user visits: cross-site WebSocket hijacking. The one signal a browser always
//! sends and a page cannot forge is `Origin`, so it is checked here, in Rust,
//! before the authorizer or the handler runs and before the 101 is sent.
//!
//! Accepted: no `Origin` at all (not a browser, and not the threat); an origin
//! naming this server, compared with `Host` or `X-Forwarded-Host`, neither of
//! which a page can set on a handshake; and the configured origins.
//!
//! The configured origins, here and for CORS, may hold one `*` in the first
//! label of the host — `https://*.example.dev`, `https://app-*.example.dev` —
//! which `Pattern` matches.

use std::collections::HashSet;

use hyper::header::{HeaderMap, HOST, ORIGIN};

/// (any origin, allowed origins) from `oxbrook._app`.
pub type OriginsTuple = (bool, Vec<String>);

pub struct SocketOrigins {
    any: bool,
    allowed: HashSet<String>,
    patterns: Vec<Pattern>,
}

/// An origin with one `*` in the first label of its host.
///
/// The `*` stands for one or more letters, digits or hyphens: never a dot,
/// so it covers exactly one label, and never nothing. Everything else is
/// compared exactly: scheme, the rest of the host, and the port. That shuts
/// the two classic holes in origin patterns, a match on
/// `https://example.dev.attacker.com` and one on `https://attackerexample.dev`,
/// since the host must end with exactly the pattern's remaining labels.
#[derive(Debug, PartialEq)]
pub struct Pattern {
    scheme: String,
    /// The first label's text before and after the `*`.
    before: String,
    after: String,
    /// The labels after the first, with their leading dot: `.example.dev`.
    rest: String,
    port: Option<String>,
}

impl Pattern {
    /// None for an origin with no `*`, or one this cannot read; Python has
    /// already refused those with a reason.
    pub fn parse(origin: &str) -> Option<Self> {
        let origin = origin.trim().to_ascii_lowercase();
        let (scheme, authority) = origin.split_once("://")?;
        let authority = without_default_port(authority, Some(scheme));
        let (host, port) = match authority.rsplit_once(':') {
            Some((host, port)) => (host.to_owned(), Some(port.to_owned())),
            None => (authority, None),
        };
        let (first, rest) = host.split_once('.')?;
        let (before, after) = first.split_once('*')?;
        if after.contains('*') || rest.contains('*') || !rest.contains('.') {
            return None;
        }
        Some(Self {
            scheme: scheme.to_owned(),
            before: before.to_owned(),
            after: after.to_owned(),
            rest: format!(".{rest}"),
            port,
        })
    }

    /// `origin` must already be lowercased.
    pub fn matches(&self, origin: &str) -> bool {
        let Some((scheme, authority)) = origin.split_once("://") else {
            return false;
        };
        if scheme != self.scheme {
            return false;
        }
        let authority = without_default_port(authority, Some(scheme));
        let (host, port) = match authority.rsplit_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (authority.as_str(), None),
        };
        if port != self.port.as_deref() {
            return false;
        }
        let Some(first) = host.strip_suffix(self.rest.as_str()) else {
            return false;
        };
        let Some(middle) = first
            .strip_prefix(self.before.as_str())
            .and_then(|f| f.strip_suffix(self.after.as_str()))
        else {
            return false;
        };
        !middle.is_empty()
            && middle
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
    }
}

/// Split configured origins into exact ones and patterns.
pub fn split(origins: Vec<String>) -> (HashSet<String>, Vec<Pattern>) {
    let mut exact = HashSet::new();
    let mut patterns = Vec::new();
    for origin in origins {
        if origin.contains('*') {
            patterns.extend(Pattern::parse(&origin));
        } else {
            exact.insert(origin.to_ascii_lowercase());
        }
    }
    (exact, patterns)
}

/// `https://example.com:443` and `example.com:443` both become `example.com`.
fn without_default_port(authority: &str, scheme: Option<&str>) -> String {
    let lowered = authority.trim().to_ascii_lowercase();
    let strip = match scheme {
        Some("https") | Some("wss") => [":443"].as_slice(),
        Some("http") | Some("ws") => [":80"].as_slice(),
        // A Host header carries no scheme, so either default port is dropped.
        _ => [":80", ":443"].as_slice(),
    };
    for suffix in strip {
        if let Some(bare) = lowered.strip_suffix(suffix) {
            return bare.to_owned();
        }
    }
    lowered
}

impl SocketOrigins {
    pub fn build(spec: OriginsTuple) -> Self {
        let (any, allowed) = spec;
        let (allowed, patterns) = split(allowed);
        Self {
            any,
            allowed,
            patterns,
        }
    }

    pub fn permits(&self, headers: &HeaderMap) -> bool {
        if self.any {
            return true;
        }
        let Some(origin) = headers.get(ORIGIN) else {
            return true;
        };
        let Ok(origin) = origin.to_str() else {
            return false;
        };
        let origin = origin.trim().to_ascii_lowercase();
        if self.allowed.contains(&origin) || self.patterns.iter().any(|p| p.matches(&origin)) {
            return true;
        }
        // `null`, from sandboxed frames and local files, has no authority to
        // compare, and is refused unless listed.
        let Some((scheme, authority)) = origin.split_once("://") else {
            return false;
        };
        let authority = without_default_port(authority, Some(scheme));

        let named = |value: Option<&str>| {
            value.is_some_and(|host| without_default_port(host, None) == authority)
        };
        if named(headers.get(HOST).and_then(|v| v.to_str().ok())) {
            return true;
        }
        // Behind a proxy that rewrites Host — nginx does by default — the
        // original arrives here instead. Trusting it cannot help an attacking
        // page, which has no way to set it on a handshake.
        named(
            headers
                .get("x-forwarded-host")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.split(',').next()),
        )
    }
}
