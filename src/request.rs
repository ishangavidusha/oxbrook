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
    #[pyo3(signature = (method = "GET".to_string(), path = "/".to_string(), query = None, body = None, headers = None, context = None, locals = None))]
    fn py_new(
        py: Python<'_>,
        method: String,
        path: String,
        query: Option<String>,
        body: Option<Vec<u8>>,
        headers: Option<Vec<(String, String)>>,
        context: Option<Py<PyAny>>,
        locals: Option<Py<PyDict>>,
    ) -> Self {
        let mut map = HeaderMap::new();
        for (name, value) in headers.unwrap_or_default() {
            if let (Ok(name), Ok(value)) = (
                HeaderName::from_bytes(name.as_bytes()),
                HeaderValue::from_str(&value),
            ) {
                map.append(name, value);
            }
        }
        // A tool call shares the `/mcp` request's dict: app middleware ran
        // once, around that request, and what it left there is for the tool
        // as much as for anything else.
        let cell = PyOnceLock::new();
        if let Some(shared) = locals {
            let _ = cell.set(py, shared);
        }
        Self {
            method,
            path,
            query,
            body: body.unwrap_or_default(),
            headers: map,
            context,
            stream: None,
            locals: cell,
        }
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
    #[getter]
    fn body<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.body)
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
        let body = &self.body;
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
        let reader = match &self.stream {
            Some(shared) => Some(Py::new(py, crate::body::BodyReader::new(shared.clone()))?),
            None => None,
        };
        let buffered = PyBytes::new(py, &self.body);
        py.import("oxbrook._bodies")?
            .getattr("BodyStream")?
            .call1((reader, buffered))
    }

    fn __repr__(&self) -> String {
        format!("<Request {} {}>", self.method, self.path)
    }
}
