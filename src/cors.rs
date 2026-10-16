//! Cross-origin resource sharing, applied in Rust.
//!
//! Here rather than in Python middleware for one reason: the server answers
//! 404, 405, 413, 422, 503 and 504 itself, before a worker is woken, and a
//! middleware never sees those responses. A browser that gets a 503 without CORS
//! headers reports a CORS failure, not an overloaded server, and the developer
//! debugging it looks in the wrong place. So every response gets the headers,
//! whoever produced it, and a preflight is answered without touching Python.

use std::collections::HashSet;

use hyper::header::{
    HeaderMap, HeaderValue, ACCESS_CONTROL_ALLOW_CREDENTIALS, ACCESS_CONTROL_ALLOW_HEADERS,
    ACCESS_CONTROL_ALLOW_METHODS, ACCESS_CONTROL_ALLOW_ORIGIN, ACCESS_CONTROL_EXPOSE_HEADERS,
    ACCESS_CONTROL_MAX_AGE, ACCESS_CONTROL_REQUEST_HEADERS, ACCESS_CONTROL_REQUEST_METHOD, ORIGIN,
    VARY,
};
use hyper::Method;

/// (origins, methods, headers, allow_credentials, expose_headers, max_age)
/// as built by `oxbrook._cors.CORS`. `"*"` in a list means any.
pub type CorsTuple = (
    Vec<String>,
    Vec<String>,
    Vec<String>,
    bool,
    Vec<String>,
    Option<u64>,
);

pub struct Cors {
    any_origin: bool,
    origins: HashSet<String>,
    patterns: Vec<crate::origin::Pattern>,
    any_method: bool,
    methods: HashSet<String>,
    methods_value: Option<HeaderValue>,
    any_header: bool,
    headers: HashSet<String>,
    headers_value: Option<HeaderValue>,
    credentials: bool,
    expose: Option<HeaderValue>,
    max_age: Option<HeaderValue>,
}

fn joined(items: &[String]) -> Option<HeaderValue> {
    if items.is_empty() {
        return None;
    }
    HeaderValue::from_str(&items.join(", ")).ok()
}

impl Cors {
    pub fn build(spec: CorsTuple) -> Result<Self, String> {
        let (origins, methods, headers, credentials, expose, max_age) = spec;
        let any_origin = origins.iter().any(|o| o == "*");
        // Checked in Python too. Repeated here because this is the layer that
        // would actually send the header: echoing any origin with credentials
        // lets every website make authenticated requests as the user.
        if any_origin && credentials {
            return Err("allow_credentials cannot be combined with any origin".into());
        }
        let any_method = methods.iter().any(|m| m == "*");
        let methods: Vec<String> = methods.iter().map(|m| m.to_ascii_uppercase()).collect();
        let any_header = headers.iter().any(|h| h == "*");
        let headers: Vec<String> = headers.iter().map(|h| h.to_ascii_lowercase()).collect();
        let (origins, patterns) =
            crate::origin::split(origins.into_iter().filter(|o| o != "*").collect());
        Ok(Self {
            any_origin,
            origins,
            patterns,
            any_method,
            methods_value: if any_method { None } else { joined(&methods) },
            methods: methods.into_iter().collect(),
            any_header,
            headers_value: if any_header { None } else { joined(&headers) },
            headers: headers.into_iter().collect(),
            credentials,
            expose: joined(&expose),
            max_age: max_age.map(HeaderValue::from),
        })
    }

    fn allows(&self, origin: &str) -> bool {
        if self.any_origin || self.origins.contains(origin) {
            return true;
        }
        // Patterns compare lowercased, as browsers send origins; the exact
        // set above is matched as given, which is what it always was.
        !self.patterns.is_empty() && {
            let lowered = origin.to_ascii_lowercase();
            self.patterns.iter().any(|p| p.matches(&lowered))
        }
    }

    /// Whether the allowed origin header is a literal `*` rather than the
    /// request's own origin. Only then may a cache ignore `Origin`.
    fn wildcard(&self) -> bool {
        self.any_origin && !self.credentials
    }

