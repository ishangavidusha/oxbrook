use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full, StreamBody};
use hyper::body::{Frame, Incoming};
use hyper::header::{
    ALLOW, CONNECTION, CONTENT_LENGTH, CONTENT_TYPE, HOST, RETRY_AFTER, SEC_WEBSOCKET_ACCEPT,
    SEC_WEBSOCKET_KEY, SEC_WEBSOCKET_VERSION, UPGRADE,
};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Response, StatusCode, Version};
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use hyper_util::server::conn::auto;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::TcpListener;
use tokio::sync::{oneshot, Notify};
use tokio_stream::wrappers::ReceiverStream;
use tokio_stream::StreamExt;

use crate::body::BodyShared;
use crate::cancel::{Abandon, Cancel};
use crate::cors::{Cors, CorsTuple};
use crate::files::{Mount, MountTuple};
use crate::origin::{OriginsTuple, SocketOrigins};
use crate::queue::{ConnectionLoad, Pending};
use crate::responder::{Body, Reply};
use crate::router::{RouteError, RouteTuple, Router, SpecTuple};
use crate::tls::TlsTuple;
use crate::websocket;
use crate::worker::Worker;

struct State {
    router: Arc<Router>,
    workers: Vec<Worker>,
    next_worker: AtomicUsize,
    request_timeout: Option<Duration>,
    /// Cap on a request body. Without one, a single request can grow the
    /// process by several times the payload before the handler ever sees it.
    max_body: usize,
    /// Largest single WebSocket message accepted, in bytes.
    max_message: usize,
    /// None when the app configured no CORS, which costs one branch.
    cors: Option<Arc<Cors>>,
    /// Checked on every upgrade, before the authorizer or handler.
    socket_origins: SocketOrigins,
    /// Static mounts, indexed by `RouteSpec::mount`. Shared, because a file
    /// is resolved on a blocking thread that outlives the borrow.
    mounts: Vec<Arc<Mount>>,
}

#[pyclass(name = "Server", module = "oxbrook._core")]
pub struct Server {
    host: String,
    port: u16,
    worker_count: usize,
    max_concurrency: usize,
    max_body: usize,
    /// Largest single WebSocket message accepted, in bytes.
    max_message: usize,
    debug: bool,
    request_timeout: Option<Duration>,
    shutdown_grace: Duration,
    /// Lets something other than Ctrl-C stop the server. `serve` blocks, so a
    /// test harness needs a way in from another thread.
    stop: Arc<Notify>,
    max_connections: usize,
    quiet: bool,
    routes: Vec<Route>,
    /// `oxbrook._lifecycle.Lifecycle`: runs the worker lifespan on each loop.
    lifecycle: Py<PyAny>,
    cors: Option<CorsTuple>,
    socket_origins: OriginsTuple,
    mounts: Vec<MountTuple>,
    /// Certificate and key paths; None serves plain HTTP.
    tls: Option<TlsTuple>,
    http2: bool,
}

/// (method, path, handler, params, is_websocket, authorizer, streams_body,
/// cancel_on_disconnect)
type Route = (
    String,
    String,
    Py<PyAny>,
    Vec<SpecTuple>,
    bool,
    Option<Py<PyAny>>,
    bool,
    bool,
);

#[pymethods]
impl Server {
    #[new]
    /// Eighteen arguments, which clippy dislikes. This is the Python
    /// constructor: the signature *is* the API, and collapsing it into a
    /// config object would move the same fields behind a dict that Python has
    /// to build on every server start.
    #[allow(clippy::too_many_arguments)]
    fn new(
        host: String,
        port: u16,
        workers: usize,
        max_concurrency: usize,
        max_body: usize,
        max_message: usize,
        debug: bool,
        request_timeout_secs: f64,
        shutdown_grace_secs: f64,
        max_connections: usize,
        quiet: bool,
        routes: Vec<Route>,
        lifecycle: Py<PyAny>,
        cors: Option<CorsTuple>,
        socket_origins: OriginsTuple,
        mounts: Vec<MountTuple>,
        tls: Option<TlsTuple>,
        http2: bool,
    ) -> Self {
        Self {
            host,
            port,
            worker_count: workers.max(1),
            max_concurrency: max_concurrency.max(1),
            max_body,
            max_message,
            debug,
            // Zero disables the timeout, for a service whose handlers are
            // legitimately long-running.
            request_timeout: (request_timeout_secs > 0.0)
                .then(|| Duration::from_secs_f64(request_timeout_secs)),
            shutdown_grace: Duration::from_secs_f64(shutdown_grace_secs.max(0.0)),
            stop: Arc::new(Notify::new()),
            max_connections: max_connections.max(1),
            quiet,
            routes,
            lifecycle,
            cors,
            socket_origins,
            mounts,
            tls,
            http2,
        }
    }

    /// Ask a running server to stop accepting and drain. Safe to call from
    /// another thread, which is the point: `serve` blocks the one it is on.
    fn shutdown(&self) {
        self.stop.notify_waiters();
    }

