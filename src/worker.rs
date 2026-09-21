use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::queue::WorkerQueue;
use crate::request::Request;
use crate::responder::Responder;
use crate::router::{ParamValue, Router};
use crate::wake::{self, WakeReader};
use crate::websocket::WebSocket;

/// Callable handed to `loop.add_reader`. asyncio invokes it on the worker's own
/// thread whenever the wake socket becomes readable, and it drains every queued
/// request in that one callback.
#[pyclass(frozen, name = "Drainer", module = "oxbrook._core")]
struct Drainer {
    queue: Arc<WorkerQueue>,
    routes: Arc<Vec<Py<PyAny>>>,
    gates: Arc<Vec<Option<Py<PyAny>>>>,
    router: Arc<Router>,
    /// `oxbrook._runtime.run_handler`, an async function.
    run_handler: Py<PyAny>,
    /// `oxbrook._runtime.run_websocket`, for upgraded connections.
    run_websocket: Py<PyAny>,
    /// Constructors for parameter types Rust validated but cannot build.
    make_uuid: Py<PyAny>,
    make_date: Py<PyAny>,
    make_datetime: Py<PyAny>,
    /// Bound `loop.create_task`.
    create_task: Py<PyAny>,
    /// Returned to the client in a 500 when set. Per server, never global.
    debug: bool,
    /// Read end of the wake pair.
    reader: WakeReader,
    /// Handed to each `Responder` so streams can watch for disconnects.
    runtime: tokio::runtime::Handle,
    /// This loop's `WorkerContext`: the app, and the state its lifespans
    /// yielded. Every request on the loop shares it.
    context: Py<PyAny>,
}

impl Drainer {
    /// Turn a coerced parameter into a Python object.
    ///
    /// The constructors are looked up once, at worker start, rather than per
    /// request.
    fn to_python<'py>(&self, py: Python<'py>, value: ParamValue) -> PyResult<Bound<'py, PyAny>> {
        Ok(match value {
            ParamValue::Str(v) => v.into_pyobject(py)?.into_any(),
            ParamValue::Int(v) => v.into_pyobject(py)?.into_any(),
            ParamValue::Float(v) => v.into_pyobject(py)?.into_any(),
            ParamValue::Bool(v) => v.into_pyobject(py)?.to_owned().into_any(),
            ParamValue::Uuid(v) => self.make_uuid.bind(py).call1((v,))?,
            ParamValue::Date(v) => self.make_date.bind(py).call1((v,))?,
            ParamValue::DateTime(v) => self.make_datetime.bind(py).call1((v,))?,
            ParamValue::List(items) => {
                let list = PyList::empty(py);
                for item in items {
                    list.append(self.to_python(py, item)?)?;
                }
                list.into_any()
            }
            ParamValue::Null | ParamValue::Omit => py.None().into_bound(py),
        })
    }
}

#[pymethods]
impl Drainer {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        self.reader.drain();

        // Order matters: clear before popping, so a push that races with this
        // drain writes a new wake byte rather than being silently swallowed.
        self.queue.clear_notified();

        // Notifications first: they are cheap, and one of them is a stream
        // learning its client is gone, which frees a subscription and a slot.
        // A raising callback must not abandon the rest of the batch.
        while let Some(callback) = self.queue.pop_wakeup() {
            if let Err(err) = callback.call0(py) {
                err.write_unraisable(py, None);
            }
        }

        // Cancellations before new work, so a handler whose client is gone
        // stops before more are started beside it.
        while let Some(cell) = self.queue.pop_cancel() {
            if let Err(err) = cell.cancel(py) {
                err.write_unraisable(py, None);
            }
        }

        let run_handler = self.run_handler.bind(py);
        let create_task = self.create_task.bind(py);

