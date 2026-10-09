//! Server metrics in the Prometheus text format (D-059).
//!
//! Collected where they happen, in Rust: how long a request waited for a
//! worker loop, how many are queued and in flight on each loop, connections
//! held, requests shed or timed out. None of these is visible from Python, and
//! they are the numbers that explain a slow service.
//!
//! Recording is lock-free: a route's counters are a fixed array of atomics,
//! indexed by status code, allocated once when the server starts. A request
//! costs a clock read and three relaxed atomic adds. Rendering walks them on
//! demand, on a tokio thread, without the interpreter.

use std::fmt::Write as _;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use bytes::Bytes;
use hyper::header::{HeaderValue, CACHE_CONTROL, CONTENT_TYPE};
use hyper::{Method, Response, StatusCode};
use tokio::sync::Semaphore;

use crate::server::{full, Out};
use crate::worker::Worker;

/// Upper bounds, in seconds, of the duration buckets: Prometheus's own
/// defaults with 1 ms and 2.5 ms added, since most requests here finish well
/// under the default's first bucket of 5 ms.
const BOUNDS: [f64; 13] = [
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];

/// Codes 100 to 599, the range hyper will send.
const STATUSES: usize = 500;

/// Path of the scrape endpoint, as built by `oxbrook._metrics.Metrics`.
pub type MetricsTuple = (String,);

/// A response the server produced itself for one of these reasons, marked in
/// its extensions so it is counted apart from a handler that chose the same
/// status.
#[derive(Clone, Copy)]
pub enum Refusal {
    /// Every worker was at its limit: `503`.
    Shed,
    /// The handler did not answer within `request_timeout`: `504`.
    TimedOut,
}

/// A response the server gave on its own behalf before any routing, such as a
/// CORS preflight: not a request to the app, so not counted as one.
#[derive(Clone, Copy)]
pub struct Uncounted;

pub struct Histogram {
    /// Per bucket, not cumulative: one add per observation. Rendering sums.
    buckets: [AtomicU64; BOUNDS.len() + 1],
    sum_micros: AtomicU64,
}

impl Histogram {
    fn new() -> Self {
        Self {
            buckets: std::array::from_fn(|_| AtomicU64::new(0)),
            sum_micros: AtomicU64::new(0),
        }
    }

    pub fn observe(&self, elapsed: Duration) {
        let seconds = elapsed.as_secs_f64();
        let slot = BOUNDS
            .iter()
            .position(|&b| seconds <= b)
            .unwrap_or(BOUNDS.len());
        self.buckets[slot].fetch_add(1, Ordering::Relaxed);
        self.sum_micros
            .fetch_add(elapsed.as_micros() as u64, Ordering::Relaxed);
    }

    fn render(&self, out: &mut String, name: &str, labels: &str) {
        let mut running = 0;
        let sep = if labels.is_empty() { "" } else { "," };
        for (i, bound) in BOUNDS.iter().enumerate() {
            running += self.buckets[i].load(Ordering::Relaxed);
            let _ = writeln!(
                out,
                "{name}_bucket{{{labels}{sep}le=\"{bound}\"}} {running}"
            );
        }
        running += self.buckets[BOUNDS.len()].load(Ordering::Relaxed);
        let _ = writeln!(out, "{name}_bucket{{{labels}{sep}le=\"+Inf\"}} {running}");
        let sum = self.sum_micros.load(Ordering::Relaxed) as f64 / 1e6;
        let braces = if labels.is_empty() {
            String::new()
        } else {
            format!("{{{labels}}}")
        };
        let _ = writeln!(out, "{name}_sum{braces} {sum}");
        let _ = writeln!(out, "{name}_count{braces} {running}");
    }
}

struct Route {
    /// `method="GET",route="/users/{id}"`, escaped once at startup.
    labels: String,
    statuses: Box<[AtomicU64]>,
    duration: Histogram,
}

impl Route {
    fn new(method: &str, path: &str) -> Self {
        Self {
            labels: format!("method=\"{}\",route=\"{}\"", escape(method), escape(path)),
            statuses: (0..STATUSES).map(|_| AtomicU64::new(0)).collect(),
            duration: Histogram::new(),
        }
    }
}

pub struct Metrics {
    path: String,
    /// By route index, as the router numbers them.
    routes: Vec<Route>,
    /// Requests that matched no route: `404`, `405`, and a bad path parameter
    /// that failed before a route could be named.
    unmatched: Route,
    shed: AtomicU64,
    timeouts: AtomicU64,
    /// Set by the accept loop, which owns the semaphore.
    connections: OnceLock<(Arc<Semaphore>, usize)>,
}

impl Metrics {
    pub fn build(spec: MetricsTuple, routes: &[(String, String)]) -> Self {
        Self {
            path: spec.0,
            routes: routes.iter().map(|(m, p)| Route::new(m, p)).collect(),
            unmatched: Route::new("", ""),
            shed: AtomicU64::new(0),
            timeouts: AtomicU64::new(0),
            connections: OnceLock::new(),
        }
    }

    pub fn watch_connections(&self, semaphore: Arc<Semaphore>, limit: usize) {
        let _ = self.connections.set((semaphore, limit));
    }

    pub fn is_scrape(&self, method: &Method, path: &str) -> bool {
        (method == Method::GET || method == Method::HEAD) && path == self.path
    }

