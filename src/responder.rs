use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::sync::{mpsc, oneshot};

use crate::queue::{ConnectionLoad, WorkerQueue};

/// Chunks a streaming response can buffer before the producer is told to slow
/// down. Small on purpose: a deep buffer only hides a slow client until memory
/// runs out.
const STREAM_BUFFER: usize = 64;

pub enum Body {
    /// The whole response, known up front.
    Full(Vec<u8>),
    /// Chunks arriving over time, for SSE and anything else long-lived.
    ///
    /// The second field is a liveness guard, not data. It travels with the
    /// response body and is dropped when hyper drops it, whether that is a
    /// client disconnect or a stream that finished. Watching the chunk sender
    /// instead would be wrong: holding a clone of it to watch would itself keep
    /// the channel open, so a finished stream would never terminate.
    Stream(mpsc::Receiver<Bytes>, oneshot::Sender<()>),
}

/// What the Python side hands back for one request.
pub struct Reply {
    pub status: u16,
    pub content_type: String,
    pub headers: Vec<(String, String)>,
    pub body: Body,
}

/// Result of pushing one chunk into a streaming response.
#[derive(Clone, Copy)]
pub enum ChunkResult {
    Sent,
    /// The client is not keeping up; the caller decides whether to drop or wait.
    Full,
    /// The client went away.
    Closed,
}

impl ChunkResult {
    fn code(self) -> u8 {
        match self {
            Self::Sent => 0,
            Self::Full => 1,
            Self::Closed => 2,
        }
    }
}

/// One-shot channel back to the tokio task that owns the connection.
/// The Python runtime calls `send` or `send_json` exactly once.
#[pyclass(frozen, name = "Responder", module = "oxbrook._core")]
pub struct Responder {
    tx: Mutex<Option<oneshot::Sender<Reply>>>,
    /// Set once a streaming response has started.
    chunks: Mutex<Option<mpsc::Sender<Bytes>>>,
    /// Resolves when the response body is dropped. Taken by `notify_disconnect`.
    body_dropped: Mutex<Option<oneshot::Receiver<()>>>,
    /// Lets a long-lived stream learn that its client is gone without polling.
    runtime: tokio::runtime::Handle,
    /// Holding the worker's queue lets the in-flight count fall when the
    /// request is finished.
    queue: Arc<WorkerQueue>,
    /// The HTTP/2 connection's count of running handlers, released with the
    /// slot and for the same reason: a client resetting its stream does not
    /// stop the handler, so the count must follow the handler, not the stream.
    connection: Option<ConnectionLoad>,
    /// Guards against releasing twice, since both `finish` and `Drop` release.
    released: AtomicBool,
}

impl Drop for Responder {
    fn drop(&mut self) {
        // Backstop for a task that was cancelled or abandoned without calling
        // `finish`. Ordinarily `finish` has already run.
        self.release_once();
    }
}

impl Responder {
    fn release_once(&self) {
        if !self.released.swap(true, Ordering::SeqCst) {
            self.queue.release();
            if let Some(load) = &self.connection {
                load.fetch_sub(1, Ordering::Relaxed);
            }
        }
    }
}

impl Responder {
    pub fn new(
        tx: oneshot::Sender<Reply>,
        queue: Arc<WorkerQueue>,
        runtime: tokio::runtime::Handle,
        connection: Option<ConnectionLoad>,
    ) -> Self {
        Self {
            tx: Mutex::new(Some(tx)),
            chunks: Mutex::new(None),
            body_dropped: Mutex::new(None),
            runtime,
            queue,
            connection,
            released: AtomicBool::new(false),
        }
    }

    fn deliver(&self, reply: Reply) -> PyResult<()> {
        let tx = self
            .tx
            .lock()
            .map_err(|_| PyRuntimeError::new_err("responder lock poisoned"))?
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("response already sent"))?;
        // A complete reply finishes the request: the handler has returned and
        // nothing else runs on it. Release the slot before the reply can reach
        // the client, not after. Released later, in `finish`, a client that
        // sent its next request as soon as this one answered could find the
        // loop still counted busy, tied with a loop genuinely held by a
        // CPU-bound handler, and be assigned to the held one — seen on CI as a
        // 255 ms wait (least-loaded assignment, D-031). A stream keeps its slot
        // until `finish`, because it is still being served.
        if matches!(reply.body, Body::Full(_)) {
            self.release_once();
        }
        // If the client went away the receiver is gone; that's not a Python error.
        let _ = tx.send(reply);
        Ok(())
    }
}

#[pymethods]
impl Responder {
    /// Send raw bytes with an explicit content type.
    #[pyo3(signature = (status, content_type, body, headers = None))]
    fn send(
        &self,
        status: u16,
        content_type: &str,
        body: &[u8],
        headers: Option<Vec<(String, String)>>,
    ) -> PyResult<()> {
        self.deliver(Reply {
            status,
            content_type: content_type.to_owned(),
            headers: headers.unwrap_or_default(),
            body: Body::Full(body.to_vec()),
        })
    }

