//! The wake pair: one byte from a tokio thread makes a worker's loop run its
//! drain callback (D-047).
//!
//! A tokio thread must never attach to the interpreter to hand over a request
//! (invariant 1), so the only thing it does to a worker is write a byte to a
//! socket the worker's asyncio loop already watches. Both ends of that socket
//! are created here, because how they are created is the one part of dispatch
//! that differs by platform:
//!
//! * Unix has `socketpair`, which is what `UnixStream::pair` calls.
//! * Windows has no socketpair at all, and its loops watch sockets rather than
//!   file descriptors, so the pair is a loopback TCP connection to a listener
//!   that exists for exactly one connection.
//!
//! The rest of the dispatch path sees one type either way, and the handle it
//! hands to `loop.add_reader` is an `i64` on both.

use std::io::{self, Read, Write};

#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::net::UnixStream;

#[cfg(windows)]
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
#[cfg(windows)]
use std::os::windows::io::AsRawSocket;

#[cfg(unix)]
type Half = UnixStream;
#[cfg(windows)]
type Half = TcpStream;

/// The end a tokio thread writes to. Held by the `WorkerQueue`.
pub struct WakeWriter(Half);

/// The end the worker's loop watches. Held by the `Drainer`.
pub struct WakeReader(Half);

/// Create a connected pair, both ends non-blocking.
pub fn pair() -> io::Result<(WakeWriter, WakeReader)> {
    let (write_end, read_end) = halves()?;
    write_end.set_nonblocking(true)?;
    read_end.set_nonblocking(true)?;
    Ok((WakeWriter(write_end), WakeReader(read_end)))
}

#[cfg(unix)]
fn halves() -> io::Result<(Half, Half)> {
    UnixStream::pair()
}

#[cfg(windows)]
fn halves() -> io::Result<(Half, Half)> {
    // A listener on the loopback is reachable by anything else on the machine,
    // so an accepted connection counts only when both ends agree on both
    // addresses: a stranger that wins the race is dropped and the real
    // connection is taken on the next accept. The listener is bound, used and
    // closed inside this function, which keeps that window to one connect.
    let listener = TcpListener::bind(SocketAddr::from((Ipv4Addr::LOCALHOST, 0)))?;
    let write_end = TcpStream::connect(listener.local_addr()?)?;
    let ours = write_end.local_addr()?;
    for _ in 0..16 {
        let (read_end, peer) = listener.accept()?;
        if peer == ours && read_end.local_addr()? == write_end.peer_addr()? {
            // Nagle would hold a single byte back waiting for company, which
            // is the whole message and turns every idle wakeup into a delay.
            write_end.set_nodelay(true)?;
            read_end.set_nodelay(true)?;
            return Ok((write_end, read_end));
        }
    }
    Err(io::Error::new(
        io::ErrorKind::ConnectionRefused,
        "the wake socket was taken by another process",
    ))
}

impl WakeWriter {
    /// One byte, from a tokio thread. The caller guarantees at most one
    /// unread byte is in flight, so the socket buffer can never fill.
    pub fn wake(&self) {
        let _ = (&self.0).write(&[1u8]);
    }
}

impl WakeReader {
    /// Empty the socket. Called by the drain callback on the worker's thread,
    /// before it looks at the queues.
    pub fn drain(&self) {
        let mut buf = [0u8; 64];
        while let Ok(n) = (&self.0).read(&mut buf) {
            if n < buf.len() {
                break;
            }
        }
    }

    /// What `loop.add_reader` and `loop.remove_reader` are given: a file
    /// descriptor on Unix, a socket handle on Windows. Both are integers to
    /// asyncio, which passes them to its selector.
    pub fn watchable(&self) -> i64 {
        #[cfg(unix)]
        {
            self.0.as_raw_fd() as i64
        }
        #[cfg(windows)]
        {
            self.0.as_raw_socket() as i64
        }
    }
}
