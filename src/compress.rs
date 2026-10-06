//! Response compression, negotiated from `Accept-Encoding` (D-057).
//!
//! Applied to a handler's whole reply, on the tokio side, after the handler
//! has answered: Python never sees it and never pays for it. Streaming replies
//! are left alone — an SSE event held back to fill a compression block is an
//! event that arrives late — and so are static files, which stream from disk
//! and answer ranges against the bytes on disk.
//!
//! Small bodies are compressed inline, where the work costs about what a
//! request does. Large ones go to the blocking pool, because a megabyte at
//! these settings takes milliseconds and every connection on the same tokio
//! thread would wait for it.

use std::io::Write;

use hyper::header::{HeaderMap, HeaderValue, ACCEPT_ENCODING, VARY};

/// (min_size, gzip_level, brotli_quality, encodings in order of preference)
/// as built by `oxbrook._compression.Compression`.
pub type CompressionTuple = (usize, u32, u32, Vec<String>);

/// Bodies at least this large are compressed on the blocking pool. Below it,
/// gzip or brotli at the default settings measured 12–50 µs (D-057): less
/// than a handoff to another thread and back is worth avoiding.
pub const INLINE_LIMIT: usize = 64 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Encoding {
    Brotli,
    Gzip,
}

impl Encoding {
    fn parse(token: &str) -> Option<Self> {
        match token {
            "br" => Some(Self::Brotli),
            // RFC 9110 §8.4.1.3: x-gzip is the same coding, kept for old clients.
            "gzip" | "x-gzip" => Some(Self::Gzip),
            _ => None,
        }
    }

    pub fn token(self) -> &'static str {
        match self {
            Self::Brotli => "br",
            Self::Gzip => "gzip",
        }
    }
}

/// What to do with one reply.
#[derive(Debug, PartialEq, Eq)]
pub enum Plan {
    /// Not a candidate at all: its headers stay as the handler set them.
    Skip,
    /// A candidate the client will not take compressed. It still varies by
    /// `Accept-Encoding`, so a cache must not hand it to a client that would.
    Identity,
    Encode(Encoding),
}

pub struct Compression {
    min_size: usize,
    gzip_level: u32,
    brotli_quality: u32,
    prefer: Vec<Encoding>,
}

impl Compression {
    pub fn build(spec: CompressionTuple) -> Result<Self, String> {
        let (min_size, gzip_level, brotli_quality, encodings) = spec;
        let mut prefer = Vec::new();
        for name in &encodings {
            let encoding =
                Encoding::parse(name).ok_or_else(|| format!("unknown content coding {name:?}"))?;
            if !prefer.contains(&encoding) {
                prefer.push(encoding);
            }
        }
        if prefer.is_empty() {
            return Err("no content codings to offer".into());
        }
        if !(1..=9).contains(&gzip_level) {
            return Err(format!("gzip level {gzip_level} is outside 1..=9"));
        }
        if brotli_quality > 11 {
            return Err(format!("brotli quality {brotli_quality} is outside 0..=11"));
        }
        Ok(Self {
            min_size,
            gzip_level,
            brotli_quality,
            prefer,
        })
    }

    /// Decide for one reply. `headers` are the handler's own.
    pub fn plan(
        &self,
        accept: Option<&HeaderValue>,
        status: u16,
        content_type: &str,
        headers: &[(String, String)],
        len: usize,
    ) -> Plan {
        // No body to compress, or one that is a byte range of another.
        if status < 200 || status == 204 || status == 206 || status == 304 {
            return Plan::Skip;
        }
        if len < self.min_size || !compressible(content_type) {
            return Plan::Skip;
        }
        for (name, value) in headers {
            // Already encoded by the handler, or explicitly not to be altered:
            // `no-transform` is how a single response opts out.
            if name.eq_ignore_ascii_case("content-encoding")
                || (name.eq_ignore_ascii_case("cache-control")
                    && value
                        .split(',')
                        .any(|d| d.trim().eq_ignore_ascii_case("no-transform")))
            {
                return Plan::Skip;
            }
        }
        match accept
            .and_then(|v| v.to_str().ok())
            .and_then(|v| negotiate(v, &self.prefer))
        {
            Some(encoding) => Plan::Encode(encoding),
            None => Plan::Identity,
        }
    }

    pub fn encode(&self, encoding: Encoding, body: &[u8]) -> Vec<u8> {
        let out = Vec::with_capacity(body.len() / 4 + 64);
        match encoding {
            Encoding::Gzip => {
                let mut encoder =
                    flate2::write::GzEncoder::new(out, flate2::Compression::new(self.gzip_level));
                // Writing to a Vec cannot fail.
                encoder.write_all(body).expect("write to memory");
                encoder.finish().expect("write to memory")
            }
            Encoding::Brotli => {
                let mut out = out;
                {
                    let mut writer = brotli::CompressorWriter::new(
                        &mut out,
                        4096,
                        self.brotli_quality,
                        window(body.len()),
                    );
                    writer.write_all(body).expect("write to memory");
                }
                out
            }
        }
    }
}

