//! Streaming request bodies.
//!
//! An ordinary route gets its body whole, collected up to `max_body` before a
//! worker is woken. A route that takes a `BodyStream` gets it incrementally
//! instead, so an upload larger than memory, or one the handler wants to refuse
//! before it arrives, is possible.
//!
//! The bridge follows the WebSocket one. The connection's own future pumps
//! frames from hyper into a shared queue; the handler takes them on its worker
//! loop. Three rules shape it:
//!
//! * **Nothing is read until the handler asks.** The pump waits for the first
//!   read, so a handler that checks auth and answers 401 never makes the client
//!   send the body — hyper only sends `100 Continue` once the body is polled.
//! * **Backpressure is real.** Past `HIGH_WATER` buffered bytes the pump stops
//!   reading the socket until the handler catches up, so a fast client cannot
//!   fill the worker's memory; TCP flow control pushes back on the client.
//! * **Python is woken only through the worker queue**, never from the tokio
//!   side directly (invariant 1, and I-038).

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::body::Incoming;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use tokio::sync::Notify;

use crate::queue::WorkerQueue;

/// Bytes allowed to sit between the socket and the handler.
const HIGH_WATER: usize = 1024 * 1024;

#[derive(Clone, Copy)]
pub enum Failure {
    TooLarge(usize),
    /// The client stopped sending for longer than the request timeout.
    Idle,
    /// The connection ended or broke before the body was complete.
    Incomplete,
}

enum Chunk {
    Data(Bytes),
    End,
    Failed(Failure),
}

pub struct BodyShared {
    chunks: Mutex<VecDeque<Chunk>>,
    buffered: AtomicUsize,
    /// Signalled by the handler when it wants more; the pump waits on it both
    /// before its first read and whenever the buffer is over the high water.
    demand: Notify,
    waiter: Mutex<Option<Py<PyAny>>>,
    queue: OnceLock<Arc<WorkerQueue>>,
    /// True once `End` or a failure has been queued.
    finished: AtomicBool,
    /// Milliseconds since `origin` at which a chunk last moved, in either
    /// direction. The request timeout measures from here for a streaming route,
    /// so a large upload that is making progress is never cut off.
    progress: AtomicU64,
    origin: Instant,
    /// True while the pump is waiting on the client for the next frame. The
    /// pump's own idle timeout owns that wait and answers 408; the reply
    /// timeout must not race it and answer 504 for the client's stall.
    reading: AtomicBool,
}

impl BodyShared {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            chunks: Mutex::new(VecDeque::new()),
            buffered: AtomicUsize::new(0),
            demand: Notify::new(),
            waiter: Mutex::new(None),
            queue: OnceLock::new(),
            finished: AtomicBool::new(false),
            progress: AtomicU64::new(0),
            origin: Instant::now(),
            reading: AtomicBool::new(false),
        })
    }

    pub fn bind_queue(&self, queue: Arc<WorkerQueue>) {
        let _ = self.queue.set(queue);
    }

    fn touch(&self) {
        let now = self.origin.elapsed().as_millis() as u64;
        self.progress.store(now, Ordering::Relaxed);
    }

    /// Whether the pump is waiting on the client right now.
    pub fn waiting_on_client(&self) -> bool {
        self.reading.load(Ordering::Relaxed)
    }

    /// When the body last made progress.
    pub fn last_progress(&self) -> Instant {
        self.origin + Duration::from_millis(self.progress.load(Ordering::Relaxed))
    }

    fn push(&self, chunk: Chunk) {
        if let Chunk::Data(bytes) = &chunk {
            self.buffered.fetch_add(bytes.len(), Ordering::Relaxed);
        } else {
            self.finished.store(true, Ordering::SeqCst);
        }
        if let Ok(mut chunks) = self.chunks.lock() {
            chunks.push_back(chunk);
        }
        self.touch();
        // Called from the connection's future. Moving the callback into the
        // worker queue touches no interpreter state.
        let waiter = self.waiter.lock().ok().and_then(|mut w| w.take());
        if let (Some(callback), Some(queue)) = (waiter, self.queue.get()) {
            queue.push_wakeup(callback);
        }
    }

    /// Called when the connection's future stops pumping for any reason. A
    /// handler still waiting must be told the body will not complete rather
    /// than wait forever.
    fn abandon(&self) {
        if !self.finished.load(Ordering::SeqCst) {
            self.push(Chunk::Failed(Failure::Incomplete));
        }
    }
}