    /// Begin a streaming response. Headers go out immediately; chunks follow
    /// through `send_chunk` until `end_stream`.
    #[pyo3(signature = (status, content_type, headers = None))]
    fn start_stream(
        &self,
        status: u16,
        content_type: &str,
        headers: Option<Vec<(String, String)>>,
    ) -> PyResult<()> {
        let (chunk_tx, chunk_rx) = mpsc::channel::<Bytes>(STREAM_BUFFER);
        let (alive_tx, alive_rx) = oneshot::channel::<()>();
        self.deliver(Reply {
            status,
            content_type: content_type.to_owned(),
            headers: headers.unwrap_or_default(),
            body: Body::Stream(chunk_rx, alive_tx),
        })?;
        *self
            .chunks
            .lock()
            .map_err(|_| PyRuntimeError::new_err("responder lock poisoned"))? = Some(chunk_tx);
        *self
            .body_dropped
            .lock()
            .map_err(|_| PyRuntimeError::new_err("responder lock poisoned"))? = Some(alive_rx);
        Ok(())
    }

    /// Push one chunk. Returns 0 sent, 1 buffer full, 2 client gone.
    ///
    /// Never blocks: this runs on a worker's event loop, where blocking would
    /// stall every other request sharing that loop.
    fn send_chunk(&self, chunk: &[u8]) -> PyResult<u8> {
        let guard = self
            .chunks
            .lock()
            .map_err(|_| PyRuntimeError::new_err("responder lock poisoned"))?;
        let Some(tx) = guard.as_ref() else {
            return Ok(ChunkResult::Closed.code());
        };
        Ok(match tx.try_send(Bytes::copy_from_slice(chunk)) {
            Ok(()) => ChunkResult::Sent,
            Err(mpsc::error::TrySendError::Full(_)) => ChunkResult::Full,
            Err(mpsc::error::TrySendError::Closed(_)) => ChunkResult::Closed,
        }
        .code())
    }

    /// Mark the request finished and free its concurrency slot.
    ///
    /// Explicit rather than left to `Drop`, because dropping depends on when
    /// Python releases the object, and the SSE path forms a reference cycle
    /// that the cyclic collector only breaks later. A slot is a resource
    /// limit; it cannot be governed by garbage-collection timing.
    fn finish(&self) {
        self.release_once();
    }

    /// Close a streaming response.
    fn end_stream(&self) -> PyResult<()> {
        *self
            .chunks
            .lock()
            .map_err(|_| PyRuntimeError::new_err("responder lock poisoned"))? = None;
        Ok(())
    }

    /// Call `callback` on the worker's loop once the client disconnects.
    ///
    /// Without this a stream sitting in `__anext__` waiting for its next
    /// message would never notice the client had gone, holding a subscription
    /// and an in-flight slot until something happened to be published. Polling
    /// would also work but costs a wakeup per connection per interval; this
    /// costs one queued callback per connection, at teardown.
    ///
    /// The notification goes through the worker's wake queue rather than
    /// `call_soon_threadsafe`. The task below runs on a tokio thread, and an
    /// earlier version attached to the interpreter there: on the GIL build that
    /// thread then blocked inside the loop's self-pipe write while holding the
    /// lock, and shutdown deadlocked (I-038). Invariants 1 and 2 both say not
    /// to, and the free-threaded build hid it.
    fn notify_disconnect(&self, callback: Py<PyAny>) -> PyResult<()> {
        let watcher = self
            .body_dropped
            .lock()
            .map_err(|_| PyRuntimeError::new_err("responder lock poisoned"))?
            .take();

        let Some(watcher) = watcher else {
            // Not streaming, or already being watched: hand it over now.
            self.queue.push_wakeup(callback);
            return Ok(());
        };

        let queue = self.queue.clone();
        self.runtime.spawn(async move {
            // Errors when the guard is dropped, which is exactly the signal.
            let _ = watcher.await;
            queue.push_wakeup(callback);
        });
        Ok(())
    }

    /// Serialize a Python object to JSON *in Rust* and send it.
    /// This is the path we care about measuring: no `json.dumps` in Python.
    fn send_json(&self, status: u16, obj: &Bound<'_, PyAny>) -> PyResult<()> {
        let value: serde_json::Value = pythonize::depythonize(obj)?;
        let body = serde_json::to_vec(&value)
            .map_err(|e| PyRuntimeError::new_err(format!("json encode failed: {e}")))?;
        self.deliver(Reply {
            status,
            content_type: "application/json".to_owned(),
            headers: Vec::new(),
            body: Body::Full(body),
        })
    }
}
