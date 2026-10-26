//! Form bodies: `application/x-www-form-urlencoded` and `multipart/form-data`.
//!
//! Parsed on demand, on the worker thread, when a handler calls
//! `request.form()`, so a route that never reads a form pays nothing. The body
//! is already whole and bounded by `max_body` by the time it gets here. A
//! multipart body too large to hold is parsed as it arrives by `PartReader`,
//! with the same parser.

use std::collections::VecDeque;
use std::convert::Infallible;
use std::future::Future;
use std::pin::{pin, Pin};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Waker};

use bytes::Bytes;
use futures_util::Stream;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

pub enum Part {
    Field {
        name: String,
        value: String,
    },
    File {
        name: String,
        filename: String,
        content_type: Option<String>,
        data: Vec<u8>,
    },
}

pub enum FormError {
    /// Not a form content type at all.
    Unsupported,
    Malformed(String),
    TooManyParts(usize),
}

/// Parse a body according to its content type.
pub fn parse(
    content_type: Option<&str>,
    body: &[u8],
    max_parts: usize,
) -> Result<Vec<Part>, FormError> {
    let content_type = content_type.ok_or(FormError::Unsupported)?;
    let essence = content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    match essence.as_str() {
        "application/x-www-form-urlencoded" => urlencoded(body, max_parts),
        "multipart/form-data" => multipart(content_type, body, max_parts),
        _ => Err(FormError::Unsupported),
    }
}

fn urlencoded(body: &[u8], max_parts: usize) -> Result<Vec<Part>, FormError> {
    let mut parts = Vec::new();
    for (name, value) in form_urlencoded::parse(body) {
        if parts.len() == max_parts {
            return Err(FormError::TooManyParts(max_parts));
        }
        parts.push(Part::Field {
            name: name.into_owned(),
            value: value.into_owned(),
        });
    }
    Ok(parts)
}

fn multipart(content_type: &str, body: &[u8], max_parts: usize) -> Result<Vec<Part>, FormError> {
    let boundary =
        multer::parse_boundary(content_type).map_err(|e| FormError::Malformed(e.to_string()))?;
    let stream = futures_util::stream::once(std::future::ready(Ok::<Bytes, Infallible>(
        Bytes::copy_from_slice(body),
    )));
    let mut reader = multer::Multipart::new(stream, boundary);

    run_ready(async move {
        let mut parts = Vec::new();
        while let Some(field) = reader
            .next_field()
            .await
            .map_err(|e| FormError::Malformed(e.to_string()))?
        {
            if parts.len() == max_parts {
                return Err(FormError::TooManyParts(max_parts));
            }
            let name = field
                .name()
                .ok_or_else(|| FormError::Malformed("a part has no name".into()))?
                .to_owned();
            let filename = field.file_name().map(str::to_owned);
            let content_type = field.content_type().map(|m| m.to_string());
            let data = field
                .bytes()
                .await
                .map_err(|e| FormError::Malformed(e.to_string()))?;
            parts.push(match filename {
                Some(filename) => Part::File {
                    name,
                    filename,
                    content_type,
                    data: data.to_vec(),
                },
                // Undecodable text becomes replacement characters, as it does
                // in a query string, rather than failing the whole form.
                None => Part::Field {
                    name,
                    value: String::from_utf8_lossy(&data).into_owned(),
                },
            });
        }
        Ok(parts)
    })
}

/// Drive a future whose every input is already in memory.
///
/// multer is written against an async stream; here the stream is one buffer
/// that is ready immediately, so the future can never be waiting on anything.
/// Polling it to completion with a no-op waker avoids pulling in an executor,
/// or blocking on the tokio runtime from a Python worker thread.
fn run_ready<T>(future: impl Future<Output = Result<T, FormError>>) -> Result<T, FormError> {
    let mut future = pin!(future);
    let mut context = Context::from_waker(Waker::noop());
    match future.as_mut().poll(&mut context) {
        Poll::Ready(result) => result,
        Poll::Pending => Err(FormError::Malformed(
            "form parser waited on input that should already be present".into(),
        )),
    }
}

/// Chunks handed over from Python, waiting for multer to read them.
#[derive(Default)]
struct Feed {
    chunks: VecDeque<Bytes>,
    done: bool,
}

/// What multer reads from: whatever Python has fed so far. With nothing
/// queued it is `Pending` and registers no waker, because the only thing that
/// can make progress is Python feeding another chunk and polling again.
struct FeedStream(Arc<Mutex<Feed>>);

impl Stream for FeedStream {
    type Item = Result<Bytes, Infallible>;

    fn poll_next(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let mut feed = self.0.lock().unwrap_or_else(|e| e.into_inner());
        match feed.chunks.pop_front() {
            Some(chunk) => Poll::Ready(Some(Ok(chunk))),
            None if feed.done => Poll::Ready(None),
            None => Poll::Pending,
        }
    }
}

struct Reading {
    multipart: multer::Multipart<'static>,
    /// The part being read. Dropped before asking for the next one, which
    /// multer requires, and which makes it skip what was left unread.
    field: Option<multer::Field<'static>>,
    parts: usize,
    max_parts: usize,
}

/// A multipart body parsed as it arrives: `BodyStream.parts()`.
///
/// The same parser as `request.form()`, so a form is accepted or refused
/// alike whether it was buffered or streamed. Python pulls chunks from the
/// body and feeds them in; each call here polls multer once with a no-op
/// waker and answers a part, a chunk, "need more", or the end. Every call is
/// state (0 ready, 1 pending, 2 end) and a value, as the body reader's are.
#[pyclass(frozen)]
pub struct PartReader {
    feed: Arc<Mutex<Feed>>,
    reading: Mutex<Reading>,
}