/// The brotli window, sized to the body. The usual 4 MiB window on a 1 KiB
/// body cost most of the time spent compressing it (D-057).
fn window(len: usize) -> u32 {
    (usize::BITS - len.max(1).leading_zeros()).clamp(10, 22)
}

/// The coding to use, by the client's weights and then the server's order.
/// None means identity: no header, a `q=0` on everything offered, or only
/// codings this server does not produce.
pub fn negotiate(accept: &str, prefer: &[Encoding]) -> Option<Encoding> {
    let mut star: Option<f32> = None;
    let mut weights: Vec<(Encoding, f32)> = Vec::new();
    for item in accept.split(',') {
        let mut parts = item.split(';');
        let token = parts.next().unwrap_or("").trim().to_ascii_lowercase();
        if token.is_empty() {
            continue;
        }
        let mut weight = Some(1.0);
        for param in parts {
            if let Some((key, value)) = param.split_once('=') {
                if key.trim().eq_ignore_ascii_case("q") {
                    weight = qvalue(value.trim());
                }
            }
        }
        // A malformed weight drops the entry, not the whole header.
        let Some(weight) = weight else { continue };
        if token == "*" {
            star = Some(weight);
        } else if let Some(encoding) = Encoding::parse(&token) {
            weights.push((encoding, weight));
        }
    }
    let mut best: Option<(Encoding, f32)> = None;
    for &encoding in prefer {
        // A coding the client did not name is acceptable only through `*`.
        let weight = weights
            .iter()
            .filter(|(e, _)| *e == encoding)
            .map(|(_, w)| *w)
            .reduce(f32::max)
            .or(star)
            .unwrap_or(0.0);
        // Strictly greater: on a tie the server's earlier preference stands.
        if weight > 0.0 && best.is_none_or(|(_, w)| weight > w) {
            best = Some((encoding, weight));
        }
    }
    best.map(|(encoding, _)| encoding)
}

/// RFC 9110 §12.4.2: 0 to 1, at most three decimals.
fn qvalue(text: &str) -> Option<f32> {
    let valid = match text.split_once('.') {
        Some((whole, fraction)) => {
            (whole == "0" || whole == "1")
                && fraction.len() <= 3
                && fraction.bytes().all(|b| b.is_ascii_digit())
                && (whole == "0" || fraction.bytes().all(|b| b == b'0'))
        }
        None => text == "0" || text == "1",
    };
    valid.then(|| text.parse().ok()).flatten()
}

/// Text formats compress four to fifteen times; images, archives and video
/// are compressed already and only cost time.
pub fn compressible(content_type: &str) -> bool {
    let essence = content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    let Some((kind, subtype)) = essence.split_once('/') else {
        return false;
    };
    if kind == "text" {
        // An event stream is a stream, which is never compressed here anyway.
        return subtype != "event-stream";
    }
    subtype.ends_with("+json")
        || subtype.ends_with("+xml")
        || subtype.ends_with("+yaml")
        || (kind == "application"
            && matches!(
                subtype,
                "json"
                    | "xml"
                    | "javascript"
                    | "x-javascript"
                    | "ecmascript"
                    | "wasm"
                    | "x-ndjson"
                    | "ndjson"
                    | "jsonl"
                    | "yaml"
                    | "x-yaml"
                    | "toml"
                    | "graphql"
                    | "x-www-form-urlencoded"
                    | "rtf"
            ))
}

/// Say that the reply depends on `Accept-Encoding`, unless it already does.
pub fn vary(headers: &mut HeaderMap) {
    let mentioned = headers.get_all(VARY).iter().any(|value| {
        value.to_str().is_ok_and(|v| {
            v.split(',').any(|item| {
                let item = item.trim();
                item == "*" || item.eq_ignore_ascii_case(ACCEPT_ENCODING.as_str())
            })
        })
    });
    if !mentioned {
        headers.append(VARY, HeaderValue::from_static("Accept-Encoding"));
    }
}

/// A strong ETag names exact bytes, and the compressed body is other bytes.
/// Weakened rather than rewritten, so a conditional request carrying the tag
/// a handler issued still compares equal to it under weak comparison.
pub fn weaken_etag(headers: &mut HeaderMap) {
    if let Some(tag) = headers.get(hyper::header::ETAG) {
        if let Ok(text) = tag.to_str() {
            if !text.starts_with("W/") {
                if let Ok(weak) = HeaderValue::from_str(&format!("W/{text}")) {
                    headers.insert(hyper::header::ETAG, weak);
                }
            }
        }
    }
}
