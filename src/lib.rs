//! Oxbrook core: a tokio + hyper HTTP server that dispatches requests into a pool
//! of Python asyncio event loops (one per worker thread).
//!
//! Dispatch is queue-based: tokio threads never attach to the interpreter. They
//! push plain Rust structs onto a lock-free per-worker queue and wake the
//! worker's asyncio loop through a socketpair it already watches.
//!
//! Milestone-1 spike. The goal is to measure the Rust<->Python boundary, not to
//! be feature complete.

mod body;
mod cancel;
mod cors;
mod files;
mod form;
mod origin;
mod queue;
mod request;
mod responder;
mod router;
mod server;
mod tls;
mod wake;
mod websocket;
mod worker;

use pyo3::prelude::*;

/// `gil_used = false` tells free-threaded CPython that this extension is safe to
/// import without re-enabling the GIL. On a standard build it is a no-op.
#[pymodule(gil_used = false)]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<request::Request>()?;
    m.add_class::<responder::Responder>()?;
    m.add_class::<server::Server>()?;
    m.add_class::<websocket::WebSocket>()?;
    Ok(())
}
