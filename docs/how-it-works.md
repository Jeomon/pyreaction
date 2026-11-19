# How it works

What happens between the first request and a live, updating page.

1. A GET renders the component tree at the requested URL, waits for its data, and
   returns the HTML. The `<head>` preloads the client script and the page's first
   images, and a `Link` header lists the same hints for CDNs that send 103 Early
   Hints. Content waiting in a `Suspense` boundary is streamed into the same
   response when ready, with any stylesheet it needs: the content shows once that
   has loaded. The render is kept for a few seconds for the page's live connection.
2. `client.js`, a single script with no dependencies, opens a WebSocket for the
   same URL (a streamed page opens it from its first chunk, so the handshake is done
   by the end of the stream). The server takes over the kept render (or renders
   again). For the kept render it sends only what the HTML cannot carry: node ids in
   document order, a checksum of the page's shape, event listeners and live values
   such as a select's; otherwise each node's attributes and events too. The browser
   adopts the existing nodes in place, as React hydrates. If the HTML differs (a
   render that reads the clock, or markup the browser's parser changed), the
   browser asks for the whole tree and rebuilds the page.
3. A state change re-renders only the affected components. The diff in
   `core/vdom.py` matches children by key and sends operations such as "set the
   text of node 12" or "move node 40 before node 9", keeping moves to a minimum.
4. Events from one page run one at a time, in order; plain handlers and effects
   run in worker threads, so a slow one does not block other pages. One event that
   reaches several elements' handlers (a button in a clickable card) is one
   message, and the page renders once after all of them.
5. While you type, the browser ignores server values older than your latest
   keystroke, so a slow round trip never undoes typing. Once the server has handled
   an edit, the field shows the server's value, so a rejected keystroke is undone.
   The focused field keeps its focus and selection when the page around it moves.

The operation format is documented at the top of `src/reaction/core/vdom.py`.