    /// Start workers, bind, and serve until SIGINT, SIGTERM or `shutdown`.
    /// Blocks the calling thread but detaches from the interpreter for the
    /// duration.
    ///
    /// `handle_signals` is false when serving from a thread other than the
    /// main one, as the test client does. A signal handler, once installed,
    /// stays for the life of the process, so a test run that had started a
    /// server afterwards ignored SIGTERM and Ctrl-C altogether. Python itself
    /// only handles signals on the main thread, and this follows it.
    #[pyo3(signature = (handle_signals = true))]
    fn serve(&self, py: Python<'_>, handle_signals: bool) -> PyResult<()> {
        let handlers: Arc<Vec<Py<PyAny>>> = Arc::new(
            self.routes
                .iter()
                .map(|(_, _, handler, _, _, _, _, _)| handler.clone_ref(py))
                .collect(),
        );

        // Parallel to `handlers`: the optional pre-accept check for a socket.
        let gates: Arc<Vec<Option<Py<PyAny>>>> = Arc::new(
            self.routes
                .iter()
                .map(|(_, _, _, _, _, gate, _, _)| gate.as_ref().map(|g| g.clone_ref(py)))
                .collect(),
        );

        let specs: Vec<RouteTuple> = self
            .routes
            .iter()
            .map(
                |(method, path, _, params, websocket, gate, streaming, cancel)| {
                    (
                        method.clone(),
                        path.clone(),
                        params.clone(),
                        *websocket,
                        gate.is_some(),
                        *streaming,
                        *cancel,
                    )
                },
            )
            .collect();
        let mounts = self
            .mounts
            .iter()
            .cloned()
            .map(|spec| Mount::build(spec).map(Arc::new))
            .collect::<Result<Vec<_>, _>>()
            .map_err(PyValueError::new_err)?;
        let patterns: Vec<_> = mounts.iter().map(|m| m.patterns()).collect();
        let router = Arc::new(Router::build(&specs, &patterns).map_err(PyValueError::new_err)?);
        // Read before any worker starts, so a missing or mismatched
        // certificate is a startup error with nothing to tear down.
        let tls = self
            .tls
            .as_ref()
            .map(|files| crate::tls::acceptor(files, self.http2))
            .transpose()
            .map_err(PyValueError::new_err)?;
        let cors = self
            .cors
            .clone()
            .map(Cors::build)
            .transpose()
            .map_err(PyValueError::new_err)?
            .map(Arc::new);

        // Built before the workers, because each Responder needs a handle to
        // spawn its disconnect watcher on.
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("tokio runtime: {e}")))?;

        let mut workers: Vec<Worker> = Vec::with_capacity(self.worker_count);
        for i in 0..self.worker_count {
            let spawned = Worker::spawn(
                py,
                i,
                handlers.clone(),
                gates.clone(),
                router.clone(),
                self.max_concurrency,
                self.debug,
                runtime.handle().clone(),
                self.lifecycle.clone_ref(py),
            );
            match spawned {
                Ok(worker) => workers.push(worker),
                Err(e) => {
                    // A worker lifespan that fails usually fails everywhere —
                    // the database is down — so this is the ordinary startup
                    // failure, not a rare one. The loops that did start own
                    // resources, and must release them before the error
                    // surfaces rather than being abandoned mid-flight.
                    for worker in &workers {
                        worker.stop(py);
                    }
                    let deadline = Instant::now() + self.shutdown_grace;
                    for worker in &workers {
                        worker.join(py, deadline);
                    }
                    return Err(e);
                }
            }
        }

        let state = Arc::new(State {
            router,
            workers,
            next_worker: AtomicUsize::new(0),
            request_timeout: self.request_timeout,
            max_body: self.max_body,
            max_message: self.max_message,
            cors,
            socket_origins: SocketOrigins::build(self.socket_origins.clone()),
            mounts,
        });

        let addr: SocketAddr = format!("{}:{}", self.host, self.port)
            .parse()
            .map_err(|e| PyRuntimeError::new_err(format!("bad address: {e}")))?;

        let shutdown = state.clone();
        let listen = Listen {
            addr,
            stop: self.stop.clone(),
            max_connections: self.max_connections,
            quiet: self.quiet,
            handle_signals,
            tls,
            http2: self.http2,
        };
        let result: Result<(), String> = py.detach(|| runtime.block_on(serve_loop(listen, state)));

        // Draining. The listener has stopped, but connection tasks are still
        // on the runtime and handlers are still on the worker loops, so wait
        // for them rather than dropping their clients mid-request.
        let grace = self.shutdown_grace;
        let drained = py.detach(|| {
            let deadline = Instant::now() + grace;
            loop {
                let busy: usize = shutdown.workers.iter().map(|w| w.queue.load()).sum();
                if busy == 0 {
                    return true;
                }
                if Instant::now() >= deadline {
                    return false;
                }
                std::thread::sleep(Duration::from_millis(25));
            }
        });
        if !drained {
            let busy: usize = shutdown.workers.iter().map(|w| w.queue.load()).sum();
            eprintln!("oxbrook: shutdown grace expired with {busy} request(s) still in flight");
        }

        for w in &shutdown.workers {
            w.stop(py);
        }
        // Each worker runs its lifespan teardown after its loop stops. Wait for
        // them, with the same grace again as a bound, before dropping the
        // runtime and returning: otherwise the process teardown that follows
        // would run while pools were still closing, or the process would exit
        // first and cut them off.
        let deadline = Instant::now() + self.shutdown_grace;
        let unfinished = shutdown
            .workers
            .iter()
            .filter(|w| !w.join(py, deadline))
            .count();
        if unfinished > 0 {
            eprintln!(
                "oxbrook: {unfinished} worker(s) still tearing down after the shutdown grace"
            );
        }
        drop(runtime);

        result.map_err(PyRuntimeError::new_err)
    }
}

