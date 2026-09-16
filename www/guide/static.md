# Static files

```python
app.static("/assets", "public")
```

Every file under `public/` is served under `/assets/`: `public/logo.png` at
`/assets/logo.png`. Files are served by the Rust core. A file request never
wakes a Python worker and never counts against `max_concurrency`, so static
assets keep loading when every worker is busy with slow handlers.

## A single-page app

```python
app = App()

@app.get("/api/users")
async def users(_: Request): ...

app.static("/", "frontend/dist", fallback="index.html")
```

Routes take precedence where they are more specific, so `/api/users` reaches its
handler and everything else is looked up in `frontend/dist`.

`fallback` is what makes client-side routing work. A browser loading
`/users/42/settings` directly gets `index.html`, and the app's router takes over.
The fallback is served only for a page load: a path with no file extension, on a
request whose `Accept` header includes `text/html`, which browsers send when
loading a page. So:

| request | answer |
|---|---|
| a browser opening `/users/42` | `index.html`, 200 |
| `fetch("/api/usrs")`, a typo | 404, not HTML the client then fails to parse |
| a missing `/app.js` | 404, not HTML served as JavaScript |

## Options

| option | default | |
|---|---|---|
| `index` | `"index.html"` | served for a directory; `None` makes directories 404 |
| `fallback` | `None` | served for page loads that match no file |
| `dotfiles` | `False` | serve names beginning with `.` |
| `cache_control` | `None` | the `Cache-Control` header on every file served |

A relative `directory` resolves from the working directory, and it must exist
when `static` is called. When the app may be started from elsewhere, anchor it
to the module:

```python
from pathlib import Path

app.static("/assets", Path(__file__).parent / "public")
```

A path naming a directory without its trailing slash is redirected to it, with
`308`, so relative links inside that directory's index resolve.

## What is refused

A static directory is readable by anyone who can reach the server, so the rules
are strict, and every refusal is a `404` whatever the reason, so a refusal says
nothing about what exists:

- **Paths that leave the directory.** `..` is refused however it is written —
  plainly, percent-encoded as `%2e%2e`, hidden behind an encoded slash — along
  with backslashes and NUL.
- **Symlinks that point outside it.** A symlink to a file elsewhere inside the
  directory is followed; one that resolves outside it is not.
- **Dotfiles, unless allowed.** `.env`, `.git/` and editor swap files are the
  files most likely to end up in a static directory by accident. For
  `.well-known/`, pass `dotfiles=True`, and keep secrets out of the directory.
- **Directory listings.** A directory is its index or nothing.

## What is answered

- **Content type** from the file extension, with `charset=utf-8` on text,
  JavaScript and JSON.
- **Conditional requests.** Every file carries an `ETag` and `Last-Modified`;
  `If-None-Match` and `If-Modified-Since` get `304 Not Modified`.
- **Ranges.** `Range: bytes=...` gets `206 Partial Content`, which video players
  and resumable downloads rely on; a range past the end gets `416`. `If-Range` is
  honoured. A request for several ranges gets the whole file.
- **HEAD**, with the length and no body. Any other method gets `405`.
- **CORS** headers, when the app has a [CORS policy](cors.md).

Files up to 256 KiB are read in one go; larger files are streamed in 64 KiB
chunks, so a large download never sits in memory and a slow client holds one
chunk, not the file.

## Caching

```python
app.static("/assets", "dist/assets", cache_control="public, max-age=31536000, immutable")
app.static("/", "dist", fallback="index.html", cache_control="no-cache")
```

Build tools put a content hash in asset file names, so those can be cached
forever; `index.html` must not be, or visitors keep loading an old build. Mounting
the hashed assets separately, with their own policy, gives each the right one.
Routes and more specific mounts take precedence over a mount at `/`.

## Middleware does not run

Static files are answered before middleware, so neither authentication
middleware nor exception handlers apply to them. A file that should only reach
signed-in users belongs behind a route:

```python
@app.get("/reports/{name}")
async def report(request: Request, name: str, user = Depends(current_user)):
    ...
```

## Speed

On a ten-core Apple Silicon machine, a 2 KiB file serves at about 95,000
requests per second and a 1 MiB file at about 6 GB/s. A Python handler returning
a small body it already holds in memory is faster, roughly 180,000 requests per
second, because it never touches the disk. What serving from Rust buys is not
raw speed on tiny files: it is that files cost no worker and no concurrency slot,
large files stream without being held in memory, and the path safety, ranges and
caching headers above come without writing them.

For heavy static traffic, a CDN or a proxy in front with its own file cache is
still the better tool.
