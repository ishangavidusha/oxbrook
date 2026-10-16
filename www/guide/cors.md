# CORS

A page on another origin can only call the app from a browser if the app says
it may.

```python
from oxbrook import App, CORS

app = App(cors=CORS(allow_origins=["https://app.example.com"]))
```

## Options

| option | default | |
|---|---|---|
| `allow_origins` | required | origins, patterns like `https://*.example.dev`, or `["*"]` for any |
| `allow_methods` | `["*"]` | methods a preflight may ask for |
| `allow_headers` | `["*"]` | request headers a preflight may ask for |
| `allow_credentials` | `False` | let the browser send cookies and `Authorization` |
| `expose_headers` | `[]` | response headers a page's script may read |
| `max_age` | `600` | seconds a browser may cache a preflight answer |

An origin is a scheme, host and optional port with no path and no trailing
slash — `https://app.example.com`, `http://localhost:5173` — exactly as the
browser sends it in the `Origin` header.

### Preview deployments

An origin may hold one `*`, in the first label of its host, for the per-branch
URLs a preview platform gives each deployment:

```python
CORS(allow_origins=["https://app.example.com", "https://*.preview.example.com"])
CORS(allow_origins=["https://myapp-*-myteam.vercel.app"])
```

The `*` stands for one label's worth of letters, digits and hyphens: one or
more, never a dot. Scheme, port and every other label must match exactly. So
`https://*.preview.example.com` allows `https://pr-12.preview.example.com`, but
not `https://preview.example.com`, `https://a.b.preview.example.com`,
`https://pr-12.preview.example.com.attacker.com` or
`https://attackerpreview.example.com`. At least two labels must follow the
`*`, so `https://*.dev` is refused.

On a domain shared by many customers, such as a hosting platform's, a pattern
admits anyone who can get a matching name there. `https://*.vercel.app` would
let every Vercel site make requests, with credentials if those are allowed.
Keep the part of the name that only you control, as the second example does.
[WebSocket origins](../streams/websockets.md) take the same patterns.

`allow_origins` has no default because it is the decision that matters. Methods
and headers default to any: once an origin is trusted, permitting a method or a
header does not widen what that origin can do. The origin list and
`allow_credentials` are the boundary.

## Credentials

```python
CORS(allow_origins=["https://app.example.com"], allow_credentials=True)
```

With credentials, the browser sends cookies and `Authorization` on cross-origin
requests, and the app's responses are readable by that origin's scripts.

`allow_credentials=True` with `allow_origins=["*"]` raises. It would let every
website make requests as a signed-in user and read the answers. Browsers refuse
a literal `*` with credentials, and a server that echoes back whatever origin
arrives to get around that is exactly the vulnerability. List the origins.

## What the server does

**On every response,** to a request whose `Origin` is allowed:
`Access-Control-Allow-Origin` — the origin itself, or `*` when any origin is
allowed without credentials — plus `Access-Control-Allow-Credentials` and
`Access-Control-Expose-Headers` when configured. `Vary: Origin` is added
whenever the answer depends on the origin, including on responses to requests
with no `Origin` at all, so a shared cache never hands one origin's answer to
another.

**On a preflight** — `OPTIONS` carrying `Origin` and
`Access-Control-Request-Method` — the server answers `204` with the allowed
method, headers and `Access-Control-Max-Age`, or `400` with the reason when the
origin, method or a header is not allowed. A preflight is answered for any path,
whether or not a route exists there, so a foreign site cannot use preflights to
discover which paths exist. A plain `OPTIONS` without those headers is routed
like any other request.

A request from a foreign origin is still served; its response simply carries no
CORS headers, and the browser withholds it from the page. CORS is enforced by
the browser. It is not access control, and does nothing against a client that is
not a browser.

A header a handler sets itself — `Access-Control-Allow-Origin` on its own
`Response` — is left alone.

## Why it is not middleware

The server answers `404`, `405`, `413`, `422`, `503` and `504` itself, without
waking a worker. Middleware never sees those responses. A browser that receives
one without CORS headers reports a CORS error, not the real status, and whoever
is debugging it starts in the wrong place. CORS is applied in Rust so that every
response carries the headers, whoever produced it, and a preflight never costs a
worker.

With no `cors` configured, none of this runs and nothing is added.

## WebSockets

Browsers do not apply CORS to WebSocket connections, so the server checks a
socket's `Origin` separately, before the handshake. By default the origins in
`allow_origins` may open sockets, along with the app's own origin; `*` does not
extend to sockets. Set `App(websocket_origins=[...])` to choose a different
list. See [which sites may connect](../streams/websockets.md#which-sites-may-connect).
