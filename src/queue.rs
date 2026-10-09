//! Per-worker request queue.
//!
//! The whole point of this module: a tokio thread must be able to hand a
//! request to a Python worker *without attaching to the interpreter*. Attaching
//! costs an atomic refcount storm on free-threaded builds and a GIL handoff on
//! standard ones, and the milestone-1 benchmark showed that cost dominating.
//!
//! So a pending request is plain Rust data. It goes into a lock-free queue, and
//! the worker's asyncio loop is woken through a socket pair that the loop
//! already watches via `loop.add_reader`. Writing one byte to a socket is a
//! syscall, not a Python call.

use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use crossbeam_queue::SegQueue;
use pyo3::prelude::*;
use tokio::sync::oneshot;

use crate::responder::Reply;
use crate::router::ParamValue;
use crate::wake::WakeWriter;
use crate::websocket::Shared;

/// Handlers still running for one HTTP/2 connection, counting those whose
/// stream the client has already reset. Incremented before the request is
/// queued and decremented when its `Responder` releases its slot.
pub type ConnectionLoad = std::sync::Arc<AtomicUsize>;

/// One request waiting for a Python worker. No Python objects: the handler is
/// referenced by index into the shared route table.
pub struct Pending {
    pub route: usize,
    /// Coerced path parameters, in the order of the route's spec.
    pub params: Vec<ParamValue>,
    pub method: String,
    pub path: String,
    pub query: Option<String>,
    pub body: Vec<u8>,
    pub headers: hyper::HeaderMap,
    pub reply: oneshot::Sender<Reply>,
    /// Set for an upgraded connection. The handler gets a socket instead of
    /// producing a reply, since the 101 has already gone out.
    pub websocket: Option<std::sync::Arc<Shared>>,
    /// Run this route's authorizer rather than its handler, and answer with
    /// its verdict. Used to decide an upgrade before the handshake.
    pub gate: bool,
    /// Set for a route that streams its body; `body` is then empty.
    pub body_stream: Option<std::sync::Arc<crate::body::BodyShared>>,
    /// The body is in `body_stream` because the route defers it, not because
    /// the handler asked for a stream: the handler reads it whole, later.
    pub deferred: bool,
    /// For a socket, the key its gate left the authenticated caller under.
    pub handoff: Option<String>,
    /// Set for a request on an HTTP/2 connection.
    pub connection: Option<ConnectionLoad>,
    /// Set for a route that is cancelled when its client leaves.
    pub cancel: Option<std::sync::Arc<crate::cancel::Cancel>>,
    /// When it was queued, stamped by `try_push` only when metrics are on.
    pub queued_at: Option<Instant>,
}

pub struct WorkerQueue {
    queue: SegQueue<Pending>,
    /// Callbacks to run on this worker's loop, pushed from tokio threads.
    ///
    /// A tokio thread must never schedule work by calling into the
    /// interpreter. `loop.call_soon_threadsafe` writes to the loop's self-pipe
    /// and can block there; on the GIL build a thread that blocks while
    /// attached holds the lock and stops every other thread, which deadlocked
    /// shutdown (I-038). Holding and moving a `Py<PyAny>` touches no refcount,
    /// so the wake path that already exists for requests carries these too.
    wakeups: SegQueue<Py<PyAny>>,
    /// Requests whose client is gone, to cancel on this loop.
    cancels: SegQueue<std::sync::Arc<crate::cancel::Cancel>>,
    /// True when a wake byte is in flight and not yet consumed. Collapses a
    /// burst of requests into a single wakeup.
    notified: AtomicBool,
    /// When `notified` last went from false to true, in milliseconds since
    /// `epoch()`, or `IDLE` while it is false. A wake that stays unconsumed is
    /// a loop that is not getting back to its queue: a handler blocking it, or
    /// a deadlock. The liveness probe reads this (D-058).
    notified_at: AtomicU64,
    /// Write end of the wake pair. The read end lives in the `Drainer`.
    waker: WakeWriter,
    /// Requests handed to the worker and not yet finished. Counted alongside
    /// the queue, because bounding the queue alone is not backpressure: the
    /// drain callback empties it into asyncio tasks immediately, so a handler
    /// that awaits I/O leaves the queue empty while thousands of requests pile
    /// up in the loop. Queued plus in-flight is the number that matters.
    inflight: AtomicUsize,
    /// Hard limit on queued + in-flight requests for this worker. Unbounded
    /// growth does not fail gracefully: memory climbs until it runs out, and
    /// every pending request is a connection held open with a client waiting
    /// on a reply that will arrive long after it stopped caring.
    limit: usize,
    /// Time from push to pop, when the app asked for metrics (D-059).
    wait: OnceLock<crate::metrics::Histogram>,
}

impl WorkerQueue {
    pub fn new(waker: WakeWriter, limit: usize) -> Self {
        Self {
            queue: SegQueue::new(),
            wakeups: SegQueue::new(),
            cancels: SegQueue::new(),
            notified: AtomicBool::new(false),
            notified_at: AtomicU64::new(IDLE),
            waker,
            inflight: AtomicUsize::new(0),
            limit,
            wait: OnceLock::new(),
        }
    }