        while let Some(item) = self.queue.pop() {
            // The client left while this waited in the queue: nothing has
            // started, so there is nothing to stop and nothing to run.
            if item.cancel.as_ref().is_some_and(|cell| cell.abandoned()) {
                if let Some(load) = &item.connection {
                    load.fetch_sub(1, std::sync::atomic::Ordering::Relaxed);
                }
                continue;
            }
            self.queue.claim();
            let handler = match (item.gate, self.gates[item.route].as_ref()) {
                (true, Some(gate)) => gate.bind(py),
                _ => self.routes[item.route].bind(py),
            };
            let spec = self.router.spec(item.route);

            // Handlers with no path parameters skip the dict entirely, so the
            // hello-world path costs exactly what it did before routing existed.
            let params = if spec.params.is_empty() {
                None
            } else {
                let dict = PyDict::new(py);
                for (param, value) in spec.params.iter().zip(item.params) {
                    match value {
                        // Left out on purpose: the handler's own default applies.
                        ParamValue::Omit => {}
                        other => dict.set_item(&param.name, self.to_python(py, other)?)?,
                    }
                }
                Some(dict)
            };

            let request = Py::new(
                py,
                Request {
                    method: item.method,
                    path: item.path,
                    query: item.query,
                    body: item.body,
                    headers: item.headers,
                    context: Some(self.context.clone_ref(py)),
                    stream: item.body_stream,
                },
            )?;
            let responder = Py::new(
                py,
                Responder::new(
                    item.reply,
                    self.queue.clone(),
                    self.runtime.clone(),
                    item.connection,
                    item.cancel.clone(),
                ),
            )?;
            let coro = match item.websocket {
                Some(shared) => {
                    let socket = Py::new(py, WebSocket::new(shared))?;
                    self.run_websocket
                        .bind(py)
                        .call1((handler, request, responder, socket, params))?
                }
                None => run_handler.call1((handler, request, responder, params, self.debug))?,
            };
            let task = create_task.call1((coro,))?;
            if let Some(cell) = item.cancel {
                cell.attach(py, task.unbind())?;
            }
        }

        self.queue.rewake_if_pending();
        Ok(())
    }
}

/// A Python worker: one OS thread, one asyncio loop, one request queue.
pub struct Worker {
    pub queue: Arc<WorkerQueue>,
    event_loop: Py<PyAny>,
    call_soon_threadsafe: Py<PyAny>,
    /// Joined at shutdown, so the loop's teardown finishes before `serve`
    /// returns rather than being cut off when the process exits.
    thread: Mutex<Option<JoinHandle<()>>>,
}

