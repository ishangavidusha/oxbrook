//! Liveness and readiness probes, answered before routing (D-058).
//!
//! Liveness is answered here, in Rust, and never reaches Python: it has to
//! answer when the worker loops cannot, since a stuck loop is what it exists
//! to report. It fails when a loop has had a wake waiting longer than
//! `stall_after` — requests queued and the loop not getting back to them —
//! and not merely because the server is busy: a probe that fails under load
//! gets a busy pod restarted, which moves the load onto the others.
//!
//! Readiness fails here while draining or stalled. Otherwise it is answered
//! here too, unless the app registered checks, in which case it falls through
//! to the Python route that runs them on every loop.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use bytes::Bytes;
use hyper::header::{HeaderValue, CACHE_CONTROL, CONTENT_TYPE};
use hyper::{Method, Response, StatusCode};

use crate::server::{full, Out};
use crate::worker::Worker;

/// (live path, ready path, readiness has Python checks, stall_after seconds,
/// drain_delay seconds) as built by `oxbrook._health.Health`.
pub type HealthTuple = (Option<String>, Option<String>, bool, f64, f64);

pub struct Health {
    live: Option<String>,
    ready: Option<String>,
    checks: bool,
    stall_after: Duration,
    pub drain_delay: Duration,
    /// Set when the server begins to stop. Readiness fails from then on, so a
    /// load balancer stops sending new requests while the old ones finish.
    draining: AtomicBool,
}

impl Health {
    pub fn build(spec: HealthTuple) -> Self {
        let (live, ready, checks, stall_after, drain_delay) = spec;
        Self {
            live,
            ready,
            checks,
            stall_after: Duration::from_secs_f64(stall_after.max(0.0)),
            drain_delay: Duration::from_secs_f64(drain_delay.max(0.0)),
            draining: AtomicBool::new(false),
        }
    }

    pub fn drain(&self) {
        self.draining.store(true, Ordering::SeqCst);
    }

    /// A probe's answer, or None for anything this does not answer.
    pub fn answer(&self, method: &Method, path: &str, workers: &[Worker]) -> Option<Response<Out>> {
        if method != Method::GET && method != Method::HEAD {
            return None;
        }
        let head = method == Method::HEAD;
        if self.live.as_deref() == Some(path) {
            return Some(match self.stalled(workers) {
                Some(loops) => stalled(&loops, head),
                None => reply(StatusCode::OK, r#"{"status":"ok"}"#.into(), head),
            });
        }
        if self.ready.as_deref() == Some(path) {
            if self.draining.load(Ordering::SeqCst) {
                return Some(reply(
                    StatusCode::SERVICE_UNAVAILABLE,
                    r#"{"status":"draining"}"#.into(),
                    head,
                ));
            }
            if let Some(loops) = self.stalled(workers) {
                return Some(stalled(&loops, head));
            }
            if !self.checks {
                return Some(reply(StatusCode::OK, r#"{"status":"ready"}"#.into(), head));
            }
        }
        None
    }

    /// The loops that have left a wake unanswered for longer than
    /// `stall_after`, by index; None when there are none.
    fn stalled(&self, workers: &[Worker]) -> Option<Vec<usize>> {
        let loops: Vec<usize> = workers
            .iter()
            .enumerate()
            .filter(|(_, w)| w.queue.waiting_for().is_some_and(|d| d > self.stall_after))
            .map(|(i, _)| i)
            .collect();
        (!loops.is_empty()).then_some(loops)
    }
}

fn stalled(loops: &[usize], head: bool) -> Response<Out> {
    let list: Vec<String> = loops.iter().map(usize::to_string).collect();
    reply(
        StatusCode::SERVICE_UNAVAILABLE,
        format!(r#"{{"status":"stalled","loops":[{}]}}"#, list.join(",")),
        head,
    )
}

fn reply(status: StatusCode, body: String, head: bool) -> Response<Out> {
    let body = if head {
        Bytes::new()
    } else {
        Bytes::from(body)
    };
    let mut response = Response::new(full(body));
    *response.status_mut() = status;
    let headers = response.headers_mut();
    headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    // A cached answer to "are you alive" is an answer about the past.
    headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}
