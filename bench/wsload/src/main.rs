//! WebSocket echo load: C connections, each sending a message and waiting for
//! its echo as fast as it can, for a fixed time.
//!
//!     wsload <url> <connections> <seconds> <size> <threads>
//!
//! Prints `measuring` when the window opens, then one JSON line: round trips
//! completed and the seconds they took.
//! bench/streams.py drives it with `--ws-client rust`, because a Python client
//! saturates its cores well before the server does.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::tungstenite::Message;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 6 {
        eprintln!("usage: wsload <url> <connections> <seconds> <size> <threads>");
        std::process::exit(2);
    }
    let url = args[1].clone();
    let conns: usize = args[2].parse().expect("connections");
    let seconds: f64 = args[3].parse().expect("seconds");
    let size: usize = args[4].parse().expect("size");
    let threads: usize = args[5].parse().expect("threads");

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(threads)
        .enable_all()
        .build()
        .expect("runtime");
    runtime.block_on(run(url, conns, Duration::from_secs_f64(seconds), size));
}

async fn run(url: String, conns: usize, length: Duration, size: usize) {
    let trips = Arc::new(AtomicU64::new(0));
    let measuring = Arc::new(AtomicBool::new(false));
    let stop = Arc::new(AtomicBool::new(false));

    // Connect everything first, so the handshakes are not part of the rate.
    let mut sockets = Vec::with_capacity(conns);
    for _ in 0..conns {
        let (ws, _) = tokio_tungstenite::connect_async(url.as_str())
            .await
            .expect("connect");
        sockets.push(ws);
    }

    let payload = vec![b'x'; size];
    let mut tasks = Vec::with_capacity(conns);
    for mut ws in sockets {
        let (trips, measuring, stop) = (trips.clone(), measuring.clone(), stop.clone());
        let payload = payload.clone();
        tasks.push(tokio::spawn(async move {
            while !stop.load(Ordering::Relaxed) {
                ws.send(Message::binary(payload.clone()))
                    .await
                    .expect("send");
                loop {
                    match ws.next().await {
                        Some(Ok(Message::Binary(_) | Message::Text(_))) => break,
                        Some(Ok(_)) => continue,
                        other => panic!("socket ended: {other:?}"),
                    }
                }
                if measuring.load(Ordering::Relaxed) {
                    trips.fetch_add(1, Ordering::Relaxed);
                }
            }
            let _ = ws.close(None).await;
        }));
    }

    // A short warm-up, then the measured window.
    tokio::time::sleep(Duration::from_millis(500)).await;
    measuring.store(true, Ordering::Relaxed);
    let started = Instant::now();
    // The driver reads the server's CPU time when it sees this line.
    println!("measuring");
    tokio::time::sleep(length).await;
    let counted = trips.load(Ordering::Relaxed);
    let elapsed = started.elapsed().as_secs_f64();
    stop.store(true, Ordering::Relaxed);
    for task in tasks {
        let _ = task.await;
    }
    println!("{{\"round_trips\": {counted}, \"seconds\": {elapsed}}}");
}
