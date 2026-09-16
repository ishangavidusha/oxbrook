//! Routing and parameter coercion.
//!
//! One `matchit` radix tree per HTTP method. A miss in the request's own method
//! is checked against the others so a wrong method returns 405 with an `Allow`
//! header rather than a misleading 404.
//!
//! Path and query parameters are both coerced here, on the tokio thread, which
//! means `/users/abc` for an `int` parameter or a missing required query
//! parameter is rejected without ever waking a Python worker.

use std::borrow::Cow;
use std::collections::HashMap;

use matchit::Router as Matcher;
use time::format_description::well_known::Iso8601;
use time::macros::format_description;

const ISO: Iso8601 = Iso8601::DEFAULT;
/// Output shapes Python's own `fromisoformat` always accepts.
const ISO_DATE: &[time::format_description::BorrowedFormatItem<'_>] =
    format_description!("[year]-[month]-[day]");
const ISO_NAIVE: &[time::format_description::BorrowedFormatItem<'_>] =
    format_description!("[year]-[month]-[day]T[hour]:[minute]:[second]");
const ISO_OFFSET: &[time::format_description::BorrowedFormatItem<'_>] = format_description!(
    "[year]-[month]-[day]T[hour]:[minute]:[second][offset_hour sign:mandatory]:[offset_minute]"
);

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ParamKind {
    Str,
    Int,
    Float,
    Bool,
    /// Validated here, built into a Python object on the worker thread. The
    /// alternative, handing Python a string and letting its constructor fail,
    /// would turn a malformed id into a 500 instead of a 422.
    Uuid,
    Date,
    DateTime,
}

impl ParamKind {
    fn parse(name: &str) -> Option<Self> {
        match name {
            "str" => Some(Self::Str),
            "int" => Some(Self::Int),
            "float" => Some(Self::Float),
            "bool" => Some(Self::Bool),
            "uuid" => Some(Self::Uuid),
            "date" => Some(Self::Date),
            "datetime" => Some(Self::DateTime),
            _ => None,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Str => "string",
            Self::Int => "integer",
            Self::Float => "number",
            Self::Bool => "boolean",
            Self::Uuid => "UUID",
            Self::Date => "date in YYYY-MM-DD form",
            Self::DateTime => "ISO 8601 datetime",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Path,
    Query,
}

impl Source {
    fn parse(name: &str) -> Option<Self> {
        match name {
            "path" => Some(Self::Path),
            "query" => Some(Self::Query),
            _ => None,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Path => "path",
            Self::Query => "query",
        }
    }
}

/// What to do when a query parameter is absent.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Presence {
    /// Absent is an error.
    Required,
    /// Leave it out of the kwargs so the handler's own default applies.
    Omit,
    /// Pass None explicitly, for an optional annotation with no default.
    Null,
}

impl Presence {
    fn parse(name: &str) -> Option<Self> {
        match name {
            "required" => Some(Self::Required),
            "omit" => Some(Self::Omit),
            "null" => Some(Self::Null),
            _ => None,
        }
    }
}

/// A coerced parameter, still plain Rust data. It becomes a Python object only
/// on the worker thread.
pub enum ParamValue {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
    /// Validated here; the worker turns it into `uuid.UUID`.
    Uuid(String),
    /// Validated here; the worker turns it into `datetime.date`.
    Date(String),
    /// Validated here; the worker turns it into `datetime.datetime`.
    DateTime(String),
    /// A repeated query parameter.
    List(Vec<ParamValue>),
    /// Set the key to None.
    Null,
    /// Do not set the key at all.
    Omit,
}

pub struct ParamSpec {
    pub name: String,
    kind: ParamKind,
    source: Source,
    presence: Presence,
    /// Collect every occurrence rather than the first, for `list[T]`.
    repeated: bool,
}

pub struct RouteSpec {
    pub params: Vec<ParamSpec>,
    /// Skip query-string parsing entirely for routes that declare none.
    has_query: bool,
    /// Handled by the upgrade path rather than the ordinary reply path.
    pub websocket: bool,
    /// Has an authorizer that must approve the upgrade first.
    pub gated: bool,
    /// Takes its body as a `BodyStream` rather than collected up front.
    pub streaming: bool,
    /// A static mount, answered in Rust from `State::mounts[i]`.
    pub mount: Option<usize>,
}

/// A parameter that was missing or would not coerce. Rendered in the shape
/// pydantic uses for body errors, so a client sees one error format for
/// every 422.
pub struct ParamError {
    source: &'static str,
    name: String,
    error_type: &'static str,
    msg: String,
    input: Option<String>,
}

impl ParamError {
    pub fn to_json(&self) -> Vec<u8> {
        let mut entry = serde_json::json!({
            "type": self.error_type,
            "loc": [self.source, self.name],
            "msg": self.msg,
        });
        if let Some(input) = &self.input {
            entry["input"] = serde_json::Value::String(input.clone());
        }
        let detail = serde_json::json!({ "detail": [entry] });
        serde_json::to_vec(&detail).unwrap_or_else(|_| br#"{"detail":[]}"#.to_vec())
    }
}

/// Why a request could not be routed.
pub enum RouteError {
    NotFound,
    /// Path exists under other methods; carries the `Allow` header value.
    MethodNotAllowed(String),
    BadParam(ParamError),
}

pub struct Matched {
    pub route: usize,
    pub params: Vec<ParamValue>,
    /// For a static mount, the path after the prefix, still percent-encoded.
    /// None when the request named the prefix itself.
    pub file: Option<String>,
}

/// (name, type, source, presence, repeated) from the Python side.
pub type SpecTuple = (String, String, String, String, bool);

/// (method, path, params, is_websocket, has_authorizer, streams_body)
pub type RouteTuple = (String, String, Vec<SpecTuple>, bool, bool, bool);

pub struct Router {
    by_method: HashMap<String, Matcher<usize>>,
    specs: Vec<RouteSpec>,
    methods: Vec<String>,
}

impl Router {
    /// `mounts` holds each static mount's patterns, as (pattern, captures the
    /// file). Mounts sit in the same tree as routes, so a mount and a route
    /// that cannot coexist are refused the same way two routes are, and a
    /// POST under a mount gets the same 405.
    pub fn build(routes: &[RouteTuple], mounts: &[Vec<(String, bool)>]) -> Result<Self, String> {
        let mut by_method: HashMap<String, Matcher<usize>> = HashMap::new();
        let mut specs = Vec::with_capacity(routes.len());

        for (index, (method, path, params, websocket, gated, streaming)) in
            routes.iter().enumerate()
        {
            let mut spec = RouteSpec {
                params: Vec::new(),
                has_query: false,
                websocket: *websocket,
                gated: *gated,
                streaming: *streaming,
                mount: None,
            };
            for (name, kind, source, presence, repeated) in params {
                let kind = ParamKind::parse(kind)
                    .ok_or_else(|| format!("unsupported parameter type {kind:?} for {name:?}"))?;
                let source = Source::parse(source)
                    .ok_or_else(|| format!("unknown parameter source {source:?}"))?;
                let presence = Presence::parse(presence)
                    .ok_or_else(|| format!("unknown parameter presence {presence:?}"))?;
                spec.has_query |= source == Source::Query;
                spec.params.push(ParamSpec {
                    name: name.clone(),
                    kind,
                    source,
                    presence,
                    repeated: *repeated,
                });
            }
            specs.push(spec);

            by_method
                .entry(method.to_ascii_uppercase())
                .or_default()
                .insert(path.as_str(), index)
                .map_err(|e| format!("cannot register {method} {path}: {e}"))?;
        }

        for (mount, patterns) in mounts.iter().enumerate() {
            for (pattern, _captures) in patterns {
                let index = specs.len();
                specs.push(RouteSpec {
                    params: Vec::new(),
                    has_query: false,
                    websocket: false,
                    gated: false,
                    streaming: false,
                    mount: Some(mount),
                });
                by_method
                    .entry("GET".to_owned())
                    .or_default()
                    .insert(pattern.as_str(), index)
                    .map_err(|e| format!("cannot mount static files at {pattern}: {e}"))?;
            }
        }

        let methods = by_method.keys().cloned().collect();
        Ok(Self {
            by_method,
            specs,
            methods,
        })
    }

    pub fn spec(&self, route: usize) -> &RouteSpec {
        &self.specs[route]
    }

    pub fn find(
        &self,
        method: &str,
        path: &str,
        query: Option<&str>,
    ) -> Result<Matched, RouteError> {
        let Some(matcher) = self.by_method.get(method) else {
            return Err(self.other_methods(method, path));
        };
        let Ok(found) = matcher.at(path) else {
            return Err(self.other_methods(method, path));
        };

        let route = *found.value;
        let spec = &self.specs[route];

        if spec.mount.is_some() {
            return Ok(Matched {
                route,
                params: Vec::new(),
                file: found.params.get("file").map(str::to_owned),
            });
        }

        // Parsed once per request, and only when the route declares query
        // parameters, so routes without them pay nothing.
        let pairs: Vec<(String, String)> = if spec.has_query {
            form_urlencoded::parse(query.unwrap_or("").as_bytes())
                .map(|(k, v)| (k.into_owned(), v.into_owned()))
                .collect()
        } else {
            Vec::new()
        };

        let mut params = Vec::with_capacity(spec.params.len());
        for param in &spec.params {
            if param.repeated {
                // Every occurrence, in the order the client sent them.
                let mut items = Vec::new();
                for (key, value) in &pairs {
                    if key == &param.name {
                        items.push(coerce(value, param).map_err(RouteError::BadParam)?);
                    }
                }
                params.push(match (items.is_empty(), param.presence) {
                    (true, Presence::Required) => {
                        return Err(RouteError::BadParam(ParamError {
                            source: param.source.label(),
                            name: param.name.clone(),
                            error_type: "missing",
                            msg: "Field required".to_owned(),
                            input: None,
                        }))
                    }
                    (true, Presence::Omit) => ParamValue::Omit,
                    (true, Presence::Null) => ParamValue::Null,
                    _ => ParamValue::List(items),
                });
                continue;
            }

            let decoded;
            let raw = match param.source {
                Source::Path => {
                    decoded = found.params.get(&param.name).map(decode_path);
                    decoded.as_deref()
                }
                // First wins on a repeated key unless the handler asked for a
                // list, which is what `repeated` above handles.
                Source::Query => pairs
                    .iter()
                    .find(|(k, _)| k == &param.name)
                    .map(|(_, v)| v.as_str()),
            };

            let value = match raw {
                Some(raw) => coerce(raw, param).map_err(RouteError::BadParam)?,
                None => match param.presence {
                    Presence::Omit => ParamValue::Omit,
                    Presence::Null => ParamValue::Null,
                    Presence::Required => {
                        return Err(RouteError::BadParam(ParamError {
                            source: param.source.label(),
                            name: param.name.clone(),
                            error_type: "missing",
                            msg: "Field required".to_owned(),
                            input: None,
                        }))
                    }
                },
            };
            params.push(value);
        }

        Ok(Matched {
            route,
            params,
            file: None,
        })
    }

    /// Distinguishes "no such path" from "wrong method for this path".
    fn other_methods(&self, method: &str, path: &str) -> RouteError {
        let mut allowed: Vec<&str> = self
            .methods
            .iter()
            .filter(|m| *m != method)
            .filter(|m| self.by_method[*m].at(path).is_ok())
            .map(String::as_str)
            .collect();

        if allowed.is_empty() {
            return RouteError::NotFound;
        }
        allowed.sort_unstable();
        RouteError::MethodNotAllowed(allowed.join(", "))
    }
}

/// Percent-decode one path segment.
///
/// Query values have always been decoded, by `form_urlencoded`; path
/// parameters were not, so the same framework handed a handler `a%20b` from a
/// path and `a b` from a query. Decoded here, lossily on invalid UTF-8,
/// matching what the query side already does.
///
/// `+` is deliberately left alone. It means a space in a query string and a
/// literal plus everywhere else in a URL.
///
/// Routing has already happened against the raw path when this runs, so a
/// `%2F` that decodes to a slash cannot change which route was chosen. A
/// catch-all is decoded the same way, which means `%2E%2E` arrives as `..`;
/// a handler that turns one into a filesystem path has to sanitise it, as it
/// would with the unencoded form that was always possible.
fn decode_path(raw: &str) -> Cow<'_, str> {
    // No escape, no work: the overwhelmingly common case allocates nothing.
    if !raw.contains('%') {
        return Cow::Borrowed(raw);
    }
    percent_encoding::percent_decode_str(raw).decode_utf8_lossy()
}

fn coerce(raw: &str, param: &ParamSpec) -> Result<ParamValue, ParamError> {
    let bad = || ParamError {
        source: param.source.label(),
        name: param.name.clone(),
        error_type: match param.source {
            Source::Path => "path_param_parsing",
            Source::Query => "query_param_parsing",
        },
        msg: format!("Input should be a valid {}", param.kind.label()),
        input: Some(raw.to_owned()),
    };
    match param.kind {
        ParamKind::Str => Ok(ParamValue::Str(raw.to_owned())),
        ParamKind::Int => raw.parse().map(ParamValue::Int).map_err(|_| bad()),
        ParamKind::Float => raw
            .parse::<f64>()
            .ok()
            .filter(|v| v.is_finite())
            .map(ParamValue::Float)
            .ok_or_else(bad),
        ParamKind::Bool => match raw {
            "true" | "True" | "1" | "yes" | "on" => Ok(ParamValue::Bool(true)),
            "false" | "False" | "0" | "no" | "off" => Ok(ParamValue::Bool(false)),
            _ => Err(bad()),
        },
        // Canonicalised, not passed through. Rust accepts forms Python's
        // constructors might not, and a value that parsed here must never
        // raise there: that would be a 500 for input already judged valid.
        ParamKind::Uuid => uuid::Uuid::try_parse(raw)
            .map(|parsed| ParamValue::Uuid(parsed.hyphenated().to_string()))
            .map_err(|_| bad()),
        // Parsed properly rather than shape-checked, so 2026-02-30 is rejected
        // here instead of raising inside a handler.
        ParamKind::Date => time::Date::parse(raw, &ISO)
            .ok()
            .and_then(|date| date.format(&ISO_DATE).ok())
            .map(ParamValue::Date)
            .ok_or_else(bad),
        ParamKind::DateTime => time::OffsetDateTime::parse(raw, &ISO)
            .ok()
            .and_then(|at| at.format(&ISO_OFFSET).ok())
            .or_else(|| {
                time::PrimitiveDateTime::parse(raw, &ISO)
                    .ok()
                    .and_then(|at| at.format(&ISO_NAIVE).ok())
            })
            .map(ParamValue::DateTime)
            .ok_or_else(bad),
    }
}
