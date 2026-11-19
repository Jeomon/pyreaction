# How it works

What happens between the first request and a live, updating page.

1. A GET renders the component tree at the requested URL, waits for its data, and
   returns the HTML. Content waiting in a `Suspense` boundary is streamed into the
   same response when ready. The render is kept for a few seconds for the page's
   live connection.
2. `client.js`, a single script with no dependencies, opens a WebSocket for the
   same URL. The server takes over the kept render (or renders again) and sends
   each node's id, attributes and events, but not the structure the HTML already
   has; the browser adopts the existing nodes in place, as React hydrates. If the
   HTML differs (a render that reads the clock), the browser asks for the whole
   tree and rebuilds the page.
3. A state change re-renders only the affected components. The diff in
   `core/vdom.py` matches children by key and sends operations such as "set the
   text of node 12" or "move node 40 before node 9", keeping moves to a minimum.
4. Events from one page run one at a time, in order; plain handlers and effects
   run in worker threads, so a slow one does not block other pages.
5. While you type, the browser ignores server values older than your latest
   keystroke, so a slow round trip never undoes typing.

The operation format is documented at the top of `src/reaction/core/vdom.py`.