    /// One request, once its response exists.
    pub fn record(
        &self,
        route: Option<usize>,
        status: StatusCode,
        elapsed: Duration,
        refusal: Option<Refusal>,
    ) {
        let entry = route
            .and_then(|i| self.routes.get(i))
            .unwrap_or(&self.unmatched);
        let code = status.as_u16() as usize;
        if (100..100 + STATUSES).contains(&code) {
            entry.statuses[code - 100].fetch_add(1, Ordering::Relaxed);
        }
        entry.duration.observe(elapsed);
        match refusal {
            Some(Refusal::Shed) => self.shed.fetch_add(1, Ordering::Relaxed),
            Some(Refusal::TimedOut) => self.timeouts.fetch_add(1, Ordering::Relaxed),
            None => 0,
        };
    }

    pub fn scrape(&self, workers: &[Worker], draining: Option<bool>, head: bool) -> Response<Out> {
        let body = if head {
            Bytes::new()
        } else {
            Bytes::from(self.render(workers, draining))
        };
        let mut response = Response::new(full(body));
        let headers = response.headers_mut();
        headers.insert(
            CONTENT_TYPE,
            HeaderValue::from_static("text/plain; version=0.0.4; charset=utf-8"),
        );
        headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
        response
    }

    fn render(&self, workers: &[Worker], draining: Option<bool>) -> String {
        let mut out = String::with_capacity(4096 + self.routes.len() * 1024);

        header(
            &mut out,
            "oxbrook_requests_total",
            "counter",
            "Requests answered, by route template and status.",
        );
        for route in self.routes.iter().chain(std::iter::once(&self.unmatched)) {
            for (i, count) in route.statuses.iter().enumerate() {
                let n = count.load(Ordering::Relaxed);
                if n > 0 {
                    let _ = writeln!(
                        out,
                        "oxbrook_requests_total{{{},status=\"{}\"}} {n}",
                        route.labels,
                        i + 100
                    );
                }
            }
        }

        header(
            &mut out,
            "oxbrook_request_duration_seconds",
            "histogram",
            "Time from the request's arrival to its response headers, by route template.",
        );
        for route in self.routes.iter().chain(std::iter::once(&self.unmatched)) {
            let seen: u64 = route
                .duration
                .buckets
                .iter()
                .map(|b| b.load(Ordering::Relaxed))
                .sum();
            if seen > 0 {
                route
                    .duration
                    .render(&mut out, "oxbrook_request_duration_seconds", &route.labels);
            }
        }

        header(
            &mut out,
            "oxbrook_queue_wait_seconds",
            "histogram",
            "Time a request waited in a worker loop's queue before the loop took it.",
        );
        for (i, worker) in workers.iter().enumerate() {
            if let Some(wait) = worker.queue.wait() {
                wait.render(
                    &mut out,
                    "oxbrook_queue_wait_seconds",
                    &format!("loop=\"{i}\""),
                );
            }
        }

        header(
            &mut out,
            "oxbrook_loop_requests",
            "gauge",
            "Requests queued or in flight on each worker loop.",
        );
        for (i, worker) in workers.iter().enumerate() {
            let _ = writeln!(
                out,
                "oxbrook_loop_requests{{loop=\"{i}\"}} {}",
                worker.queue.load()
            );
        }
        header(
            &mut out,
            "oxbrook_loop_queued",
            "gauge",
            "Requests waiting in each worker loop's queue, not yet taken.",
        );
        for (i, worker) in workers.iter().enumerate() {
            let _ = writeln!(
                out,
                "oxbrook_loop_queued{{loop=\"{i}\"}} {}",
                worker.queue.queued()
            );
        }
        header(
            &mut out,
            "oxbrook_loop_wake_pending_seconds",
            "gauge",
            "How long each loop has left a wakeup unanswered; 0 when it has none.",
        );
        for (i, worker) in workers.iter().enumerate() {
            let waited = worker.queue.waiting_for().map_or(0.0, |d| d.as_secs_f64());
            let _ = writeln!(
                out,
                "oxbrook_loop_wake_pending_seconds{{loop=\"{i}\"}} {waited}"
            );
        }

        if let Some((semaphore, limit)) = self.connections.get() {
            header(
                &mut out,
                "oxbrook_connections",
                "gauge",
                "Connections held open.",
            );
            let open = limit.saturating_sub(semaphore.available_permits());
            let _ = writeln!(out, "oxbrook_connections {open}");
            header(
                &mut out,
                "oxbrook_connections_limit",
                "gauge",
                "max_connections.",
            );
            let _ = writeln!(out, "oxbrook_connections_limit {limit}");
        }

        header(
            &mut out,
            "oxbrook_requests_shed_total",
            "counter",
            "Requests answered 503 because every worker loop was at max_concurrency.",
        );
        let _ = writeln!(
            out,
            "oxbrook_requests_shed_total {}",
            self.shed.load(Ordering::Relaxed)
        );
        header(
            &mut out,
            "oxbrook_request_timeouts_total",
            "counter",
            "Requests answered 504 because the handler passed request_timeout.",
        );
        let _ = writeln!(
            out,
            "oxbrook_request_timeouts_total {}",
            self.timeouts.load(Ordering::Relaxed)
        );

        if let Some(draining) = draining {
            header(
                &mut out,
                "oxbrook_draining",
                "gauge",
                "1 while the server is draining.",
            );
            let _ = writeln!(out, "oxbrook_draining {}", u8::from(draining));
        }
        out
    }
}

fn header(out: &mut String, name: &str, kind: &str, help: &str) {
    let _ = writeln!(out, "# HELP {name} {help}");
    let _ = writeln!(out, "# TYPE {name} {kind}");
}

/// Label values escape backslash, double quote and newline.
fn escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
}

/// The queue's half: a histogram created only when metrics are on, so a
/// server without them never reads the clock for this.
pub fn wait_histogram() -> Histogram {
    Histogram::new()
}
