//! Cancelling a handler whose client is gone (D-045).
//!
//! A request is abandoned when the tokio task waiting for its reply stops
//! waiting before the first reply arrives: the client closed the connection,
//! reset its HTTP/2 stream, or the request timed out. The handler's work can
//! no longer reach anyone, and left running it holds its worker's slot, so a
//! client that connected and closed in a loop could fill every worker (I-082).
//!
//! The tokio side never touches the interpreter (invariant 1). It flips a flag
//! and queues this shared cell for the request's worker, which cancels the
//! asyncio task on its own thread. The task handle lives in the cell only
//! between the drain that created it and the `Responder` releasing the
//! request, and the `Responder` holds the cell for that whole time, so the
//! last reference a tokio thread drops never carries a Python object.

use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Mutex};

use pyo3::prelude::*;

use crate::queue::WorkerQueue;

const RUNNING: u8 = 0;
const ABANDONED: u8 = 1;
const ANSWERED: u8 = 2;

#[derive(Default)]
pub struct Cancel {
    state: AtomicU8,
    /// The asyncio task running the handler, once the drain has created it.
    task: Mutex<Option<Py<PyAny>>>,
}

impl Cancel {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    pub fn abandoned(&self) -> bool {
        self.state.load(Ordering::Relaxed) == ABANDONED
    }

    /// Worker thread, right after `create_task`. If the client left while the
    /// task was being created, cancel it now: the notice may already have been
    /// drained, and found no task to cancel.
    pub fn attach(&self, py: Python<'_>, task: Py<PyAny>) -> PyResult<()> {
        if let Ok(mut slot) = self.task.lock() {
            *slot = Some(task);
        }
        if self.abandoned() {
            self.cancel(py)?;
        }
        Ok(())
    }

    /// Worker thread. Cancels the task at most once; a second call finds the
    /// slot empty.
    pub fn cancel(&self, py: Python<'_>) -> PyResult<()> {
        let task = self.task.lock().ok().and_then(|mut slot| slot.take());
        if let Some(task) = task {
            task.bind(py).call_method0("cancel")?;
        }
        Ok(())
    }

    /// Worker thread, as the handler replies. A late abandon then finds the
    /// request answered and does nothing.
    pub fn answer(&self) {
        let _ = self
            .state
            .compare_exchange(RUNNING, ANSWERED, Ordering::SeqCst, Ordering::SeqCst);
    }

    /// Worker thread, from the `Responder` as it releases the request. Breaks
    /// the cycle through the task, which holds the coroutine, which holds the
    /// `Responder`, which holds this cell.
    pub fn forget(&self) {
        self.answer();
        if let Ok(mut slot) = self.task.lock() {
            slot.take();
        }
    }
}

/// Held by the tokio task waiting for a reply. Dropped while still armed, it
/// abandons the request.
///
/// Borrows the queue rather than cloning its `Arc`: that count is shared by
/// every tokio thread, and touching it per request measured as a real share of
/// this feature's cost.
pub struct Abandon<'a> {
    cell: Arc<Cancel>,
    queue: &'a WorkerQueue,
    armed: bool,
}

impl<'a> Abandon<'a> {
    pub fn new(cell: Arc<Cancel>, queue: &'a WorkerQueue) -> Self {
        Self {
            cell,
            queue,
            armed: true,
        }
    }

    /// The handler answered: from here on its client leaving is not a reason
    /// to stop it. A stream that has started has its own disconnect signal.
    pub fn answered(mut self) {
        self.armed = false;
    }
}

impl Drop for Abandon<'_> {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        // Flag first, notice second: a drain that misses the notice sees the
        // flag when it attaches the task, and one that misses the flag gets
        // the notice later.
        if self
            .cell
            .state
            .compare_exchange(RUNNING, ABANDONED, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            self.queue.push_cancel(self.cell.clone());
        }
    }
}
