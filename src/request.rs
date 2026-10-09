use hyper::header::{HeaderMap, HeaderName, HeaderValue};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};

use crate::form::{self, FormError, Part};

/// Immutable view of an incoming HTTP request, handed to the Python handler.
/// `frozen` means no Python-side mutation, so no locking is needed even on
/// free-threaded builds.
#[pyclass(frozen, name = "Request", module = "oxbrook._core")]
pub struct Request {
    pub method: String,
    pub path: String,
    pub query: Option<String>,
    pub body: Vec<u8>,
    /// Kept as hyper's own map rather than converted up front. Most handlers
    /// never look at a header, and converting every one into Python strings on
    /// every request would be paid by all of them.
    pub headers: HeaderMap,
    /// The worker loop's `WorkerContext`, shared by every request on it.
    /// None for a request built by hand with no context passed.
    pub context: Option<Py<PyAny>>,
    /// The incremental body, for a route that declared a `BodyStream`.
    pub stream: Option<std::sync::Arc<crate::body::BodyShared>>,
    /// `request.locals`, made on first use. Most requests never touch it, so
    /// it costs nothing until one does; a once-lock rather than a Mutex
    /// because it is written exactly once and read many times after.
    pub locals: PyOnceLock<Py<PyDict>>,
    /// The body is in `stream` because the route deferred it until after
    /// authentication, and `body` is empty until `filled` is set.
    pub deferred: bool,
    /// The whole body, once read from `stream`. Written once, like `locals`.
    pub filled: std::sync::OnceLock<Vec<u8>>,
    /// For a socket, the key its gate left the authenticated caller under.
    pub handoff: Option<String>,
    /// The client's address, through any trusted proxies. None for a request
    /// built by hand without one.
    pub client: Option<std::net::IpAddr>,
}

impl Request {
    /// The body, wherever it is. A deferred body that has not been read yet
    /// is an error rather than empty bytes: code that parsed `b""` would fail
    /// somewhere far from the reason.
    fn bytes(&self) -> PyResult<&[u8]> {
        if let Some(filled) = self.filled.get() {
            return Ok(filled);
        }
        if self.deferred {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "the request body has not been read yet: this route authenticates \
                 the caller before reading it. Read it in the handler, or \
                 `await request.read()` to read it now",
            ));
        }
        Ok(&self.body)
    }
}

#[pymethods]
impl Request {
    /// Build one from Python.
    ///
    /// Needed because a capability invoked over MCP never came in over HTTP,
    /// but the handler it calls still expects a request. The test client uses
    /// it too.
    #[new]
    // Each argument is a keyword on the Python side, which is the interface
    // that matters; bundling them into a struct would only move the list.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (method = "GET".to_string(), path = "/".to_string(), query = None, body = None, headers = None, context = None, locals = None, client = None))]
    fn py_new(
        py: Python<'_>,
        method: String,
        path: String,
        query: Option<String>,
        body: Option<Vec<u8>>,
        headers: Option<Vec<(String, String)>>,
        context: Option<Py<PyAny>>,
        locals: Option<Py<PyDict>>,
        client: Option<String>,
    ) -> PyResult<Self> {
        let client = client
            .map(|c| {
                c.parse().map_err(|_| {
                    pyo3::exceptions::PyValueError::new_err(format!("bad client address {c:?}"))
                })
            })
            .transpose()?;
        let mut map = HeaderMap::new();
        for (name, value) in headers.unwrap_or_default() {
            if let (Ok(name), Ok(value)) = (
                HeaderName::from_bytes(name.as_bytes()),
                HeaderValue::from_str(&value),
            ) {
                map.append(name, value);
            }
        }
        // A tool call starts with a copy of the `/mcp` request's dict: app
        // middleware ran once, around that request, and what it left there is
        // for the tool as much as for anything else. A copy, because the tool
        // is a request of its own: who its route's authentication found is
        // the tool's answer, and must not overwrite the `/mcp` request's.
        let cell = PyOnceLock::new();
        if let Some(inherited) = locals {
            let _ = cell.set(py, inherited.bind(py).copy()?.unbind());
        }
        Ok(Self {
            method,
            path,
            query,
            body: body.unwrap_or_default(),
            headers: map,
            context,
            stream: None,
            locals: cell,
            deferred: false,
            filled: std::sync::OnceLock::new(),
            handoff: None,
            client,
        })
    }