/// Move the body from hyper into `shared`, honouring demand and limits.
///
/// Runs inside the connection's request future, so it stops when that future
/// is dropped — after the response is sent, or when the connection dies — and
/// How long to keep reading a body that has already been refused.
///
/// A response sent while the request body is still arriving reaches the client
/// only if the socket has no unread data left when it closes: closing with
/// data still in the receive queue is a reset, and Windows throws away
/// whatever the client had already buffered when one arrives — so the 413 the
/// server took care to send is replaced by "connection aborted" (I-089). The
/// cure is to read the rest away first, what nginx calls a lingering close.
///
/// Bounded by time, not by bytes: the point is to let a client that is about
/// to stop sending finish, not to accept a body after refusing it. A client
/// that keeps sending past this is reset, as before.
const LINGER: Duration = Duration::from_millis(250);

/// Read and discard what is left of a body, for at most `LINGER`.
async fn drain(mut body: Incoming) {
    let _ = tokio::time::timeout(LINGER, async {
        while let Some(Ok(_)) = body.frame().await {}
    })
    .await;
}

/// `drain`, from the pump. `reading` stays set, so the wait counts as the
/// client's rather than as a handler that has stalled.
async fn linger(body: Incoming, shared: &BodyShared) {
    shared.reading.store(true, Ordering::Relaxed);
    drain(body).await;
    shared.reading.store(false, Ordering::Relaxed);
}

/// Collect a whole body, or say why not. Over the limit, what is left is read
/// away before the caller answers, for the reason in `linger`.
pub async fn collect_bounded(mut body: Incoming, limit: usize) -> Result<Vec<u8>, Failure> {
    let mut collected: Vec<u8> = Vec::new();
    loop {
        match body.frame().await {
            None => return Ok(collected),
            Some(Err(_)) => return Err(Failure::Incomplete),
            Some(Ok(frame)) => {
                let Ok(data) = frame.into_data() else {
                    continue; // Trailers.
                };
                if collected.len() + data.len() > limit {
                    drain(body).await;
                    return Err(Failure::TooLarge(limit));
                }
                collected.extend_from_slice(&data);
            }
        }
    }
}

/// cannot outlive the request.
pub async fn pump(
    body: Incoming,
    declared_length: Option<u64>,
    shared: Arc<BodyShared>,
    max_body: usize,
    idle: Option<Duration>,
) {
    struct Abandon(Arc<BodyShared>);
    impl Drop for Abandon {
        fn drop(&mut self) {
            self.0.abandon();
        }
    }
    let _abandon = Abandon(shared.clone());

    // Lazy: nothing leaves the socket until the handler's first read.
    shared.demand.notified().await;

    // A declared length over the limit is refused without reading a byte of
    // it — but the client is sending it anyway, so it is read away before the
    // refusal goes out rather than after.
    if declared_length.is_some_and(|length| length > max_body as u64) {
        linger(body, &shared).await;
        shared.push(Chunk::Failed(Failure::TooLarge(max_body)));
        return;
    }

    let mut body = body;
    let mut total = 0usize;
    loop {
        while shared.buffered.load(Ordering::Relaxed) > HIGH_WATER {
            shared.demand.notified().await;
        }
        let next = body.frame();
        shared.reading.store(true, Ordering::Relaxed);
        let frame = match idle {
            Some(limit) => tokio::time::timeout(limit, next).await,
            None => Ok(next.await),
        };
        shared.reading.store(false, Ordering::Relaxed);
        let Ok(frame) = frame else {
            shared.push(Chunk::Failed(Failure::Idle));
            return;
        };
        match frame {
            None => {
                shared.push(Chunk::End);
                return;
            }
            Some(Err(_)) => {
                shared.push(Chunk::Failed(Failure::Incomplete));
                return;
            }
            Some(Ok(frame)) => {
                let Ok(data) = frame.into_data() else {
                    // Trailers. Nothing a handler reads through this API.
                    continue;
                };
                total += data.len();
                if total > max_body {
                    linger(body, &shared).await;
                    shared.push(Chunk::Failed(Failure::TooLarge(max_body)));
                    return;
                }
                if !data.is_empty() {
                    shared.push(Chunk::Data(data));
                }
            }
        }
    }
}

