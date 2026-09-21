//! Static files, served from Rust without waking a Python worker.
//!
//! A mount maps a URL prefix to a directory. Every request under the prefix is
//! answered here: the path is resolved inside the directory or refused, the file
//! is streamed from disk in chunks, and conditional and range requests are
//! answered from its metadata.
//!
//! **Refused, always, with a 404**, so a refusal says nothing about what exists:
//! a `..` or `.` segment in any encoding, a backslash or NUL, a path that
//! resolves outside the directory through a symlink, and a directory with no
//! index. Dotfiles are refused unless the mount allows them, because the file
//! most likely to be sitting in a static directory by accident is `.env`.

use std::convert::Infallible;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use bytes::Bytes;
use http_body_util::{BodyExt, StreamBody};
use hyper::body::Frame;
use hyper::header::{
    HeaderMap, HeaderValue, ACCEPT_RANGES, CACHE_CONTROL, CONTENT_LENGTH, CONTENT_RANGE,
    CONTENT_TYPE, ETAG, IF_MODIFIED_SINCE, IF_NONE_MATCH, IF_RANGE, LAST_MODIFIED, LOCATION, RANGE,
};
use hyper::{Response, StatusCode};
use tokio::io::AsyncReadExt;

use crate::server::{full, Out};

/// (prefix, directory, index, fallback, dotfiles, cache_control) from
/// `oxbrook._files.StaticMount`.
pub type MountTuple = (
    String,
    String,
    Option<String>,
    Option<String>,
    bool,
    Option<String>,
);

const CHUNK: usize = 64 * 1024;

pub struct Mount {
    prefix: String,
    root: PathBuf,
    index: Option<String>,
    fallback: Option<String>,
    dotfiles: bool,
    cache_control: Option<HeaderValue>,
}

impl Mount {
    pub fn build(spec: MountTuple) -> Result<Self, String> {
        let (prefix, directory, index, fallback, dotfiles, cache_control) = spec;
        // Canonical, so a resolved file can be checked against it by prefix:
        // a symlink inside the directory pointing outside it is caught there.
        let root = std::fs::canonicalize(&directory)
            .map_err(|e| format!("static directory {directory}: {e}"))?;
        let cache_control = cache_control
            .map(|v| HeaderValue::from_str(&v).map_err(|_| format!("bad cache_control {v:?}")))
            .transpose()?;
        Ok(Self {
            prefix,
            root,
            index,
            fallback,
            dotfiles,
            cache_control,
        })
    }

    /// Router patterns for this mount: the prefix itself and everything under
    /// it, captured as `file`.
    pub fn patterns(&self) -> Vec<(String, bool)> {
        if self.prefix == "/" {
            vec![("/".to_owned(), false), ("/{*file}".to_owned(), true)]
        } else {
            vec![
                (self.prefix.clone(), false),
                (format!("{}/{{*file}}", self.prefix), true),
            ]
        }
    }
}

fn not_found() -> Response<Out> {
    Response::builder()
        .status(StatusCode::NOT_FOUND)
        .header(CONTENT_TYPE, "text/plain")
        .body(full(Bytes::from_static(b"not found")))
        .unwrap()
}

fn redirect(to: String) -> Response<Out> {
    Response::builder()
        .status(StatusCode::PERMANENT_REDIRECT)
        .header(LOCATION, to)
        .body(full(Bytes::new()))
        .unwrap_or_else(|_| not_found())
}

/// Names Windows resolves to a device rather than to a file. `CON.txt` is the
/// console too, so the name is judged up to its first dot.
const DEVICES: &[&str] = &[
    "con", "prn", "aux", "nul", "conin$", "conout$", "com1", "com2", "com3", "com4", "com5",
    "com6", "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8",
    "lpt9",
];

/// Segments Windows turns into something other than a file under the mount,
/// inside the OS rather than in any path that could be inspected first: a name
/// with a trailing dot or space is trimmed to a different file, and a device
/// name opens a device, which can block rather than fail. A drive-relative
/// segment like `C:evil` escapes the join outright, and is refused with the
/// other colons below.
///
/// Refused on every platform, so that a mount answers the same everywhere and
/// the tests that prove it run everywhere.
fn windows_hazard(segment: &str) -> bool {
    if segment.ends_with('.') || segment.ends_with(' ') {
        return true;
    }
    let stem = segment.split('.').next().unwrap_or(segment);
    DEVICES
        .iter()
        .any(|device| stem.eq_ignore_ascii_case(device))
}

