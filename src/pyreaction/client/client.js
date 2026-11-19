// pyreaction browser client: applies DOM operations from the server and sends events back.
// No build step and no dependencies. The op format is documented in core/reconciler.py.
(() => {
  "use strict";

  const script = document.currentScript;
  const mount = document.getElementById(script.dataset.mount || "pyreaction-root");
  const wsPath = script.dataset.ws;

  // Set as DOM properties: they hold live state, which attributes do not.
  const PROPS = new Set(["value", "checked", "selected", "indeterminate", "muted"]);
  const EVENT_KEYS = [
    "key", "code", "altKey", "ctrlKey", "shiftKey", "metaKey", "repeat",
    "clientX", "clientY", "offsetX", "offsetY", "button", "buttons", "deltaX", "deltaY",
  ];

  let nodes = new Map([[0, mount]]);
  let socket = null;
  let seq = 0;
  let ack = 0;
  let retry = 0;
  const outbox = [];
  let uploads = 0;
  let maxUpload = 0; // set by the server's first message
  // The last event seq each input sent. A server value older than that is stale:
  // the person has typed since, and a newer render is on its way.
  let lastSent = new WeakMap();

  function setProp(el, name, value) {
    if (PROPS.has(name)) {
      if (name !== "value" && name !== "indeterminate" && name !== "muted") value = !!value && value !== "";
      if (name === "value") {
        value = value == null ? "" : String(value);
        if (el.localName === "select") el.__value = value;
      }
      if ((lastSent.get(el) || 0) > ack) return;
      if (el[name] !== value) el[name] = value;
      return;
    }
    if (value === null) el.removeAttribute(name);
    else el.setAttribute(name, value);
  }

  function serialize(e) {
    const t = e.target || {};
    const target = {};
    for (const k of ["value", "checked", "name", "id", "type"]) if (t[k] !== undefined) target[k] = t[k];
    if (t.tagName) target.tagName = t.tagName.toLowerCase();
    if (t.multiple && t.selectedOptions) target.values = Array.from(t.selectedOptions, (o) => o.value);
    const data = { type: e.type, target, value: target.value, checked: target.checked };
    for (const k of EVENT_KEYS) if (e[k] !== undefined) data[k] = e[k];
    if (e.type === "submit" && t.tagName === "FORM") {
      const form = {};
      for (const [k, v] of new FormData(t)) form[k] = typeof v === "string" ? v : v.name;
      data.form = form;
    }
    return data;
  }

  function send(msg) {
    if (socket && socket.readyState === 1) socket.send(msg instanceof Uint8Array ? msg : JSON.stringify(msg));
    else outbox.push(msg);
  }

  // A file goes to the server as one binary frame (4-byte header length, JSON
  // header, bytes) ahead of the event that refers to it by id.
  async function upload(fileList) {
    const out = [];
    for (const file of fileList) {
      const meta = { name: file.name, type: file.type, size: file.size };
      if (file.size > maxUpload) {
        out.push({ ...meta, error: `larger than the ${maxUpload} byte limit` });
        continue;
      }
      const id = String(++uploads);
      const header = new TextEncoder().encode(JSON.stringify({ u: id, ...meta }));
      const body = new Uint8Array(await file.arrayBuffer());
      const frame = new Uint8Array(4 + header.length + body.length);
      new DataView(frame.buffer).setUint32(0, header.length);
      frame.set(header, 4);
      frame.set(body, 4 + header.length);
      send(frame);
      out.push({ ...meta, id });
    }
    return out;
  }

  function listen(el, name, spec) {
    const events = el.__ev || (el.__ev = {});
    const prev = events[name];
    if (prev) el.removeEventListener(name, prev);
    if (spec === null) {
      delete events[name];
      return;
    }
    const [preventDefault, stopPropagation, debounce, throttle, keys] = spec;
    let timer = null;
    let last = 0;
    const fire = (data) => {
      const s = ++seq;
      lastSent.set(el, s);
      send([s, el.__id, name, data]);
    };
    const handler = (e) => {
      // Other keys stay entirely in the browser: not sent, default not prevented.
      if (keys && !keys.includes(e.key)) return;
      if (preventDefault) e.preventDefault();
      if (stopPropagation) e.stopPropagation();
      if (e.target && e.target !== el) lastSent.set(e.target, seq + 1);
      const data = serialize(e);
      const t = e.target;
      if (t && t.localName === "input" && t.type === "file" && t.files && t.files.length) {
        upload(t.files).then((files) => fire({ ...data, files }));
      } else if (debounce) {
        clearTimeout(timer);
        timer = setTimeout(() => fire(data), debounce);
      } else if (throttle) {
        const now = Date.now();
        if (now - last >= throttle) {
          last = now;
          fire(data);
        }
      } else fire(data);
    };
    events[name] = handler;
    el.addEventListener(name, handler);
  }

  // ---- JavaScript components -------------------------------------------------------

  function mountJs(el, module, exportName, props) {
    const state = { module, props, handle: null, gone: false };
    el.__js = state;
    const emit = (name, data) => {
      const payload = data !== null && typeof data === "object" && !Array.isArray(data) ? data : { value: data };
      send([++seq, el.__id, name, payload]);
    };
    import(new URL(module, location.href).href)
      .then((mod) => {
        if (state.gone) return;
        const mount = mod[exportName];
        if (typeof mount !== "function") throw new Error(`${module} has no function export "${exportName}"`);
        state.handle = mount(el, state.props, emit) || {};
      })
      .catch((err) => console.error(`pyreaction: could not mount ${module}`, err));
  }

  function updateJs(el, props) {
    const state = el.__js;
    state.props = props;
    if (state.handle && state.handle.update) state.handle.update(props);
  }

  function register(id, node) {
    node.__id = id;
    nodes.set(id, node);
  }

  function forget(node) {
    if (node.__id !== undefined) nodes.delete(node.__id);
    if (node.__js) {
      node.__js.gone = true;
      const handle = node.__js.handle;
      if (handle && handle.unmount) {
        try {
          handle.unmount();
        } catch (err) {
          console.error("pyreaction: unmount failed", err);
        }
      }
      return; // what the module drew is its own
    }
    for (let c = node.firstChild; c; c = c.nextSibling) forget(c);
  }

  function apply(ops) {
    for (const op of ops) {
      switch (op[0]) {
        case "c": {
          const el = document.createElement(op[2]);
          register(op[1], el);
          for (const k in op[3]) setProp(el, k, op[3][k]);
          for (const k in op[4]) listen(el, k, op[4][k]);
          break;
        }
        case "t":
          register(op[1], document.createTextNode(op[2]));
          break;
        case "j": {
          const el = document.createElement(op[2]);
          register(op[1], el);
          mountJs(el, op[3], op[4], op[5]);
          break;
        }
        case "J":
          updateJs(nodes.get(op[1]), op[2]);
          break;
        case "A": {
          const parent = nodes.get(op[1]);
          const frag = document.createDocumentFragment();
          for (const id of op[2]) frag.appendChild(nodes.get(id));
          parent.appendChild(frag);
          // A <select> can only take its value once its options exist.
          if (parent.__value !== undefined) parent.value = parent.__value;
          break;
        }
        case "a": {
          const parent = nodes.get(op[1]);
          parent.insertBefore(nodes.get(op[2]), op[3] === null ? null : nodes.get(op[3]));
          if (parent.__value !== undefined) parent.value = parent.__value;
          break;
        }
        case "r": {
          const node = nodes.get(op[1]);
          if (node) {
            node.remove();
            forget(node);
          }
          break;
        }
        case "p":
          setProp(nodes.get(op[1]), op[2], op[3]);
          break;
        case "x":
          nodes.get(op[1]).data = op[2];
          break;
        case "e":
          listen(nodes.get(op[1]), op[2], op[3]);
          break;
        case "m": {
          const el = nodes.get(op[1]);
          if (el && typeof el[op[2]] === "function") {
            try {
              el[op[2]](...op[3]);
            } catch (err) {
              console.error(`pyreaction: ${op[2]}() failed`, err);
            }
          }
          break;
        }
      }
    }
  }

  // The page's markup in document order: the order of the server's first message.
  function domOrder() {
    const out = [];
    const walk = (parent) => {
      for (let c = parent.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 8) {
          if (c.data === "$") {
            const empty = document.createTextNode("");
            parent.replaceChild(empty, c);
            c = empty;
            out.push(c);
          }
        } else if (c.nodeType === 3) out.push(c);
        else if (c.nodeType === 1) {
          out.push(c);
          // A JavaScript component's contents are its own, not part of the tree.
          if (c.localName !== "textarea" && !c.__js) walk(c);
        }
      }
    };
    walk(mount);
    return out;
  }

  // Adopt the nodes already on the page; on any difference build the tree afresh.
  function hydrate(ops) {
    const found = domOrder();
    const created = ops.filter((op) => op[0] === "c" || op[0] === "t" || op[0] === "j");
    const ok =
      created.length === found.length &&
      created.every((op, i) => {
        const n = found[i];
        return op[0] === "t" ? n.nodeType === 3 && n.data === op[2] : n.nodeType === 1 && n.localName === op[2];
      });
    if (!ok) {
      console.warn("pyreaction: the page's markup differs from this render (clock, randomness?); rebuilding it");
      mount.dataset.hydration = "rebuilt";
      return replace(ops);
    }
    mount.dataset.hydration = "adopted";
    nodes = new Map([[0, mount]]);
    created.forEach((op, i) => {
      const n = found[i];
      register(op[1], n);
      if (op[0] === "j") {
        // Still mounted from the last session: keep it and pass the new props.
        if (n.__js && n.__js.module === op[3]) updateJs(n, op[5]);
        else mountJs(n, op[3], op[4], op[5]);
        return;
      }
      if (op[0] !== "c") return;
      const attrs = op[3];
      for (const a of Array.from(n.attributes)) if (!(a.name in attrs)) n.removeAttribute(a.name);
      for (const k in attrs) setProp(n, k, attrs[k]);
      // Listeners left from a previous session would send stale node ids.
      for (const k in n.__ev || {}) if (!(k in op[4])) listen(n, k, null);
      for (const k in op[4]) listen(n, k, op[4][k]);
    });
    apply(ops.filter((op) => op[0] === "m"));
  }

  function replace(ops) {
    mount.textContent = "";
    nodes = new Map([[0, mount]]);
    apply(ops);
  }

  // ---- navigation ----------------------------------------------------------------

  const here = () => location.pathname + location.search;

  function navigated() {
    send([++seq, 0, "#navigate", { to: here() }]);
  }

  // Links made with Link() change the URL in place; modified clicks still open tabs.
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a[data-pyreaction-link]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target && a.target !== "_self") return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname + url.search === here() && url.hash) return; // in-page anchor
    e.preventDefault();
    if (a.dataset.pyreactionLink === "replace") history.replaceState(null, "", url);
    else {
      history.pushState(null, "", url);
      scrollTo(0, 0);
    }
    navigated();
  });

  addEventListener("popstate", navigated);

  function followServer(nav) {
    if (nav.to === here()) return;
    if (nav.replace) history.replaceState(null, "", nav.to);
    else {
      history.pushState(null, "", nav.to);
      scrollTo(0, 0);
    }
  }

  // ---- errors (debug mode) ---------------------------------------------------------

  function showErrors(errors) {
    let box = document.getElementById("pyreaction-errors");
    if (!box) {
      box = document.createElement("div");
      box.id = "pyreaction-errors";
      box.setAttribute("role", "alert");
      const close = document.createElement("button");
      close.textContent = "Dismiss";
      close.onclick = () => box.remove();
      box.appendChild(close);
      document.body.appendChild(box);
    }
    for (const text of errors) {
      const pre = document.createElement("pre");
      pre.textContent = text;
      box.appendChild(pre);
    }
  }

  // ---- connection ----------------------------------------------------------------

  function connect() {
    const url = new URL(wsPath, location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("p", here());
    // Offer to adopt what is on the page: the server's HTML, or the last session's tree.
    if (mount.firstChild) url.searchParams.set("h", "1");
    socket = new WebSocket(url);
    socket.onopen = () => {
      retry = 0;
      document.documentElement.removeAttribute("data-pyreaction-offline");
    };
    socket.onmessage = (msg) => {
      const data = JSON.parse(msg.data);
      if (data.init) {
        // A new session numbers its nodes and events afresh: nothing queued for the
        // old one, and no pending input edit, may carry over.
        outbox.length = 0;
        seq = 0;
        lastSent = new WeakMap();
        maxUpload = data.maxUpload || 0;
      }
      ack = data.ack;
      if (data.init === "hydrate") hydrate(data.ops);
      else if (data.init === "replace") replace(data.ops);
      else apply(data.ops);
      if (data.title != null) document.title = data.title;
      if (data.nav) followServer(data.nav);
      if (data.errors) showErrors(data.errors);
      // The page may have moved while the socket was down.
      if (data.init && here() !== url.searchParams.get("p")) navigated();
      while (outbox.length && socket.readyState === 1) send(outbox.shift());
    };
    socket.onclose = () => {
      socket = null;
      document.documentElement.setAttribute("data-pyreaction-offline", "");
      setTimeout(connect, Math.min(250 * 2 ** retry++, 8000));
    };
  }

  connect();
})();