/// The handler's side of a streaming body. Wrapped by `oxbrook.BodyStream`.
#[pyclass(frozen, name = "BodyReader", module = "oxbrook._core")]
pub struct BodyReader {
    shared: Arc<BodyShared>,
}

impl BodyReader {
    pub fn new(shared: Arc<BodyShared>) -> Self {
        Self { shared }
    }
}

/// `poll` results, mirrored in `oxbrook._bodies`.
const DATA: u8 = 0;
const PENDING: u8 = 1;
const END: u8 = 2;
const FAILED: u8 = 3;

#[pymethods]
impl BodyReader {
    /// `(DATA, bytes)`, `(PENDING, None)`, `(END, None)`, or
    /// `(FAILED, (status, detail))`. Never blocks.
    fn poll<'py>(&self, py: Python<'py>) -> PyResult<(u8, Bound<'py, PyAny>)> {
        let chunk = self
            .shared
            .chunks
            .lock()
            .ok()
            .and_then(|mut c| c.pop_front());
        let Some(chunk) = chunk else {
            if !self.shared.finished.load(Ordering::SeqCst) {
                // Empty and not finished: ask the pump for more. `notify_one`
                // keeps a permit if the pump is not waiting yet, so the request
                // cannot be lost between here and its next wait.
                self.shared.demand.notify_one();
            }
            return Ok((PENDING, py.None().into_bound(py)));
        };
        self.shared.touch();
        Ok(match chunk {
            Chunk::Data(bytes) => {
                let before = self
                    .shared
                    .buffered
                    .fetch_sub(bytes.len(), Ordering::Relaxed);
                if before > HIGH_WATER && before - bytes.len() <= HIGH_WATER {
                    self.shared.demand.notify_one();
                }
                (DATA, PyBytes::new(py, &bytes).into_any())
            }
            Chunk::End => {
                // Leave the marker for any later poll, so reading past the end
                // keeps answering END rather than PENDING forever.
                if let Ok(mut chunks) = self.shared.chunks.lock() {
                    chunks.push_front(Chunk::End);
                }
                (END, py.None().into_bound(py))
            }
            Chunk::Failed(failure) => {
                // Left in place, so every later read reports the same failure.
                if let Ok(mut chunks) = self.shared.chunks.lock() {
                    chunks.push_front(Chunk::Failed(failure));
                }
                let (status, detail) = match failure {
                    Failure::TooLarge(limit) => {
                        (413u16, format!("request body larger than {limit} bytes"))
                    }
                    Failure::Idle => (408u16, "request body stalled".to_owned()),
                    Failure::Incomplete => (400u16, "request body ended early".to_owned()),
                };
                (FAILED, (status, detail).into_pyobject(py)?.into_any())
            }
        })
    }

    /// Call `callback` on the worker loop when there is something to poll.
    /// Fires promptly if there already is.
    fn notify(&self, callback: Py<PyAny>) {
        if let Ok(mut waiter) = self.shared.waiter.lock() {
            *waiter = Some(callback);
        }
        // Data may have arrived between the caller's poll and installing the
        // waiter; without this re-check that wakeup would be lost.
        let ready = self
            .shared
            .chunks
            .lock()
            .map(|c| !c.is_empty())
            .unwrap_or(true);
        if ready {
            let waiter = self.shared.waiter.lock().ok().and_then(|mut w| w.take());
            if let (Some(callback), Some(queue)) = (waiter, self.shared.queue.get()) {
                queue.push_wakeup(callback);
            }
        }
    }
}