struct Listen {
    addr: SocketAddr,
    stop: Arc<Notify>,
    max_connections: usize,
    quiet: bool,
    handle_signals: bool,
    tls: Option<tokio_rustls::TlsAcceptor>,
    http2: bool,
}

/// How long a connection may hold a slot with nothing to show for it: request
/// headers not yet complete, a TLS handshake not yet finished, or, on HTTP/2,
/// no request in flight at all.
const IDLE: Duration = Duration::from_secs(15);

/// Streams one HTTP/2 client may have open at once, and handlers it may have
/// running at once. The second is the one that matters: a stream the client
/// resets is closed immediately, while its handler runs on, so a client that
/// opened and reset streams in a loop could start handlers without limit and
/// take every worker's capacity from one connection (CVE-2023-44487).
const MAX_STREAMS: u32 = 200;

/// The connection handler, built once and shared by every connection.
enum Protocols {
    /// HTTP/1.1 alone, as before HTTP/2 existed here: `http2=False`.
    Http1(http1::Builder),
    /// Either protocol, chosen per connection by the client's first bytes.
    Auto(auto::Builder<TokioExecutor>),
}

impl Protocols {
    fn build(http2: bool) -> Self {
        if !http2 {
            let mut builder = http1::Builder::new();
            builder
                // hyper panics on a timeout with no timer wired in, so this
                // line is load-bearing, not decorative.
                .timer(TokioTimer::new())
                // What stops a client from opening a connection and dribbling
                // request headers forever. It also closes an idle keep-alive
                // connection.
                .header_read_timeout(Some(IDLE));
            return Protocols::Http1(builder);
        }
        let mut builder = auto::Builder::new(TokioExecutor::new());
        builder
            .http1()
            .timer(TokioTimer::new())
            .header_read_timeout(Some(IDLE));
        builder
            .http2()
            .timer(TokioTimer::new())
            .max_concurrent_streams(MAX_STREAMS)
            // Pings find a peer that vanished without closing, which on a
            // quiet event stream nothing else would notice.
            .keep_alive_interval(Some(Duration::from_secs(20)))
            .keep_alive_timeout(Duration::from_secs(20));
        Protocols::Auto(builder)
    }
}

/// A connection's place under `max_connections`, shared with any WebSocket
/// the connection is upgraded into. hyper ends the connection task when it
/// hands the socket over, so a permit held by that task alone was released at
/// every handshake, and sockets were never counted.
#[derive(Clone)]
struct ConnectionSlot(#[allow(dead_code)] Arc<tokio::sync::OwnedSemaphorePermit>);

/// Only an upgrade can outlive its connection, so only an upgrade request
/// carries the slot. Everything else pays one header lookup.
fn lend_slot(req: &mut hyper::Request<Incoming>, slot: &ConnectionSlot) {
    if req.headers().contains_key(UPGRADE) {
        req.extensions_mut().insert(slot.clone());
    }
}

/// Requests on one HTTP/2 connection: how many are in flight, and how many
/// have started, which tells the idle check whether anything happened between
/// two of its looks without reading a clock per request.
#[derive(Default)]
struct Activity {
    in_flight: AtomicUsize,
    started: AtomicUsize,
    /// Handlers running, which outlives `in_flight` when a stream is reset.
    running: ConnectionLoad,
    /// Set by the first HTTP/1 request. hyper's header timeout already closes
    /// an idle HTTP/1 connection, so from then on nothing is tracked.
    http1: std::sync::atomic::AtomicBool,
}

impl Activity {
    fn begin(self: &Arc<Self>) -> Busy {
        self.in_flight.fetch_add(1, Ordering::Relaxed);
        self.started.fetch_add(1, Ordering::Relaxed);
        Busy(self.clone())
    }
}

/// Held by a request until its response body is dropped, which is when hyper
/// has finished sending it or the client has reset the stream.
struct Busy(Arc<Activity>);

impl Drop for Busy {
    fn drop(&mut self) {
        self.0.in_flight.fetch_sub(1, Ordering::Relaxed);
    }
}

async fn connection<S>(stream: S, protocols: &Protocols, state: Arc<State>, slot: ConnectionSlot)
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let io = TokioIo::new(stream);
    let builder = match protocols {
        Protocols::Http1(builder) => {
            let svc = service_fn(move |mut req| {
                lend_slot(&mut req, &slot);
                serve_request(req, state.clone(), None)
            });
            // `with_upgrades` is required for 101 responses to hand the
            // connection over instead of closing it.
            let _ = builder.serve_connection(io, svc).with_upgrades().await;
            return;
        }
        Protocols::Auto(builder) => builder,
    };

    let activity = Arc::new(Activity::default());
    let tracked = activity.clone();
    let svc = service_fn(move |mut req: hyper::Request<Incoming>| {
        lend_slot(&mut req, &slot);
        let (busy, running) = if req.version() == Version::HTTP_2 {
            (Some(tracked.begin()), Some(tracked.running.clone()))
        } else {
            // Read first: a store on every request would bounce the cache
            // line between threads for nothing.
            if !tracked.http1.load(Ordering::Relaxed) {
                tracked.http1.store(true, Ordering::Relaxed);
            }
            (None, None)
        };
        let state = state.clone();
        async move {
            let response = serve_request(req, state, running).await?;
            let Some(busy) = busy else {
                return Ok(response);
            };
            Ok::<_, Infallible>(response.map(|body| {
                body.map_frame(move |frame| {
                    let _held = &busy;
                    frame
                })
                .boxed()
            }))
        }
    });
    // With upgrades, so an HTTP/1.1 client can still open a WebSocket.
    let mut conn = std::pin::pin!(builder.serve_connection_with_upgrades(io, svc));

    // hyper has no idle timeout for HTTP/2, and none for the bytes it reads to
    // tell the protocols apart, so a client that connected and said nothing
    // held its slot until the process exited. This looks every fifth of IDLE:
    // a connection with nothing in flight and nothing started for a whole
    // IDLE is asked to close, and dropped if it is still idle one IDLE later.
    const LOOKS: u32 = 3;
    let mut ticks = tokio::time::interval(IDLE / LOOKS);
    ticks.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    ticks.tick().await;
    let mut seen = activity.started.load(Ordering::Relaxed);
    let mut quiet = 0;
    loop {
        tokio::select! {
            _ = conn.as_mut() => return,
            _ = ticks.tick() => {
                if activity.http1.load(Ordering::Relaxed) {
                    break;
                }
                let started = activity.started.load(Ordering::Relaxed);
                if started != seen || activity.in_flight.load(Ordering::Relaxed) > 0 {
                    seen = started;
                    quiet = 0;
                    continue;
                }
                quiet += 1;
                if quiet == LOOKS {
                    // GOAWAY on HTTP/2, so the client opens a new connection
                    // for its next request rather than seeing this one fail.
                    conn.as_mut().graceful_shutdown();
                } else if quiet == 2 * LOOKS {
                    return;
                }
            }
        }
    }
    let _ = conn.await;
}

