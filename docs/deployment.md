# Deployment and security

Running in production: workers, proxies, compression, and what keeps a page safe.

## Deployment

**Running.** `uv run pro` (or `reaction run --host 0.0.0.0`) serves a project
with uvicorn; any ASGI server can serve `create_app(App)`. In a container, set
`PORT` and the `PYREACTION_*` variables instead of editing code.

**Compression and caching.** Pages and static files are gzipped (`compress`; turn
it off when a proxy already compresses), streamed pages included. The client
script is served at a URL with a hash of its contents (`client.js?v=...`), so
browsers keep it for a year and fetch it again only after an upgrade.

**Sessions and workers.** Each open page is a live session on the server. The
render that made a page's HTML waits up to `handoff_timeout` seconds for the
page's live connection, which takes it over instead of rendering again (only for
the same browser cookies and URL; at most `max_handoffs` wait at once). If the
connection drops, the page dims and reconnects with growing, randomised delays.
The server keeps the dropped session for `resume_timeout` seconds, so a reconnect
within that time continues with its state; after that, or on another worker
process, or after a deploy, the page renders afresh at the same URL. Any number
of workers can serve the app. To resume sessions or take over renders across
workers, route a page's connections to the worker that served it (sticky
sessions), and share stores with `RedisBackend`.

With `reconnect_attempts` set, a page that cannot reconnect stops after that many
attempts and shows "Connection to the server was lost." with a Reload button
(styled through `#pyreaction-lost`; `<html data-pyreaction-lost>` is set).

**Proxies.** The browser sends a small message every 25 seconds so proxies do not
close the WebSocket as idle. Nginx must pass the upgrade through and allow long
connections:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
}
```

A proxy that rewrites the `Host` header needs the public origin in
`allowed_origins`. On Google Cloud Run a WebSocket counts as a request and is
closed at the service's request timeout (5 minutes by default); raise it to 60
minutes. The page reconnects by itself when that happens.

## Security

- **Origins.** WebSockets are accepted only from the page's own origin (plus
  `allowed_origins`), so other websites cannot drive a visitor's session.
- **Authentication.** A WebSocket cannot set cookies, and JavaScript must not read
  a session cookie. Log in and out with ordinary HTTP routes that set an
  `HttpOnly` cookie, read it with `use_request().cookies`, and use
  `navigate(url, reload=True)` afterwards so the next connection carries it.
  `examples/login.py` is a complete version.
- **Markup and code.** Text is always escaped. `dangerously_set_inner_html`
  inserts HTML as is; `parse_html` removes scripts and event attributes. `js()`
  code and `js_component_from_string` modules are sent as written: never build
  them from user input.
- **Limits.** Uploads are capped by `max_upload_size`, queued events per page by
  `max_pending_events`, and pending updates to a slow browser by
  `max_pending_ops`.
- **Templates.** `jinja_components` signs the component and props it puts in the
  page; tampered links render nothing.
- **Content-Security-Policy.** The page uses inline style and small inline
  scripts for streaming, and `js_component_from_string` needs `data:` scripts;
  allow them in a policy that restricts `script-src`.