    /// Start measuring queue wait. Before serving, once.
    pub fn measure_wait(&self) {
        let _ = self.wait.set(crate::metrics::wait_histogram());
    }

    pub fn wait(&self) -> Option<&crate::metrics::Histogram> {
        self.wait.get()
    }

    /// Requests pushed and not yet taken by the loop.
    pub fn queued(&self) -> usize {
        self.queue.len()
    }

    pub fn load(&self) -> usize {
        self.queue.len() + self.inflight.load(Ordering::Relaxed)
    }

    pub fn has_room(&self) -> bool {
        self.load() < self.limit
    }

    /// The drain callback claims a request: it leaves the queue and becomes
    /// in-flight. Brief undercounting between the two is harmless, since the
    /// limit is a pressure valve rather than an invariant.
    pub fn claim(&self) {
        self.inflight.fetch_add(1, Ordering::Relaxed);
    }

    /// Called when a `Responder` is dropped, which happens whether the handler
    /// replied, raised, or was cancelled.
    pub fn release(&self) {
        self.inflight.fetch_sub(1, Ordering::Relaxed);
    }

    /// Called from tokio threads. Returns the item when this worker is at its
    /// limit, so the caller can try another worker or shed the request.
    ///
    /// The `Err` variant is the whole request, which clippy notes is large.
    /// Boxing it would move an allocation onto the shed path — the path taken
    /// when the server is already overloaded, and the one place where an extra
    /// allocation is least welcome. The cost here is stack space in a function
    /// that already takes the same value by value.
    #[allow(clippy::result_large_err)]
    pub fn try_push(&self, mut item: Pending) -> Result<(), Pending> {
        // Racy against other producers. Overshooting by a few under a burst is
        // fine; what matters is that the number cannot grow without bound.
        if !self.has_room() {
            return Err(item);
        }
        if self.wait.get().is_some() {
            item.queued_at = Some(Instant::now());
        }
        self.queue.push(item);
        self.wake();
        Ok(())
    }

    pub fn pop(&self) -> Option<Pending> {
        let item = self.queue.pop()?;
        if let (Some(wait), Some(at)) = (self.wait.get(), item.queued_at) {
            wait.observe(at.elapsed());
        }
        Some(item)
    }

    /// Ask this worker's loop to call `callback`. Safe from a tokio thread:
    /// moving the handle touches no refcount and the wake is one socket write.
    pub fn push_wakeup(&self, callback: Py<PyAny>) {
        self.wakeups.push(callback);
        self.wake();
    }

    pub fn pop_wakeup(&self) -> Option<Py<PyAny>> {
        self.wakeups.pop()
    }

    /// From a tokio thread: a request's client is gone. Carries no Python
    /// object, only the cell the worker finds the task in.
    pub fn push_cancel(&self, cell: std::sync::Arc<crate::cancel::Cancel>) {
        self.cancels.push(cell);
        self.wake();
    }

    pub fn pop_cancel(&self) -> Option<std::sync::Arc<crate::cancel::Cancel>> {
        self.cancels.pop()
    }

    /// Called by the drain callback before it starts popping, so that a
    /// producer racing with the drain always triggers a fresh wakeup.
    pub fn clear_notified(&self) {
        // Before the flag, so a reader that sees the flag still set never
        // pairs it with this episode's start once the episode is over.
        self.notified_at.store(IDLE, Ordering::SeqCst);
        self.notified.store(false, Ordering::SeqCst);
    }

    /// How long a wake has gone unanswered, if one is waiting. From any
    /// thread; two atomic loads.
    pub fn waiting_for(&self) -> Option<Duration> {
        if !self.notified.load(Ordering::SeqCst) {
            return None;
        }
        // IDLE here is the instant between the flip and the store below:
        // just woken, not stalled.
        let at = self.notified_at.load(Ordering::SeqCst);
        (at != IDLE).then(|| Duration::from_millis(millis().saturating_sub(at)))
    }

    /// Re-arm if anything arrived while we were draining. Both queues count:
    /// a disconnect notification that lands mid-drain and does not re-arm sits
    /// unserved until the next request, which for an idle stream is never.
    pub fn rewake_if_pending(&self) {
        if !self.queue.is_empty() || !self.wakeups.is_empty() || !self.cancels.is_empty() {
            self.wake();
        }
    }

    fn wake(&self) {
        // Only the thread that flips false->true writes the byte, so at most
        // one unread byte exists and the socket buffer can never fill.
        if !self.notified.swap(true, Ordering::SeqCst) {
            // Only the flipping thread stamps it: a later push while the wake
            // is still pending must not restart the clock, or a loop that is
            // stuck under steady traffic would never look stuck.
            self.notified_at.store(millis(), Ordering::SeqCst);
            self.waker.wake();
        }
    }
}

const IDLE: u64 = u64::MAX;

fn epoch() -> Instant {
    static EPOCH: OnceLock<Instant> = OnceLock::new();
    *EPOCH.get_or_init(Instant::now)
}

fn millis() -> u64 {
    epoch().elapsed().as_millis() as u64
}