/// How the operating system asks this process to stop, besides Ctrl-C, which
/// `tokio::signal::ctrl_c` handles on both platforms. Either way the server
/// gets the same graceful drain rather than dying where it stands: unhandled,
/// the default action killed the process outright, with in-flight requests cut
/// off and lifespan teardown never run.
///
/// On Unix that is SIGTERM, what a container runtime, systemd or Kubernetes
/// sends. Windows has no SIGTERM: a supervisor stops a child with Ctrl-Break,
/// which is what `oxb run --reload` sends, and closing the console window
/// arrives as its own event with a few seconds to drain before the process is
/// killed regardless.
#[cfg(unix)]
struct Terminate(tokio::signal::unix::Signal);

#[cfg(windows)]
struct Terminate(
    tokio::signal::windows::CtrlBreak,
    tokio::signal::windows::CtrlClose,
    tokio::signal::windows::CtrlShutdown,
);

impl Terminate {
    #[cfg(unix)]
    fn install() -> Result<Self, String> {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .map(Self)
            .map_err(|e| format!("installing the SIGTERM handler: {e}"))
    }

    #[cfg(windows)]
    fn install() -> Result<Self, String> {
        let describe =
            |what: &str, e: std::io::Error| format!("installing the {what} handler: {e}");
        Ok(Self(
            tokio::signal::windows::ctrl_break().map_err(|e| describe("Ctrl-Break", e))?,
            tokio::signal::windows::ctrl_close().map_err(|e| describe("console close", e))?,
            tokio::signal::windows::ctrl_shutdown().map_err(|e| describe("shutdown", e))?,
        ))
    }

    #[cfg(unix)]
    async fn recv(&mut self) {
        self.0.recv().await;
    }

    #[cfg(windows)]
    async fn recv(&mut self) {
        tokio::select! {
            _ = self.0.recv() => {}
            _ = self.1.recv() => {}
            _ = self.2.recv() => {}
        }
    }
}

async fn serve_loop(listen: Listen, state: Arc<State>) -> Result<(), String> {
    let Listen {
        addr,
        stop,
        max_connections,
        quiet,
        handle_signals,
        tls,
        http2,
    } = listen;
    let listener = TcpListener::bind(addr)
        .await
        .map_err(|e| format!("bind {addr}: {e}"))?;
    if !quiet {
        let scheme = if tls.is_some() { "https" } else { "http" };
        println!("Oxbrook listening on {scheme}://{addr}");
    }
    let protocols = Arc::new(Protocols::build(http2));

    let mut terminate = if handle_signals {
        Some(Terminate::install()?)
    } else {
        None
    };

    // `max_concurrency` bounds requests handed to a worker, which is not the
    // same as sockets held open. An idle keep-alive connection costs a file
    // descriptor and buffers without ever reaching a worker, so it needs its
    // own limit.
    let connections = Arc::new(tokio::sync::Semaphore::new(max_connections));

    loop {
        // Taken before accepting, so at the limit the listener simply stops
        // accepting and the OS backlog absorbs the wait. That is the shape of
        // backpressure a client understands.
        let permit = tokio::select! {
            slot = connections.clone().acquire_owned() => match slot {
                Ok(permit) => permit,
                Err(_) => break,
            },
            _ = stop.notified() => break,
        };

        tokio::select! {
            accepted = listener.accept() => {
                let Ok((stream, _)) = accepted else { continue };
                let _ = stream.set_nodelay(true);
                let state = state.clone();
                let protocols = protocols.clone();
                let tls = tls.clone();
                tokio::spawn(async move {
                    // Released when the connection task ends, or when the
                    // last socket upgraded from it closes, whichever is later.
                    let slot = ConnectionSlot(Arc::new(permit));
                    match tls {
                        None => connection(stream, &protocols, state, slot).await,
                        // Bounded like request headers: a client that connects
                        // and never finishes the handshake holds a slot too.
                        Some(acceptor) => {
                            if let Ok(Ok(stream)) =
                                tokio::time::timeout(IDLE, acceptor.accept(stream)).await
                            {
                                connection(stream, &protocols, state, slot).await;
                            }
                        }
                    }
                });
            }
            _ = async {
                if handle_signals {
                    let _ = tokio::signal::ctrl_c().await;
                } else {
                    std::future::pending::<()>().await
                }
            } => break,
            _ = async {
                match terminate.as_mut() {
                    Some(signals) => signals.recv().await,
                    None => std::future::pending::<()>().await,
                }
            } => break,
            _ = stop.notified() => break,
        }
    }
    Ok(())
}

