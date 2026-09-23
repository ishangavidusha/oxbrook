//! RFC 9457 problem details, for the errors Rust answers without Python.
//!
//! No such route, wrong method, a parameter that will not coerce, a body over
//! the limit, no capacity, a timeout: all answered here, on a tokio thread,
//! before or instead of a handler. They use the same shape Python writes for
//! an `HTTPError`, so a client parses one error format whichever side answered.

use bytes::Bytes;
use hyper::header::CONTENT_TYPE;
use hyper::{Response, StatusCode};

use crate::server::{full, Out};

pub const CONTENT_TYPE_PROBLEM: &str = "application/problem+json";

/// The status's name, as RFC 9110 gives it.
///
/// With `type` left as `about:blank`, RFC 9457 asks for the status phrase as
/// the title. The `http` crate predates RFC 9110 for two of the statuses
/// answered here, and Python's `http.HTTPStatus` does not, so without this a
/// 422 would be titled differently depending on which side refused it.
pub fn title(status: StatusCode) -> &'static str {
    match status.as_u16() {
        413 => "Content Too Large",
        422 => "Unprocessable Content",
        _ => status.canonical_reason().unwrap_or("Error"),
    }
}

/// The JSON body. `errors` is the extension member a 422 carries its
/// individual failures in, in the shape pydantic uses for body errors.
///
/// Written member by member rather than through `json!`, whose map sorts its
/// keys: RFC order puts `type` first, and Python writes it in that order too.
pub fn body(status: StatusCode, detail: Option<&str>, errors: Option<&[u8]>) -> Vec<u8> {
    use std::io::Write;

    let mut out = Vec::with_capacity(128);
    out.extend_from_slice(br#"{"type":"about:blank","title":"#);
    let _ = serde_json::to_writer(&mut out, title(status));
    let _ = write!(out, r#","status":{}"#, status.as_u16());
    if let Some(detail) = detail {
        out.extend_from_slice(br#","detail":"#);
        let _ = serde_json::to_writer(&mut out, detail);
    }
    if let Some(errors) = errors {
        // Already JSON: an array the caller wrote.
        out.extend_from_slice(br#","errors":"#);
        out.extend_from_slice(errors);
    }
    out.push(b'}');
    out
}

/// A complete problem response with no extension members.
pub fn response(status: StatusCode, detail: Option<&str>) -> Response<Out> {
    with_body(status, body(status, detail, None))
}

pub fn with_body(status: StatusCode, body: Vec<u8>) -> Response<Out> {
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, CONTENT_TYPE_PROBLEM)
        .body(full(Bytes::from(body)))
        .unwrap()
}