    /// The app serving this request.
    #[getter]
    fn app<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        self.context
            .as_ref()
            .map(|context| context.bind(py).getattr("app"))
            .transpose()
    }

    /// Values yielded by the app's `lifespan` and `worker_lifespan`, read-only.
    #[getter]
    fn state<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        self.context
            .as_ref()
            .map(|context| context.bind(py).getattr("state"))
            .transpose()
    }

    /// Scratch space for this request alone: a plain dict, empty until
    /// something writes to it, gone when the request is.
    ///
    /// Where middleware leaves something for a handler or a dependency to
    /// read, such as the caller it authenticated. `state` is the wrong place
    /// for that: it is shared by every request on a worker loop, and
    /// read-only for that reason.
    #[getter]
    fn locals<'py>(&self, py: Python<'py>) -> Bound<'py, PyDict> {
        self.locals
            .get_or_init(py, || PyDict::new(py).unbind())
            .bind(py)
            .clone()
    }

    /// The worker context itself, so a request synthesized from this one —
    /// an MCP tool call — sees the same app and state.
    #[getter(_context)]
    fn context_handle(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.context.as_ref().map(|context| context.clone_ref(py))
    }

    /// The client's IP address, as a string.
    ///
    /// The address of whoever opened the connection, unless the app trusts
    /// it as a proxy (`App(trusted_proxies=...)`): then the nearest address in
    /// `X-Forwarded-For` that no trusted proxy wrote. Everything further left
    /// in that header is whatever the client chose to send, and is not
    /// believed. None for a request built by hand without one.
    #[getter]
    fn client(&self) -> Option<String> {
        self.client.map(|ip| ip.to_string())
    }

    /// The HTTP method, uppercase.
    #[getter]
    fn method(&self) -> &str {
        &self.method
    }

    /// The request path, without the query string.
    #[getter]
    fn path(&self) -> &str {
        &self.path
    }

    /// The raw query string, or None. Declared query parameters are already
    /// coerced and passed as handler arguments; this is for the rest.
    #[getter]
    fn query(&self) -> Option<&str> {
        self.query.as_deref()
    }

    /// The raw request body. A pydantic-annotated argument is the usual way
    /// to read a body; this is for handlers that parse it themselves.
    ///
    /// On a route with `auth=`, the body is read after the caller is
    /// authenticated, so middleware outside that check, and the check itself,
    /// find it unread: this raises there, and `await request.read()` reads it.
    #[getter]
    fn body<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        Ok(PyBytes::new(py, self.bytes()?))
    }

    /// The whole body, reading it first if it has not arrived yet.
    ///
    /// Returns an awaitable: `body = await request.read()`. Anywhere
    /// `request.body` would do, this does too; it is needed only where the
    /// body may not have been read, such as in an authentication scheme that
    /// checks a signature over it. Bounded by `max_body`, like any body.
    fn read<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        let py = slf.py();
        py.import("oxbrook._bodies")?
            .getattr("read_body")?
            .call1((slf,))
    }

    /// The reader for a body still to be read, or None.
    fn _reader(&self, py: Python<'_>) -> PyResult<Option<Py<crate::body::BodyReader>>> {
        match (&self.stream, self.filled.get()) {
            (Some(shared), None) => Ok(Some(Py::new(
                py,
                crate::body::BodyReader::new(shared.clone()),
            )?)),
            _ => Ok(None),
        }
    }

    /// Keep the body read by `read`. Once only.
    fn _fill(&self, body: Vec<u8>) -> PyResult<()> {
        self.filled.set(body).map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("the request body was already read")
        })
    }

    /// True while a body deferred for authentication has not been read.
    #[getter]
    fn _unread(&self) -> bool {
        self.deferred && self.filled.get().is_none()
    }

    /// The key a socket's gate left the authenticated caller under.
    #[getter]
    fn _handoff(&self) -> Option<&str> {
        self.handoff.as_deref()
    }

    /// One header by name, case-insensitively. None if absent.
    ///
    /// This is the cheap path: no dict is built, and a header that is not
    /// valid UTF-8 reads as absent rather than raising.
    #[pyo3(signature = (name, default = None))]
    fn header(&self, name: &str, default: Option<String>) -> Option<String> {
        self.headers
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
            .or(default)
    }

    /// Every header, lowercased. Repeated headers are joined with ", " as
    /// HTTP itself defines.
    #[getter]
    fn headers<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for name in self.headers.keys() {
            let joined = self
                .headers
                .get_all(name)
                .iter()
                .filter_map(|value| value.to_str().ok())
                .collect::<Vec<_>>()
                .join(", ");
            dict.set_item(name.as_str(), joined)?;
        }
        Ok(dict)
    }

    /// Cookies parsed from the `Cookie` header.
    #[getter]
    fn cookies<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for header in self.headers.get_all(hyper::header::COOKIE) {
            let Ok(raw) = header.to_str() else { continue };
            for pair in raw.split(';') {
                let pair = pair.trim();
                if pair.is_empty() {
                    continue;
                }
                // A cookie with no "=" is malformed; skip it rather than
                // inventing a name or a value for it.
                if let Some((name, value)) = pair.split_once('=') {
                    dict.set_item(name.trim(), value.trim())?;
                }
            }
        }
        Ok(dict)
    }

    /// Parse the body as a form, `application/x-www-form-urlencoded` or
    /// `multipart/form-data`, into a `FormData`.
    ///
    /// Parsed each time it is called, and only when it is called. Raises
    /// `HTTPError(415)` for a body that is not a form, `HTTPError(400)` for a
    /// malformed one and `HTTPError(413)` past `max_parts`, which bounds how
    /// many Python objects a single request can make the worker build.
    #[pyo3(signature = (max_parts = 1000))]
    fn form<'py>(&self, py: Python<'py>, max_parts: usize) -> PyResult<Bound<'py, PyAny>> {
        let content_type = self
            .headers
            .get(hyper::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned);
        let body = self.bytes()?;
        // Pure Rust over bytes already owned here, so other threads may run.
        let parsed = py.detach(|| form::parse(content_type.as_deref(), body, max_parts));

        let error = |status: u16, detail: String| -> PyErr {
            match py
                .import("oxbrook._errors")
                .and_then(|m| m.getattr("HTTPError"))
                .and_then(|cls| cls.call1((status, detail)))
            {
                Ok(exc) => PyErr::from_value(exc),
                Err(e) => e,
            }
        };

        let parts = match parsed {
            Ok(parts) => parts,
            Err(FormError::Unsupported) => {
                return Err(error(
                    415,
                    "expected a form body: application/x-www-form-urlencoded or \
                     multipart/form-data"
                        .into(),
                ))
            }
            Err(FormError::Malformed(reason)) => {
                return Err(error(400, format!("malformed form body: {reason}")))
            }
            Err(FormError::TooManyParts(limit)) => {
                return Err(error(413, format!("form has more than {limit} parts")))
            }
        };

        let list = PyList::empty(py);
        for part in parts {
            let item = match part {
                Part::Field { name, value } => PyTuple::new(
                    py,
                    [
                        name.into_pyobject(py)?.into_any(),
                        value.into_pyobject(py)?.into_any(),
                    ],
                )?,
                Part::File {
                    name,
                    filename,
                    content_type,
                    data,
                } => PyTuple::new(
                    py,
                    [
                        name.into_pyobject(py)?.into_any(),
                        filename.into_pyobject(py)?.into_any(),
                        content_type.into_pyobject(py)?.into_any(),
                        PyBytes::new(py, &data).into_any(),
                    ],
                )?,
            };
            list.append(item)?;
        }
        py.import("oxbrook._forms")?
            .getattr("FormData")?
            .call_method1("from_parts", (list,))
    }

    /// The body as an async iterator of `bytes` chunks: a `BodyStream`.
    ///
    /// On a route that declares a `BodyStream` argument the chunks arrive as
    /// the client sends them, and nothing is read until the first one is
    /// asked for. On any other route the body was already collected, and this
    /// yields it as a single chunk, so code reading a stream works on both.
    fn stream<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let reader = self._reader(py)?;
        let buffered = PyBytes::new(py, self.filled.get().unwrap_or(&self.body));
        py.import("oxbrook._bodies")?
            .getattr("BodyStream")?
            .call1((reader, buffered))
    }

    fn __repr__(&self) -> String {
        format!("<Request {} {}>", self.method, self.path)
    }
}