/// Responses are either a complete buffer or a stream of chunks, so every
/// helper hands back the same boxed body type.
pub(crate) type Out = BoxBody<Bytes, Infallible>;

pub(crate) fn full(bytes: Bytes) -> Out {
    Full::new(bytes).boxed()
}

fn plain(status: StatusCode, msg: &'static str) -> Response<Out> {
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, "text/plain")
        .body(full(Bytes::from_static(msg.as_bytes())))
        .unwrap()
}

fn json(status: StatusCode, body: Vec<u8>) -> Response<Out> {
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, "application/json")
        .body(full(Bytes::from(body)))
        .unwrap()
}

fn overloaded() -> Response<Out> {
    Response::builder()
        .status(StatusCode::SERVICE_UNAVAILABLE)
        .header(CONTENT_TYPE, "text/plain")
        .header(RETRY_AFTER, "1")
        .body(full(Bytes::from_static(b"server overloaded")))
        .unwrap()
}

/// Path parameters are consumed by whichever Pending takes them, so the gate
/// needs its own copy before the handler's.
fn clone_param(value: &crate::router::ParamValue) -> crate::router::ParamValue {
    use crate::router::ParamValue as V;
    match value {
        V::Str(v) => V::Str(v.clone()),
        V::Int(v) => V::Int(*v),
        V::Float(v) => V::Float(*v),
        V::Bool(v) => V::Bool(*v),
        V::Uuid(v) => V::Uuid(v.clone()),
        V::Date(v) => V::Date(v.clone()),
        V::DateTime(v) => V::DateTime(v.clone()),
        V::List(items) => V::List(items.iter().map(clone_param).collect()),
        V::Null => V::Null,
        V::Omit => V::Omit,
    }
}

fn too_large() -> Response<Out> {
    Response::builder()
        .status(StatusCode::PAYLOAD_TOO_LARGE)
        .header(CONTENT_TYPE, "text/plain")
        .body(full(Bytes::from_static(b"request body too large")))
        .unwrap()
}

fn method_not_allowed(allow: String) -> Response<Out> {
    Response::builder()
        .status(StatusCode::METHOD_NOT_ALLOWED)
        .header(CONTENT_TYPE, "text/plain")
        .header(ALLOW, allow)
        .body(full(Bytes::from_static(b"method not allowed")))
        .unwrap()
}

/// Wait for a streaming route's reply while pumping its body.
///
/// The body is pumped by this same future, so it can never outlive the
/// request: when the reply arrives the pump is dropped, and a handler still
/// reading learns the body ended early.
///
/// The request timeout counts from the body's last progress rather than from
/// dispatch. A large upload that keeps moving is not cut off; a handler that
/// stops reading, or stops answering once the body is done, still is. None
/// means that timeout passed.
async fn wait_while_streaming<F>(
    mut reply: oneshot::Receiver<Reply>,
    mut pump: std::pin::Pin<Box<F>>,
    shared: &BodyShared,
    limit: Option<Duration>,
) -> Option<Result<Reply, oneshot::error::RecvError>>
where
    F: std::future::Future<Output = ()>,
{
    let mut pumping = true;
    loop {
        // While the pump waits on the client, its own idle timeout is the one
        // that applies; this one only measures a handler that has stalled.
        let deadline = limit.map(|l| {
            let from = if shared.waiting_on_client() {
                Instant::now()
            } else {
                shared.last_progress()
            };
            tokio::time::Instant::from_std(from + l)
        });
        tokio::select! {
            result = &mut reply => return Some(result),
            _ = pump.as_mut(), if pumping => pumping = false,
            _ = async {
                match deadline {
                    Some(at) => tokio::time::sleep_until(at).await,
                    None => std::future::pending::<()>().await,
                }
            } => {
                // Progress may have been made while this timer was set, or the
                // wait may belong to the client rather than the handler.
                let stalled = limit.is_some_and(|l| shared.last_progress() + l <= Instant::now());
                if stalled && !shared.waiting_on_client() {
                    return None;
                }
            }
        }
    }
}

