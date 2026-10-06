# Compression

```python
from oxbrook import App, Compression

app = App(compression=Compression())
```

A client that sends `Accept-Encoding` gets the reply compressed with brotli or
gzip. A JSON reply of a few kilobytes comes out eleven to fifteen times
smaller. Off unless set, because a proxy or CDN in front of the app often compresses already, and
compressing twice only spends the time twice.

## Options

| option | default | |
|---|---|---|
| `min_size` | `1024` | smallest body, in bytes, worth compressing |
| `encodings` | `("br", "gzip")` | codings to offer, most preferred first |
| `gzip_level` | `6` | 1 to 9 |
| `brotli_quality` | `4` | 0 to 11 |

The client's weights decide between the codings (`br;q=0.5, gzip` picks gzip),
and the order of `encodings` settles a tie. A client that names neither, or
sends no `Accept-Encoding`, gets the reply as it is.

Brotli is first because on JSON it comes out 12 to 15 percent smaller than
gzip, for about half as much time again. The defaults suit compressing every
reply as it goes out. Brotli from quality 5 up costs ten times as much for a
few percent, which is a trade made for files compressed once ahead of time,
not per request.

## What is compressed

A reply is compressed when all of these hold:

- it is whole: returned from a handler, not streamed
- it is at least `min_size` bytes
- its content type is text-like: `text/*`, JSON (including `+json` types such
  as `application/problem+json`), XML, JavaScript, YAML, NDJSON, WebAssembly
- the handler did not set `Content-Encoding` itself
- it is not a `204`, `206` or `304`

Everything else goes out unchanged, and so do these, whatever their size:

- **Streams.** [SSE](../streams/sse.md) and any other streamed reply. A
  compressor holds bytes back until it has a block worth emitting, and an event
  held back is an event that arrives late.
- **[Static files](static.md).** They are sent from disk, ranges and all; their
  bytes are not read into memory to be compressed.
- **Images, video, archives**, which are compressed already.

Errors a handler returns are compressed like any other reply. The short errors
the server answers itself, such as a `404` for an unknown path, are well under
`min_size`.

## Headers

A compressed reply carries `Content-Encoding` and a `Content-Length` for the
compressed bytes. Any reply that could have gone either way carries
`Vary: Accept-Encoding`, including one sent plain because the client did not
ask, so a shared cache never hands a gzip body to a client that cannot read
it. A `Vary` the handler set is kept, with `Accept-Encoding` added to it.

A strong `ETag` the handler set is made weak (`"v1"` becomes `W/"v1"`) on a
compressed reply: a strong tag promises exact bytes, and these are different
bytes. Compared weakly, as `If-None-Match` is, it still matches the tag the
handler issued.

A `HEAD` request is answered with the `Content-Encoding` a `GET` would have,
and without a `Content-Length`, since learning the compressed length would mean
compressing the body.

## Opting one reply out

```python
return Response(page, content_type="text/html", headers={"cache-control": "no-transform"})
```

`Cache-Control: no-transform` leaves that reply as it is.

Use it on a page that puts a secret, such as a CSRF token, in the same body as
text an attacker can choose, such as a search term echoed back. Compression
makes the body smaller when the attacker's guess matches part of the secret,
and an attacker who can make a signed-in browser send many requests can read
the secret out of the sizes. This is the BREACH attack. A JSON API whose
callers authenticate with a header is not exposed this way, because another
site cannot make a browser send that header.

## What it costs

Measured on an Apple M4 with four worker loops, a 17 KB JSON reply: about
10 to 18 microseconds of added time per request with gzip and 15 to 23 with
brotli, which cost 6 to 13 percent of throughput, for a reply of 1.5 KB or
1.3 KB on the wire. A reply under `min_size` costs nothing measurable.

## Where the work happens

Compression runs in the server, after the handler has answered, on the threads
that handle connections. The worker loops that run handlers are not involved.
A body over 64 KiB is compressed on a separate thread pool instead, so that the
milliseconds a large body takes do not hold up other connections.
