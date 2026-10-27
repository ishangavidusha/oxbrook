//! WebSocket connections.
//!
//! A tokio task owns the socket and does the framing; the handler runs on a
//! Python worker loop. The two are bridged by a queue in each direction, with
//! the same rule as everywhere else in Oxbrook: Python is only touched when it
//! is actually idle and needs waking, never once per message on a busy socket.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use futures_util::{SinkExt, StreamExt};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, Message, Role, WebSocketConfig};
use tokio_tungstenite::WebSocketStream;

use crate::queue::WorkerQueue;

/// How many outgoing messages may queue before `send` reports back-pressure.
const SEND_BUFFER: usize = 256;

pub enum Frame {
    Text(String),
    Binary(Vec<u8>),
    /// The server's close, with the code and reason to send, if any.
    Close(Option<(u16, String)>),
}

/// State shared between the tokio task and the Python handler.
pub struct Shared {
    incoming: Mutex<VecDeque<Frame>>,
    /// (event loop, callback) to fire when a message lands and Python is idle.
    waiter: Mutex<Option<(Py<PyAny>, Py<PyAny>)>>,
    /// Fired once, when the socket closes. Separate from `waiter` because a
    /// handler blocked on something other than this socket still has to learn
    /// that its peer went away.
    close_waiters: Mutex<Vec<(Py<PyAny>, Py<PyAny>)>>,
    outgoing: mpsc::Sender<Frame>,
    closed: AtomicBool,
    /// The queue of the worker running this socket's handler, bound when the
    /// connection is handed to a worker and therefore before any waiter can
    /// exist. Wakes travel through it: the tokio task must not attach to the
    /// interpreter to schedule them, which on the GIL build deadlocked the
    /// whole process (I-038).
    queue: OnceLock<Arc<WorkerQueue>>,
}

impl Shared {
    pub fn new() -> (Arc<Self>, mpsc::Receiver<Frame>) {
        let (tx, rx) = mpsc::channel(SEND_BUFFER);
        (
            Arc::new(Self {
                incoming: Mutex::new(VecDeque::new()),
                waiter: Mutex::new(None),
                close_waiters: Mutex::new(Vec::new()),
                outgoing: tx,
                closed: AtomicBool::new(false),
                queue: OnceLock::new(),
            }),
            rx,
        )
    }

    /// Bound once, when the connection is handed to a worker.
    pub fn bind_queue(&self, queue: Arc<WorkerQueue>) {
        let _ = self.queue.set(queue);
    }

    /// Hand a callback to the worker's loop. Called from the tokio task, so it
    /// must not touch the interpreter: moving the handle into the queue does
    /// not, and the loop runs it from the drain callback.
    fn schedule(&self, callback: Py<PyAny>) {
        if let Some(queue) = self.queue.get() {
            queue.push_wakeup(callback);
        }
    }

    fn wake(&self) {
        let waiter = self.waiter.lock().ok().and_then(|mut w| w.take());
        if let Some((_event_loop, callback)) = waiter {
            self.schedule(callback);
        }
    }

    fn deliver(&self, frame: Frame) {
        if let Ok(mut queue) = self.incoming.lock() {
            queue.push_back(frame);
        }
        self.wake();
    }

    pub fn mark_closed(&self) {
        self.closed.store(true, Ordering::SeqCst);
        self.wake();
        let waiting = self
            .close_waiters
            .lock()
            .map(|mut w| std::mem::take(&mut *w))
            .unwrap_or_default();
        for (_event_loop, callback) in waiting {
            self.schedule(callback);
        }
    }
}

/// The handler's view of a live connection.
#[pyclass(frozen, name = "WebSocket", module = "oxbrook._core")]
pub struct WebSocket {
    shared: Arc<Shared>,
}

impl WebSocket {
    pub fn new(shared: Arc<Shared>) -> Self {
        Self { shared }
    }
}

#[pymethods]
impl WebSocket {
    /// Next queued message, or None if nothing is waiting right now.
    ///
    /// Text arrives as `str` and binary as `bytes`, which is how the Python
    /// side tells them apart without a wrapper object per message.
    fn try_receive<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyAny>> {
        let frame = self.shared.incoming.lock().ok()?.pop_front()?;
        match frame {
            Frame::Text(text) => Some(text.into_pyobject(py).ok()?.into_any()),
            Frame::Binary(bytes) => Some(PyBytes::new(py, &bytes).into_any()),
            Frame::Close(_) => None,
        }
    }

    /// Ask to be called back on `event_loop` when a message arrives or the
    /// socket closes. Fires immediately if either already happened.
    fn notify(&self, py: Python<'_>, event_loop: Py<PyAny>, callback: Py<PyAny>) -> PyResult<()> {
        if let Ok(mut waiter) = self.shared.waiter.lock() {
            *waiter = Some((event_loop.clone_ref(py), callback.clone_ref(py)));
        }
        // A message may have landed between the caller's check and installing
        // the waiter, which would otherwise wait forever for an event already
        // delivered.
        let ready = self
            .shared
            .incoming
            .lock()
            .map(|q| !q.is_empty())
            .unwrap_or(true)
            || self.shared.closed.load(Ordering::SeqCst);
        if ready {
            let pending = self.shared.waiter.lock().ok().and_then(|mut w| w.take());
            if let Some((event_loop, callback)) = pending {
                event_loop.call_method1(py, "call_soon_threadsafe", (callback,))?;
            }
        }
        Ok(())
    }