/// Hand the request to the least-loaded worker with room, and say which one
/// took it. None means every worker is at its limit.
///
/// Round-robin alone sent every Nth request to a loop held by a handler that
/// computes rather than awaits, where it waited out the whole computation while
/// the other loops sat idle: one such handler on four loops put a quarter of all
/// requests up to its full hold time behind it (I-018). Load is queued plus
/// in-flight, and a held loop cannot drain, so its load climbs with each request
/// it is given and the scan steers the next ones elsewhere.
///
/// The scan starts at a rotating offset so ties still spread evenly, and stops
/// at the first idle worker, which on a lightly loaded server is the first one
/// it looks at.
fn enqueue(state: &State, pending: Pending) -> Option<usize> {
    let mut pending = pending;
    let rotation = state.next_worker.fetch_add(1, Ordering::Relaxed);
    let count = state.workers.len();

    let mut start = rotation % count;
    let mut lightest = usize::MAX;
    for offset in 0..count {
        let idx = (rotation + offset) % count;
        let load = state.workers[idx].queue.load();
        if load < lightest {
            lightest = load;
            start = idx;
            if load == 0 {
                break;
            }
        }
    }

    // Lightest first, then round from there: the load read above is already
    // stale by the time the push happens, so a full worker still spills over.
    for offset in 0..count {
        let idx = (start + offset) % count;
        // Cheap: None for every ordinary request, one Arc clone for an upgrade
        // or a streaming body.
        let socket = pending.websocket.clone();
        let body = pending.body_stream.clone();
        match state.workers[idx].queue.try_push(pending) {
            Ok(()) => {
                // The socket's task, or the body pump, wakes the handler
                // through this queue, so it has to know which worker took the
                // request before the handler can register a waiter.
                if let Some(shared) = socket {
                    shared.bind_queue(state.workers[idx].queue.clone());
                }
                if let Some(shared) = body {
                    shared.bind_queue(state.workers[idx].queue.clone());
                }
                return Some(idx);
            }
            Err(returned) => pending = returned,
        }
    }
    None
}

/// Complete a WebSocket handshake and hand the socket to a handler.
///
/// The 101 goes out from here rather than from the handler, because hyper only
/// yields the upgraded connection after the response has been written.
async fn upgrade_websocket(
    mut req: hyper::Request<Incoming>,
    matched: crate::router::Matched,
    state: Arc<State>,
) -> Response<Out> {
    let headers = req.headers();
    let upgrading = headers
        .get(UPGRADE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case("websocket"))
        && headers
            .get(CONNECTION)
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.to_ascii_lowercase().contains("upgrade"));

    let version_ok = headers
        .get(SEC_WEBSOCKET_VERSION)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.trim() == "13");

    let key = headers.get(SEC_WEBSOCKET_KEY).cloned();

    let (Some(key), true, true) = (key, upgrading, version_ok) else {
        // A plain GET to a socket route is a client mistake worth naming.
        return Response::builder()
            .status(StatusCode::UPGRADE_REQUIRED)
            .header(CONTENT_TYPE, "text/plain")
            .header(SEC_WEBSOCKET_VERSION, "13")
            .body(full(Bytes::from_static(
                b"this endpoint speaks websocket; send an Upgrade request",
            )))
            .unwrap();
    };

    // Before anything application-level runs: a page on another site must not
    // be able to reach an authorizer or handler with the user's cookies.
    if !state.socket_origins.permits(req.headers()) {
        return plain(
            StatusCode::FORBIDDEN,
            "websocket origin not allowed; list it in App(websocket_origins=[...])",
        );
    }

    // Ask the application before switching protocols. Once the 101 is sent it
    // is too late to refuse, which is why this cannot be left to the handler.
    if state.router.spec(matched.route).gated {
        let (verdict_tx, verdict_rx) = oneshot::channel::<Reply>();
        let queued = enqueue(
            &state,
            Pending {
                route: matched.route,
                params: matched.params.iter().map(clone_param).collect(),
                method: req.method().as_str().to_owned(),
                path: req.uri().path().to_owned(),
                query: req.uri().query().map(str::to_owned),
                body: Vec::new(),
                headers: req.headers().clone(),
                reply: verdict_tx,
                websocket: None,
                gate: true,
                body_stream: None,
                connection: None,
                cancel: None,
            },
        );
        if queued.is_none() {
            return overloaded();
        }

        let verdict = match state.request_timeout {
            Some(limit) => match tokio::time::timeout(limit, verdict_rx).await {
                Ok(result) => result,
                Err(_) => return plain(StatusCode::GATEWAY_TIMEOUT, "authorizer timed out"),
            },
            None => verdict_rx.await,
        };

        match verdict {
            // 101 from the authorizer means "go ahead"; the real handshake
            // response is built below.
            Ok(reply) if reply.status == 101 => {}
            Ok(reply) => {
                let body = match reply.body {
                    Body::Full(bytes) => Bytes::from(bytes),
                    Body::Stream(..) => Bytes::new(),
                };
                let mut builder = Response::builder()
                    .status(reply.status)
                    .header(CONTENT_TYPE, reply.content_type);
                for (name, value) in &reply.headers {
                    builder = builder.header(name.as_str(), value.as_str());
                }
                return builder
                    .body(full(body))
                    .unwrap_or_else(|_| plain(StatusCode::FORBIDDEN, "refused"));
            }
            Err(_) => {
                return plain(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "authorizer did not answer",
                )
            }
        }
    }

    let accept = tokio_tungstenite::tungstenite::handshake::derive_accept_key(key.as_bytes());
    let (shared, outgoing) = websocket::Shared::new();

    // The reply channel goes nowhere: the response is built here, and the
    // Responder exists only so the in-flight count is released on drop.
    let (reply_tx, reply_rx) = oneshot::channel();
    drop(reply_rx);

    let queued = enqueue(
        &state,
        Pending {
            route: matched.route,
            params: matched.params,
            method: req.method().as_str().to_owned(),
            path: req.uri().path().to_owned(),
            query: req.uri().query().map(str::to_owned),
            body: Vec::new(),
            headers: req.headers().clone(),
            reply: reply_tx,
            websocket: Some(shared.clone()),
            gate: false,
            body_stream: None,
            connection: None,
            cancel: None,
        },
    );
    if queued.is_none() {
        return overloaded();
    }

    let upgrade = hyper::upgrade::on(&mut req);
    let slot = req.extensions_mut().remove::<ConnectionSlot>();
    // Copied out before the move: `state` does not travel into the task.
    let max_message = state.max_message;
    tokio::spawn(async move {
        // The socket holds its connection's place until it closes.
        let _slot = slot;
        match upgrade.await {
            Ok(upgraded) => {
                websocket::serve(TokioIo::new(upgraded), shared, outgoing, max_message).await;
            }
            // The handler is already running; tell it the socket never opened.
            Err(_) => shared.mark_closed(),
        }
    });

    Response::builder()
        .status(StatusCode::SWITCHING_PROTOCOLS)
        .header(UPGRADE, "websocket")
        .header(CONNECTION, "Upgrade")
        .header(SEC_WEBSOCKET_ACCEPT, accept)
        .body(full(Bytes::new()))
        .unwrap()
}