/// The requested path as segments under the mount, or None if it must be
/// refused. Percent-decoding happens here, before inspection, so `%2e%2e` is
/// seen as the `..` it is and `%2f` as the separator it is.
fn segments(raw: &str, dotfiles: bool) -> Option<Vec<String>> {
    let decoded = percent_encoding::percent_decode_str(raw)
        .decode_utf8()
        .ok()?
        .into_owned();
    let mut out = Vec::new();
    for segment in decoded.split('/') {
        if segment.is_empty() {
            continue;
        }
        if segment == "." || segment == ".." || segment.contains(['\\', '\0', ':']) {
            return None;
        }
        if windows_hazard(segment) {
            return None;
        }
        if segment.starts_with('.') && !dotfiles {
            return None;
        }
        out.push(segment.to_owned());
    }
    Some(out)
}

fn etag_for(len: u64, modified: SystemTime) -> String {
    let nanos = modified
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("W/\"{len:x}-{nanos:x}\"")
}

/// `If-None-Match` compares weakly: a tag matches with or without `W/`.
fn matches_etag(header: &str, etag: &str) -> bool {
    let bare = etag.trim_start_matches("W/");
    header
        .split(',')
        .map(str::trim)
        .any(|candidate| candidate == "*" || candidate.trim_start_matches("W/") == bare)
}

/// `bytes=a-b`, `bytes=a-` or `bytes=-n` against a length. `Ok(None)` means
/// serve the whole file — no range, or several, which a server may ignore.
/// `Err(())` means the range cannot be satisfied.
fn parse_range(header: &str, len: u64) -> Result<Option<(u64, u64)>, ()> {
    let Some(spec) = header.trim().strip_prefix("bytes=") else {
        return Ok(None);
    };
    if spec.contains(',') {
        return Ok(None);
    }
    let (start, end) = spec.split_once('-').ok_or(())?;
    let (start, end) = (start.trim(), end.trim());
    let range = if start.is_empty() {
        let suffix: u64 = end.parse().map_err(|_| ())?;
        if suffix == 0 || len == 0 {
            return Err(());
        }
        (len.saturating_sub(suffix), len - 1)
    } else {
        let start: u64 = start.parse().map_err(|_| ())?;
        let end: u64 = if end.is_empty() {
            len.saturating_sub(1)
        } else {
            end.parse::<u64>()
                .map_err(|_| ())?
                .min(len.saturating_sub(1))
        };
        (start, end)
    };
    if len == 0 || range.0 >= len || range.0 > range.1 {
        return Err(());
    }
    Ok(Some(range))
}

fn content_type(path: &Path) -> String {
    let guess = mime_guess::from_path(path).first_or_octet_stream();
    let essence = guess.essence_str();
    // A text type without a charset leaves the browser guessing, and a guess
    // of anything but UTF-8 garbles every non-ASCII character.
    if guess.type_() == "text"
        || essence == "application/javascript"
        || essence == "application/json"
    {
        format!("{essence}; charset=utf-8")
    } else {
        essence.to_owned()
    }
}

/// Characters left as they are when a segment goes back into a redirect.
const SEGMENT: &percent_encoding::AsciiSet = &percent_encoding::CONTROLS
    .add(b' ')
    .add(b'"')
    .add(b'#')
    .add(b'<')
    .add(b'>')
    .add(b'?')
    .add(b'`')
    .add(b'{')
    .add(b'}')
    .add(b'%')
    .add(b'/');

/// Files up to this size are read whole in the same blocking call that finds
/// them; larger ones are streamed.
const READ_WHOLE: u64 = 256 * 1024;

/// The request headers an answer depends on, copied out so the work can move
/// to a blocking thread without holding the request.
struct Asked {
    if_none_match: Option<String>,
    if_modified_since: Option<String>,
    range: Option<String>,
    if_range: Option<String>,
    wants_html: bool,
    head: bool,
}

impl Asked {
    fn from(headers: &HeaderMap, head: bool) -> Self {
        let text = |name| {
            headers
                .get(name)
                .and_then(|v: &HeaderValue| v.to_str().ok())
                .map(str::to_owned)
        };
        Self {
            if_none_match: text(IF_NONE_MATCH),
            if_modified_since: text(IF_MODIFIED_SINCE),
            range: text(RANGE),
            if_range: text(IF_RANGE),
            wants_html: headers
                .get(hyper::header::ACCEPT)
                .and_then(|v| v.to_str().ok())
                .is_some_and(|accept| accept.contains("text/html")),
            head,
        }
    }
}

enum Outcome {
    Ready(Response<Out>),
    Stream {
        builder: hyper::http::response::Builder,
        file: std::fs::File,
        length: u64,
    },
}