    /// Call `callback` on `event_loop` when the socket closes. Fires straight
    /// away if it already has.
    fn on_close(&self, py: Python<'_>, event_loop: Py<PyAny>, callback: Py<PyAny>) -> PyResult<()> {
        if self.shared.closed.load(Ordering::SeqCst) {
            event_loop.call_method1(py, "call_soon_threadsafe", (callback,))?;
            return Ok(());
        }
        if let Ok(mut waiting) = self.shared.close_waiters.lock() {
            waiting.push((event_loop.clone_ref(py), callback.clone_ref(py)));
        }
        // Re-check, in case the socket closed while the waiter was being added.
        if self.shared.closed.load(Ordering::SeqCst) {
            self.shared.mark_closed();
        }
        Ok(())
    }

    /// True when the message queue is empty and the socket is finished.
    #[getter]
    fn closed(&self) -> bool {
        self.shared.closed.load(Ordering::SeqCst)
            && self
                .shared
                .incoming
                .lock()
                .map(|q| q.is_empty())
                .unwrap_or(true)
    }

    /// Queue a text message. False means the socket is gone.
    fn send_text(&self, text: String) -> bool {
        self.shared.outgoing.try_send(Frame::Text(text)).is_ok()
    }

    /// Queue a binary message. False means the socket is gone.
    fn send_bytes(&self, data: &[u8]) -> bool {
        self.shared
            .outgoing
            .try_send(Frame::Binary(data.to_vec()))
            .is_ok()
    }

    /// Start a clean close handshake, with a status code and reason when
    /// given. Python checks both before they get here.
    #[pyo3(signature = (code = None, reason = String::new()))]
    fn close(&self, code: Option<u16>, reason: String) {
        let _ = self
            .shared
            .outgoing
            .try_send(Frame::Close(code.map(|code| (code, reason))));
    }
}

/// Own the socket until either side finishes.
pub async fn drive<S>(
    stream: WebSocketStream<S>,
    shared: Arc<Shared>,
    mut outgoing: mpsc::Receiver<Frame>,
) where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    let (mut sink, mut source) = stream.split();
    loop {
        tokio::select! {
            received = source.next() => match received {
                // Ping and Pong are answered by tungstenite itself; forwarding
                // them to the handler would only be noise.
                Some(Ok(Message::Text(text))) => shared.deliver(Frame::Text(text.to_string())),
                Some(Ok(Message::Binary(data))) => shared.deliver(Frame::Binary(data.to_vec())),
                Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break,
                Some(Ok(_)) => {}
            },
            queued = outgoing.recv() => {
                let message = match queued {
                    Some(Frame::Text(text)) => Message::Text(text.into()),
                    Some(Frame::Binary(data)) => Message::Binary(data.into()),
                    // Close, or the handler dropped its end of the socket.
                    Some(Frame::Close(status)) => {
                        let frame = status.map(|(code, reason)| CloseFrame {
                            code: CloseCode::from(code),
                            reason: reason.into(),
                        });
                        let _ = sink.send(Message::Close(frame)).await;
                        break;
                    }
                    // The handler dropped its end of the socket.
                    None => {
                        let _ = sink.send(Message::Close(None)).await;
                        break;
                    }
                };
                if sink.send(message).await.is_err() {
                    break;
                }
            }
        }
    }
    let _ = sink.close().await;
    shared.mark_closed();
}

/// What tungstenite reads into. Its default is 128 KiB, and it zero-fills the
/// spare capacity before every read, so an idle socket kept 128 KiB of touched
/// pages: about 156 KiB a connection all told, where 16 KiB echoes at the same
/// rate for small and 64 KiB messages alike. A frame larger than this still
/// arrives whole; the buffer is reserved to the frame's length once its header
/// is read.
const READ_BUFFER: usize = 16 * 1024;

/// Wrap an upgraded connection and run it to completion.
///
/// `max_message` caps a single incoming message. Without it tungstenite's own
/// default applies, which is 64 MiB — four times what the server accepts as a
/// request body, and not something the application had any way to change.
pub async fn serve<S>(
    io: S,
    shared: Arc<Shared>,
    outgoing: mpsc::Receiver<Frame>,
    max_message: usize,
) where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    let config = WebSocketConfig::default()
        .max_message_size(Some(max_message))
        .max_frame_size(Some(max_message))
        .read_buffer_size(READ_BUFFER);
    let stream = WebSocketStream::from_raw_socket(io, Role::Server, Some(config)).await;
    drive(stream, shared, outgoing).await;
}
