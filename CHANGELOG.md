# Changelog

All notable changes to pyreaction are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0 the API may still change
between minor versions.

## [Unreleased]

### Changed

- A page's first render (and its HTML) runs in a worker thread when `threaded`, so
  a large page no longer holds up the other pages on the event loop: while a
  10,000-row page renders, the loop's longest pause drops from about 67 ms to
  about 19 ms. A path that rendered in under 5 ms last time stays on the loop.
  Components may now render in a thread other than the event loop's; asyncio work
  they start (`cache`, `use`, `use_data`, `lazy`) is handed to the loop.

### Performance

- A mounted page no longer keeps the elements it rendered: each HTML node keeps
  its attributes, events and children, and a serial number to recognise an
  unchanged element. A 10,000-row page holds about 16.2 MB on the server, from
  19.9 MB; update times are unchanged.

### Fixed

- A ref's `focus()` or `blur()` called from an event handler did nothing: the
  browser put the focus back where it was before the update (a regression in
  0.1.0, from keeping the focus when a list reorders).
- An error in a page's first render made `on_error` itself fail (`NameError`), so
  the handler never saw it.

## [0.1.0] - 2026-11-20

The first public version.

### Added

- React's component model in Python: components, hooks (`use_state`,
  `use_reducer`, `use_effect`, `use_memo`, `use_ref`, `use_context` and the rest),
  keys, context, error boundaries, Suspense, portals, refs, `StrictMode`, and
  React 19's form actions, `use_optimistic`, `use`, `cache`, resource hints and
  `ViewTransition`.
- Server rendering: every URL returns HTML with its data; Suspense content streams
  into the same response; the live page adopts the HTML instead of redrawing it.
- Live updates over a WebSocket: the server diffs the tree and sends DOM
  operations. Pages reconnect by themselves and resume with their state.
- `VirtualList`, and stores shared across pages (`Store`, `use_page_store`,
  `use_store` with selectors; `RedisBackend` for several workers).
- Integrations: mount in FastAPI (with `Depends` and `@page` routes), wrap Django
  or Flask, render components from Jinja templates.
- JavaScript interop: `js()` handlers, `js_component` and
  `js_component_from_string`, element methods called from Python, event
  properties read on demand.
- `reaction.testing`: render components against a fake DOM, or serve them to a
  real browser.
- The `reaction` command: `init`, `dev` and `run`.

### Changed

- One native event that reaches several elements' handlers (a button inside a
  clickable card) is sent as one message: the handlers run in order and the page
  renders once, as in React. A later handler sees the state from before the event;
  use the function form, `set_n(lambda n: n + 1)`, to build on an earlier one.
- A `style` given as a dict updates only the properties that changed, so styles a
  script set on the element stay.
- A controlled input shows the server's value once it has handled an edit, so a
  handler that rejects a keystroke undoes it.
- `max_pending_ops` counts only updates queued behind the one being sent, so a
  single large render (10,000 new rows) is always delivered.

### Fixed

- A page whose single render exceeded `max_pending_ops` reconnected forever.
- A memo component given elements in a tuple prop kept stale event handlers.
- Streamed pages rendered and loaded their data a second time when the browser
  connected.
- The focused input lost its focus and selection when its list was reordered.
- Removing `margin` and adding `margin-top` in one update lost `margin-top`.

### Performance

Measured on a 10,000-row table (`benchmarks/bench.py`); see the commit history
for each change and its numbers.

- Page loads: about 82 ms to serve, from about 170 ms; about 20 MB of server
  memory per page, from about 29 MB.
- The browser's first message after loading a page is a compact form (node ids,
  a checksum, listeners) when the server kept the render that made the HTML.
- The client script and the page's first images are preloaded from `<head>`, and
  a `Link` header lists them for CDNs that send 103 Early Hints.
- A streamed page opens its WebSocket from its first chunk, and a Suspense
  section's stylesheet arrives with it.
- Faster diffing, unmounting and HTML rendering (selecting one row: 12 to 7.6 ms;
  removing the first row: 19 to 9 ms; HTML: 16 to 10 ms).

[Unreleased]: https://github.com/Jeomon/pyreaction/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Jeomon/pyreaction/releases/tag/v0.1.0