/// CORS around routing. Without a CORS policy this is a single branch.
async fn serve_request(
    req: hyper::Request<Incoming>,
    state: Arc<State>,
    connection: Option<ConnectionLoad>,
) -> Result<Response<Out>, Infallible> {
    let Some(cors) = state.cors.clone() else {
        return handle(req, state, connection).await;
    };
    if Cors::is_preflight(req.method(), req.headers()) {
        return Ok(match cors.preflight(req.headers()) {
            Ok(headers) => {
                let mut response = Response::builder()
                    .status(StatusCode::NO_CONTENT)
                    .body(full(Bytes::new()))
                    .unwrap();
                response.headers_mut().extend(headers);
                response
            }
            // Refused with a reason rather than a bare 204 with no headers:
            // the browser fails either way, and only this one says why.
            Err(reason) => {
                let mut response = plain(StatusCode::BAD_REQUEST, reason);
                cors.decorate(None, response.headers_mut());
                response
            }
        });
    }
    let origin = req.headers().get(hyper::header::ORIGIN).cloned();
    let mut response = handle(req, state, connection).await?;
    // A socket upgrade is not subject to CORS; an authorizer checks `Origin`.
    if response.status() != StatusCode::SWITCHING_PROTOCOLS {
        cors.decorate(origin.as_ref(), response.headers_mut());
    }
    Ok(response)
}

/// A place in an HTTP/2 connection's handler count, given back if the request
/// is answered before it reaches a worker.
struct Reservation(Option<ConnectionLoad>);

impl Reservation {
    /// None when the connection already has `MAX_STREAMS` handlers running.
    fn take(connection: Option<ConnectionLoad>) -> Option<Self> {
        if let Some(load) = &connection {
            if load.fetch_add(1, Ordering::Relaxed) >= MAX_STREAMS as usize {
                load.fetch_sub(1, Ordering::Relaxed);
                return None;
            }
        }
        Some(Self(connection))
    }

    /// The count now belongs to the request's `Responder`.
    fn hand_over(mut self) -> Option<ConnectionLoad> {
        self.0.take()
    }
}

impl Drop for Reservation {
    fn drop(&mut self) {
        if let Some(load) = &self.0 {
            load.fetch_sub(1, Ordering::Relaxed);
        }
    }
}