    /// OPTIONS carrying `Origin` and `Access-Control-Request-Method`. A plain
    /// OPTIONS is not a preflight and is routed like any other request.
    pub fn is_preflight(method: &Method, headers: &HeaderMap) -> bool {
        method == Method::OPTIONS
            && headers.contains_key(ORIGIN)
            && headers.contains_key(ACCESS_CONTROL_REQUEST_METHOD)
    }

    /// The headers of a successful preflight, or why it was refused.
    ///
    /// Answered for any path, whether or not a route exists there: a preflight
    /// is asking about the origin, and answering differently for paths that do
    /// and do not exist would tell a foreign site which ones exist.
    pub fn preflight(&self, request: &HeaderMap) -> Result<HeaderMap, &'static str> {
        let origin = request
            .get(ORIGIN)
            .and_then(|v| v.to_str().ok())
            .ok_or("missing origin")?;
        if !self.allows(origin) {
            return Err("origin not allowed");
        }
        let method = request
            .get(ACCESS_CONTROL_REQUEST_METHOD)
            .and_then(|v| v.to_str().ok())
            .ok_or("missing request method")?;
        if !self.any_method && !self.methods.contains(&method.to_ascii_uppercase()) {
            return Err("method not allowed");
        }
        let asked = request
            .get(ACCESS_CONTROL_REQUEST_HEADERS)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        if !self.any_header {
            let refused = asked
                .split(',')
                .map(|h| h.trim().to_ascii_lowercase())
                .any(|h| !h.is_empty() && !self.headers.contains(&h));
            if refused {
                return Err("header not allowed");
            }
        }

        let mut out = HeaderMap::new();
        self.allow_origin(origin, &mut out);
        // Echoing what was asked is valid whether or not credentials are on,
        // where a literal `*` is not honoured by browsers with credentials.
        let methods = if self.any_method {
            HeaderValue::from_str(method).ok()
        } else {
            self.methods_value.clone()
        };
        if let Some(value) = methods {
            out.insert(ACCESS_CONTROL_ALLOW_METHODS, value);
        }
        let headers = if self.any_header {
            (!asked.is_empty())
                .then(|| HeaderValue::from_str(asked).ok())
                .flatten()
        } else {
            self.headers_value.clone()
        };
        if let Some(value) = headers {
            out.insert(ACCESS_CONTROL_ALLOW_HEADERS, value);
        }
        if let Some(age) = &self.max_age {
            out.insert(ACCESS_CONTROL_MAX_AGE, age.clone());
        }
        out.append(
            VARY,
            HeaderValue::from_static(
                "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
            ),
        );
        Ok(out)
    }

    fn allow_origin(&self, origin: &str, out: &mut HeaderMap) {
        let value = if self.wildcard() {
            HeaderValue::from_static("*")
        } else {
            match HeaderValue::from_str(origin) {
                Ok(value) => value,
                Err(_) => return,
            }
        };
        out.insert(ACCESS_CONTROL_ALLOW_ORIGIN, value);
        if self.credentials {
            out.insert(
                ACCESS_CONTROL_ALLOW_CREDENTIALS,
                HeaderValue::from_static("true"),
            );
        }
    }

    /// Add CORS headers to an actual response.
    ///
    /// A header the handler already set is left alone, so a route can make its
    /// own decision. `Vary: Origin` is added whenever the answer depends on the
    /// origin, including when there was no origin: a shared cache that stored
    /// the header-less response would otherwise serve it to a foreign site.
    pub fn decorate(&self, origin: Option<&HeaderValue>, response: &mut HeaderMap) {
        if !self.wildcard() {
            response.append(VARY, HeaderValue::from_static("Origin"));
        }
        let Some(origin) = origin.and_then(|v| v.to_str().ok()) else {
            return;
        };
        if !self.allows(origin) || response.contains_key(ACCESS_CONTROL_ALLOW_ORIGIN) {
            return;
        }
        let mut added = HeaderMap::new();
        self.allow_origin(origin, &mut added);
        if let Some(expose) = &self.expose {
            added.insert(ACCESS_CONTROL_EXPOSE_HEADERS, expose.clone());
        }
        for (name, value) in added {
            if let Some(name) = name {
                response.insert(name, value);
            }
        }
    }
}