const READY: u8 = 0;
const PENDING: u8 = 1;
const END: u8 = 2;

fn http_error(py: Python<'_>, status: u16, detail: String) -> PyErr {
    match py
        .import("oxbrook._errors")
        .and_then(|m| m.getattr("HTTPError"))
        .and_then(|cls| cls.call1((status, detail)))
    {
        Ok(exc) => PyErr::from_value(exc),
        Err(e) => e,
    }
}

impl PartReader {
    fn reading(&self) -> std::sync::MutexGuard<'_, Reading> {
        // One handler reads one body, so this is never contended; a poisoned
        // lock means a panic mid-parse, and the state is no worse than torn.
        self.reading.lock().unwrap_or_else(|e| e.into_inner())
    }
}

enum Next {
    Part {
        name: String,
        filename: Option<String>,
        content_type: Option<String>,
        headers: Vec<(String, String)>,
    },
    Pending,
    End,
    TooMany(usize),
    Malformed(String),
}

#[pymethods]
impl PartReader {
    #[new]
    #[pyo3(signature = (content_type, max_parts = 1000))]
    fn new(py: Python<'_>, content_type: Option<&str>, max_parts: usize) -> PyResult<Self> {
        let essence = content_type
            .and_then(|c| c.split(';').next())
            .map(|e| e.trim().to_ascii_lowercase());
        if essence.as_deref() != Some("multipart/form-data") {
            return Err(http_error(
                py,
                415,
                "expected a multipart/form-data body".into(),
            ));
        }
        let boundary = multer::parse_boundary(content_type.unwrap_or_default())
            .map_err(|e| http_error(py, 400, format!("malformed form body: {e}")))?;
        let feed = Arc::new(Mutex::new(Feed::default()));
        Ok(PartReader {
            reading: Mutex::new(Reading {
                multipart: multer::Multipart::new(FeedStream(feed.clone()), boundary),
                field: None,
                parts: 0,
                max_parts,
            }),
            feed,
        })
    }

    /// Hand over the next chunk of the body.
    fn feed(&self, chunk: &[u8]) {
        let chunk = Bytes::copy_from_slice(chunk);
        let mut feed = self.feed.lock().unwrap_or_else(|e| e.into_inner());
        feed.chunks.push_back(chunk);
    }

    /// The body has no more chunks.
    fn finish(&self) {
        self.feed.lock().unwrap_or_else(|e| e.into_inner()).done = true;
    }

    /// The next part's (name, filename, content_type, headers), skipping
    /// whatever of the current part was not read.
    fn next_part<'py>(&self, py: Python<'py>) -> PyResult<(u8, Bound<'py, PyAny>)> {
        let next = py.detach(|| {
            let mut reading = self.reading();
            reading.field = None;
            let mut context = Context::from_waker(Waker::noop());
            match reading.multipart.poll_next_field(&mut context) {
                Poll::Pending => Next::Pending,
                Poll::Ready(Ok(None)) => Next::End,
                Poll::Ready(Err(e)) => Next::Malformed(e.to_string()),
                Poll::Ready(Ok(Some(field))) => {
                    if reading.parts == reading.max_parts {
                        return Next::TooMany(reading.max_parts);
                    }
                    reading.parts += 1;
                    let Some(name) = field.name().map(str::to_owned) else {
                        return Next::Malformed("a part has no name".into());
                    };
                    let next = Next::Part {
                        name,
                        filename: field.file_name().map(str::to_owned),
                        content_type: field.content_type().map(|m| m.to_string()),
                        headers: field
                            .headers()
                            .iter()
                            .map(|(k, v)| {
                                (
                                    k.as_str().to_owned(),
                                    String::from_utf8_lossy(v.as_bytes()).into_owned(),
                                )
                            })
                            .collect(),
                    };
                    reading.field = Some(field);
                    next
                }
            }
        });
        match next {
            Next::Part {
                name,
                filename,
                content_type,
                headers,
            } => Ok((
                READY,
                (name, filename, content_type, headers)
                    .into_pyobject(py)?
                    .into_any(),
            )),
            Next::Pending => Ok((PENDING, py.None().into_bound(py))),
            Next::End => Ok((END, py.None().into_bound(py))),
            Next::TooMany(limit) => Err(http_error(
                py,
                413,
                format!("form has more than {limit} parts"),
            )),
            Next::Malformed(reason) => Err(http_error(
                py,
                400,
                format!("malformed form body: {reason}"),
            )),
        }
    }

    /// The current part's next chunk of data.
    fn read<'py>(&self, py: Python<'py>) -> PyResult<(u8, Bound<'py, PyAny>)> {
        let polled = py.detach(|| {
            let mut reading = self.reading();
            let Some(field) = reading.field.as_mut() else {
                return Poll::Ready(None);
            };
            let mut context = Context::from_waker(Waker::noop());
            Pin::new(field).poll_next(&mut context)
        });
        match polled {
            Poll::Pending => Ok((PENDING, py.None().into_bound(py))),
            Poll::Ready(None) => Ok((END, py.None().into_bound(py))),
            Poll::Ready(Some(Ok(chunk))) => Ok((READY, PyBytes::new(py, &chunk).into_any())),
            Poll::Ready(Some(Err(e))) => {
                Err(http_error(py, 400, format!("malformed form body: {e}")))
            }
        }
    }
}