/// Answer one request under a mount.
///
/// `file` is the part of the path after the prefix, still percent-encoded, or
/// None when the request named the prefix itself. `path` and `query` are the
/// request's own, for building a redirect.
///
/// Every filesystem call for a request happens in one blocking task. tokio's
/// async file API sends each call — stat, canonicalize, open, metadata, read —
/// to the blocking pool separately, and five handoffs per request measured a
/// small static file at 22,670 req/s, a seventh of a Python handler returning
/// the same bytes from memory.
pub async fn serve(
    mount: Arc<Mount>,
    file: Option<&str>,
    path: &str,
    query: Option<&str>,
    headers: &HeaderMap,
    head: bool,
) -> Response<Out> {
    // `/assets` names a directory, and relative links inside its index only
    // resolve against `/assets/`.
    let (parts, trailing_slash) = match file {
        None if mount.prefix != "/" => {
            let mut to = format!("{path}/");
            if let Some(query) = query {
                to.push('?');
                to.push_str(query);
            }
            return redirect(to);
        }
        None => (Vec::new(), true),
        Some(raw) => match segments(raw, mount.dotfiles) {
            Some(parts) => (parts, raw.is_empty() || raw.ends_with('/')),
            None => return not_found(),
        },
    };

    let asked = Asked::from(headers, head);
    let query = query.map(str::to_owned);
    let outcome = tokio::task::spawn_blocking(move || {
        resolve(&mount, parts, trailing_slash, query.as_deref(), &asked)
    })
    .await;

    match outcome {
        Ok(Outcome::Ready(response)) => response,
        Ok(Outcome::Stream {
            builder,
            file,
            length,
        }) => stream(builder, file, length),
        Err(_) => not_found(),
    }
}

fn resolve(
    mount: &Mount,
    parts: Vec<String>,
    trailing_slash: bool,
    query: Option<&str>,
    asked: &Asked,
) -> Outcome {
    let mut candidate = mount.root.clone();
    for part in &parts {
        candidate.push(part);
    }

    let target = match std::fs::metadata(&candidate) {
        Ok(meta) if meta.is_dir() => {
            let Some(index) = &mount.index else {
                return Outcome::Ready(not_found());
            };
            if !trailing_slash {
                // Only redirect where there is something to land on.
                if std::fs::metadata(candidate.join(index)).is_err() {
                    return Outcome::Ready(not_found());
                }
                let mut to = String::from(if mount.prefix == "/" {
                    ""
                } else {
                    &mount.prefix
                });
                for part in &parts {
                    to.push('/');
                    to.push_str(&percent_encoding::utf8_percent_encode(part, SEGMENT).to_string());
                }
                to.push('/');
                // Kept, as for the bare prefix: `/docs?page=2` means `/docs/?page=2`.
                if let Some(query) = query {
                    to.push('?');
                    to.push_str(query);
                }
                return Outcome::Ready(redirect(to));
            }
            candidate.join(index)
        }
        Ok(_) => candidate,
        Err(_) => {
            // A single-page app routes in the browser, so a browser loading an
            // unknown page — no extension, and asking for HTML — gets the app.
            // A missing `app.js` does not, and neither does an API call: with the
            // app mounted at `/`, `fetch("/api/usrs")` answered with index.html
            // and 200 would fail in the client, far from the typo. Browsers send
            // `text/html` when loading a page; fetch and API clients do not.
            let looks_like_a_page = parts.last().is_none_or(|last| !last.contains('.'));
            match &mount.fallback {
                Some(fallback) if looks_like_a_page && asked.wants_html => {
                    mount.root.join(fallback)
                }
                _ => return Outcome::Ready(not_found()),
            }
        }
    };

    let Some(resolved) = contained(&mount.root, &target) else {
        return Outcome::Ready(not_found());
    };
    let Ok(file) = std::fs::File::open(&resolved) else {
        return Outcome::Ready(not_found());
    };
    let Ok(meta) = file.metadata() else {
        return Outcome::Ready(not_found());
    };
    if !meta.is_file() {
        return Outcome::Ready(not_found());
    }
    respond(mount, file, &resolved, meta, asked)
}

/// `target` if it stays inside `root` once symlinks are followed.
///
/// Only the components below `root` are checked, one `lstat` each, and the
/// full canonical path is computed only when one of them is a symlink. The
/// alternative, canonicalising every request, walks every directory from `/`
/// down: about nine lookups for a typical path, and most of the cost of
/// serving a small file.
fn contained(root: &Path, target: &Path) -> Option<PathBuf> {
    let relative = target.strip_prefix(root).ok()?;
    let mut walked = root.to_path_buf();
    for component in relative.components() {
        walked.push(component);
        let is_link = std::fs::symlink_metadata(&walked)
            .ok()?
            .file_type()
            .is_symlink();
        if is_link {
            let resolved = std::fs::canonicalize(target).ok()?;
            return resolved.starts_with(root).then_some(resolved);
        }
    }
    Some(target.to_path_buf())
}