async fn handle(
    mut req: hyper::Request<Incoming>,
    state: Arc<State>,
    connection: Option<ConnectionLoad>,
) -> Result<Response<Out>, Infallible> {
    // HTTP/2 carries the host in the request line's `:authority`, which hyper
    // puts in the URI and not in the headers. Copied across, so a handler
    // reads `host` the same way whichever protocol the client chose.
    if req.version() == Version::HTTP_2 && !req.headers().contains_key(HOST) {
        if let Some(value) = req
            .uri()
            .authority()
            .and_then(|a| hyper::header::HeaderValue::from_str(a.as_str()).ok())
        {
            req.headers_mut().insert(HOST, value);
        }
    }

    // HTTP requires HEAD wherever GET is allowed, so a miss on HEAD retries as
    // GET and the body is dropped from the reply below.
    let head = req.method() == Method::HEAD;
    let found = state
        .router
        .find(req.method().as_str(), req.uri().path(), req.uri().query());
    let found = match found {
        Err(RouteError::NotFound) | Err(RouteError::MethodNotAllowed(_)) if head => state
            .router
            .find("GET", req.uri().path(), req.uri().query())
            // A HEAD cannot open a socket, so leave upgrade routes to fail.
            .and_then(|m| {
                if state.router.spec(m.route).websocket {
                    Err(RouteError::MethodNotAllowed("GET".to_owned()))
                } else {
                    Ok(m)
                }
            })
            .or(found),
        other => other,
    };

    let matched = match found {
        Ok(matched) => matched,
        Err(RouteError::NotFound) => return Ok(plain(StatusCode::NOT_FOUND, "not found")),
        Err(RouteError::MethodNotAllowed(allow)) => return Ok(method_not_allowed(allow)),
        // Coercion runs here, so a bad path parameter never wakes a worker.
        Err(RouteError::BadParam(err)) => {
            return Ok(json(StatusCode::UNPROCESSABLE_ENTITY, err.to_json()))
        }
    };

    // A static file never reaches a worker, and its body, if any, is ignored.
    if let Some(mount) = state.router.spec(matched.route).mount {
        return Ok(crate::files::serve(
            state.mounts[mount].clone(),
            matched.file.as_deref(),
            req.uri().path(),
            req.uri().query(),
            req.headers(),
            head,
        )
        .await);
    }

    if state.router.spec(matched.route).websocket {
        return Ok(upgrade_websocket(req, matched, state).await);
    }

    // Refused before the body is read, since reading it is work too.
    let Some(reservation) = Reservation::take(connection) else {
        return Ok(overloaded());
    };

    let method = req.method().as_str().to_owned();
    let path = req.uri().path().to_owned();
    let query = req.uri().query().map(str::to_owned);
    // Moved, not copied: handing the whole map over costs nothing, and a
    // handler that never reads a header never pays to convert one.
    let headers = std::mem::take(req.headers_mut());

    // A streaming route is handed to its worker before the body is read; any
    // other waits here until the body is complete and within the limit.
    let (body, stream, pump) = if state.router.spec(matched.route).streaming {
        let declared = headers
            .get(CONTENT_LENGTH)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<u64>().ok());
        let shared = BodyShared::new();
        let pump = Box::pin(crate::body::pump(
            req.into_body(),
            declared,
            shared.clone(),
            state.max_body,
            state.request_timeout,
        ));
        (Vec::new(), Some(shared), Some(pump))
    } else {
        match crate::body::collect_bounded(req.into_body(), state.max_body).await {
            Ok(collected) => (collected, None, None),
            Err(crate::body::Failure::TooLarge(_)) => return Ok(too_large()),
            Err(_) => return Ok(plain(StatusCode::BAD_REQUEST, "bad body")),
        }
    };

    let (reply_tx, reply_rx) = oneshot::channel::<Reply>();
    let connection = reservation.hand_over();
    let cancel = state
        .router
        .spec(matched.route)
        .cancellable
        .then(Cancel::new);
    let pending = Pending {
        route: matched.route,
        params: matched.params,
        method,
        path,
        query,
        body,
        headers,
        reply: reply_tx,
        websocket: None,
        gate: false,
        body_stream: stream.clone(),
        connection: connection.clone(),
        cancel: cancel.clone(),
    };

    // No Python involvement on this thread: plain Rust data plus one byte
    // written to the worker's wake socket.
    let Some(worker) = enqueue(&state, pending) else {
        // The request was dropped with its copy of the count unreleased.
        if let Some(load) = connection {
            load.fetch_sub(1, Ordering::Relaxed);
        }
        // Every worker is at its limit. Shed the request now rather than let it
        // wait behind work the server has already failed to keep up with.
        return Ok(overloaded());
    };
    // From here until the first reply, this future ending for any reason —
    // the client closing, an HTTP/2 reset, the timeout below — abandons the
    // request, and the worker cancels its handler.
    let abandon = cancel.map(|cell| Abandon::new(cell, &state.workers[worker].queue));

    // Waiting only for the *first* reply, so a long-lived SSE stream is not
    // affected: its headers go out as soon as the handler starts streaming.
    let replied = match (pump, stream) {
        (Some(pump), Some(shared)) => {
            wait_while_streaming(reply_rx, pump, &shared, state.request_timeout).await
        }
        _ => match state.request_timeout {
            Some(limit) => tokio::time::timeout(limit, reply_rx).await.ok(),
            None => Some(reply_rx.await),
        },
    };
    let Some(replied) = replied else {
        return Ok(plain(
            StatusCode::GATEWAY_TIMEOUT,
            "handler did not respond in time",
        ));
    };

    match replied {
        Ok(reply) => {
            if let Some(abandon) = abandon {
                abandon.answered();
            }
            // A HEAD reply carries the headers a GET would, including the
            // length it would have had, but no body.
            if head {
                let length = match &reply.body {
                    Body::Full(bytes) => bytes.len(),
                    Body::Stream(..) => 0,
                };
                let mut builder = Response::builder()
                    .status(reply.status)
                    .header(CONTENT_TYPE, reply.content_type)
                    .header(CONTENT_LENGTH, length);
                for (name, value) in &reply.headers {
                    builder = builder.header(name.as_str(), value.as_str());
                }
                return Ok(builder.body(full(Bytes::new())).unwrap_or_else(|_| {
                    plain(StatusCode::INTERNAL_SERVER_ERROR, "bad response header")
                }));
            }

            let body = match reply.body {
                Body::Full(bytes) => full(Bytes::from(bytes)),
                // Headers go out now; chunks follow as the handler produces
                // them, which is what makes SSE possible. `guard` is moved into
                // the closure so it lives exactly as long as the body, and its
                // drop is what tells the handler the client has gone.
                Body::Stream(rx, guard) => {
                    StreamBody::new(ReceiverStream::new(rx).map(move |chunk| {
                        let _keep_alive = &guard;
                        Ok::<_, Infallible>(Frame::data(chunk))
                    }))
                    .boxed()
                }
            };
            let mut builder = Response::builder()
                .status(reply.status)
                .header(CONTENT_TYPE, reply.content_type);
            for (name, value) in &reply.headers {
                builder = builder.header(name.as_str(), value.as_str());
            }
            // A handler-supplied header could be malformed; fall back rather
            // than kill the connection.
            Ok(builder.body(body).unwrap_or_else(|_| {
                plain(StatusCode::INTERNAL_SERVER_ERROR, "bad response header")
            }))
        }
        Err(_) => Ok(plain(
            StatusCode::INTERNAL_SERVER_ERROR,
            "handler finished without responding",
        )),
    }
}