impl Worker {
    /// Nine arguments. Called once per worker at start, with a different value
    /// for each, so grouping them would be a struct that exists only to
    /// satisfy a count.
    #[allow(clippy::too_many_arguments)]
    pub fn spawn(
        py: Python<'_>,
        index: usize,
        routes: Arc<Vec<Py<PyAny>>>,
        gates: Arc<Vec<Option<Py<PyAny>>>>,
        router: Arc<Router>,
        limit: usize,
        debug: bool,
        tokio_handle: tokio::runtime::Handle,
        lifecycle: Py<PyAny>,
    ) -> PyResult<Self> {
        let (write_end, read_end) = wake::pair()?;

        let queue = Arc::new(WorkerQueue::new(write_end, limit));
        let worker_queue = queue.clone();
        let (tx, rx) = mpsc::channel::<PyResult<(Py<PyAny>, Py<PyAny>)>>();

        let handle = thread::Builder::new()
            .name(format!("oxbrook-py-{index}"))
            .spawn(move || {
                Python::attach(|py| {
                    let event_loop = match py
                        .import("oxbrook._runtime")
                        .and_then(|runtime| runtime.call_method0("make_worker_loop"))
                    {
                        Ok(event_loop) => event_loop,
                        Err(e) => {
                            let _ = tx.send(Err(e));
                            return;
                        }
                    };
                    let lifecycle = lifecycle.bind(py);

                    // The worker lifespan runs on this loop before anything is
                    // served from it, since what it builds may be bound to it.
                    let context = match lifecycle
                        .call_method0("start_worker")
                        .and_then(|coro| event_loop.call_method1("run_until_complete", (coro,)))
                    {
                        Ok(context) => context,
                        Err(e) => {
                            let _ = event_loop.call_method0("close");
                            let _ = tx.send(Err(e));
                            return;
                        }
                    };

                    let started = (|| -> PyResult<(i64, Py<PyAny>)> {
                        let runtime = py.import("oxbrook._runtime")?;
                        let drainer = Drainer {
                            queue: worker_queue,
                            routes,
                            gates,
                            router,
                            run_handler: runtime.getattr("run_handler")?.unbind(),
                            run_websocket: runtime.getattr("run_websocket")?.unbind(),
                            make_uuid: runtime.getattr("make_uuid")?.unbind(),
                            make_date: runtime.getattr("make_date")?.unbind(),
                            make_datetime: runtime.getattr("make_datetime")?.unbind(),
                            create_task: event_loop.getattr("create_task")?.unbind(),
                            debug,
                            reader: read_end,
                            runtime: tokio_handle,
                            context: context.clone().unbind(),
                        };
                        let watched = drainer.reader.watchable();
                        event_loop.call_method1("add_reader", (watched, Py::new(py, drainer)?))?;
                        let csts = event_loop.getattr("call_soon_threadsafe")?.unbind();
                        Ok((watched, csts))
                    })();

                    let teardown = |event_loop: &Bound<'_, PyAny>| {
                        let stopped = lifecycle
                            .call_method1("stop_worker", (context.clone(),))
                            .and_then(|coro| {
                                event_loop.call_method1("run_until_complete", (coro,))
                            });
                        if let Err(e) = stopped {
                            eprintln!("oxbrook: worker {index} lifespan teardown raised");
                            e.print(py);
                        }
                        let _ = event_loop.call_method0("close");
                    };

                    match started {
                        Ok((watched, csts)) => {
                            let _ = tx.send(Ok((event_loop.clone().unbind(), csts)));
                            if let Err(e) = event_loop.call_method0("run_forever") {
                                e.print(py);
                            }
                            // Stop taking requests before tearing down what
                            // they would use. Anything still queued is never
                            // started, and its client sees the connection end.
                            let _ = event_loop.call_method1("remove_reader", (watched,));
                            teardown(&event_loop);
                        }
                        Err(e) => {
                            teardown(&event_loop);
                            let _ = tx.send(Err(e));
                        }
                    }
                });
            })
            .expect("failed to spawn python worker thread");

        // Detach while waiting: blocking in native code while attached stalls
        // free-threaded CPython's stop-the-world and deadlocks startup. The
        // wait includes the worker lifespan, which may take a while.
        let reported = py.detach(move || rx.recv());
        let (event_loop, call_soon_threadsafe) = match reported {
            Ok(Ok(pair)) => pair,
            Ok(Err(e)) => {
                let _ = py.detach(move || handle.join());
                return Err(e);
            }
            Err(_) => panic!("worker thread died before reporting its event loop"),
        };

        Ok(Self {
            queue,
            event_loop,
            call_soon_threadsafe,
            thread: Mutex::new(Some(handle)),
        })
    }

    pub fn stop(&self, py: Python<'_>) {
        if let Ok(stop) = self.event_loop.getattr(py, "stop") {
            let _ = self.call_soon_threadsafe.call1(py, (stop,));
        }
    }

    /// Wait for the worker thread, and so its lifespan teardown, to finish.
    ///
    /// Bounded, because teardown is application code and may hang; a hang
    /// must not turn Ctrl-C into a process that never exits. Detached while
    /// waiting, since the worker thread needs the interpreter to finish.
    pub fn join(&self, py: Python<'_>, deadline: Instant) -> bool {
        let Some(handle) = self.thread.lock().ok().and_then(|mut slot| slot.take()) else {
            return true;
        };
        py.detach(|| {
            while !handle.is_finished() {
                if Instant::now() >= deadline {
                    return false;
                }
                thread::sleep(Duration::from_millis(10));
            }
            let _ = handle.join();
            true
        })
    }
}