fn respond(
    mount: &Mount,
    mut file: std::fs::File,
    path: &Path,
    meta: std::fs::Metadata,
    asked: &Asked,
) -> Outcome {
    use std::io::{Read, Seek};

    let len = meta.len();
    let modified = meta.modified().unwrap_or(UNIX_EPOCH);
    let etag = etag_for(len, modified);
    let last_modified = httpdate::fmt_http_date(modified);

    let mut builder = Response::builder()
        .header(ETAG, &etag)
        .header(LAST_MODIFIED, &last_modified)
        .header(ACCEPT_RANGES, "bytes");
    if let Some(cache) = &mount.cache_control {
        builder = builder.header(CACHE_CONTROL, cache.clone());
    }

    // If-None-Match takes precedence over If-Modified-Since when both arrive.
    let unchanged = match &asked.if_none_match {
        Some(tags) => matches_etag(tags, &etag),
        None => asked
            .if_modified_since
            .as_deref()
            .and_then(|v| httpdate::parse_http_date(v).ok())
            .is_some_and(|since| {
                // HTTP dates have whole-second precision.
                let secs = |t: SystemTime| {
                    t.duration_since(UNIX_EPOCH)
                        .map(|d| d.as_secs())
                        .unwrap_or(0)
                };
                secs(modified) <= secs(since)
            }),
    };
    if unchanged {
        return Outcome::Ready(
            builder
                .status(StatusCode::NOT_MODIFIED)
                .body(full(Bytes::new()))
                .unwrap_or_else(|_| not_found()),
        );
    }

    builder = builder.header(CONTENT_TYPE, content_type(path));

    // A Range is honoured only when If-Range, if sent, still names this file.
    let range_applies = asked
        .if_range
        .as_deref()
        .is_none_or(|condition| condition == etag || condition == last_modified);
    let range = match &asked.range {
        Some(header) if range_applies => parse_range(header, len),
        _ => Ok(None),
    };

    let (status, start, end) = match range {
        Ok(Some((start, end))) => {
            builder = builder.header(CONTENT_RANGE, format!("bytes {start}-{end}/{len}"));
            (StatusCode::PARTIAL_CONTENT, start, end)
        }
        Ok(None) => (StatusCode::OK, 0, len.saturating_sub(1)),
        Err(()) => {
            return Outcome::Ready(
                builder
                    .status(StatusCode::RANGE_NOT_SATISFIABLE)
                    .header(CONTENT_RANGE, format!("bytes */{len}"))
                    .body(full(Bytes::new()))
                    .unwrap_or_else(|_| not_found()),
            );
        }
    };
    let length = if len == 0 { 0 } else { end - start + 1 };
    builder = builder.status(status).header(CONTENT_LENGTH, length);

    if asked.head || length == 0 {
        return Outcome::Ready(
            builder
                .body(full(Bytes::new()))
                .unwrap_or_else(|_| not_found()),
        );
    }
    if start > 0 && file.seek(std::io::SeekFrom::Start(start)).is_err() {
        return Outcome::Ready(not_found());
    }
    if length <= READ_WHOLE {
        let mut body = Vec::with_capacity(length as usize);
        if file.take(length).read_to_end(&mut body).is_err() {
            return Outcome::Ready(not_found());
        }
        return Outcome::Ready(
            builder
                .body(full(Bytes::from(body)))
                .unwrap_or_else(|_| not_found()),
        );
    }
    Outcome::Stream {
        builder,
        file,
        length,
    }
}

/// Stream a large file as hyper sends it, so it is never held in memory and a
/// slow client holds one chunk, not the file.
fn stream(
    builder: hyper::http::response::Builder,
    file: std::fs::File,
    length: u64,
) -> Response<Out> {
    let file = tokio::fs::File::from_std(file);
    let body = futures_util::stream::unfold((file, length), |(mut file, remaining)| async move {
        if remaining == 0 {
            return None;
        }
        let mut buffer = vec![0u8; CHUNK.min(remaining as usize)];
        match file.read(&mut buffer).await {
            Ok(0) | Err(_) => None,
            Ok(n) => {
                buffer.truncate(n);
                Some((
                    Ok::<_, Infallible>(Frame::data(Bytes::from(buffer))),
                    (file, remaining - n as u64),
                ))
            }
        }
    });
    builder
        .body(StreamBody::new(body).boxed())
        .unwrap_or_else(|_| not_found())
}
