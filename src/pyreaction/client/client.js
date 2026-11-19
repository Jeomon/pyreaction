// pyreaction browser client: applies DOM operations from the server and sends events back.
// No build step and no dependencies. The op format is documented in core/reconciler.py.
(() => {
  "use strict";

  const script = document.currentScript;
  const mount = document.getElementById(script.dataset.mount || "pyreaction-root");
  const wsPath = script.dataset.ws;

  // Set as DOM properties: they hold live state, which attributes do not.
  const PROPS = new Set(["value", "checked", "selected", "indeterminate", "muted"]);
  // Elements inside <svg> and <math> only draw when created in their namespace.
  const NS = { svg: "http://www.w3.org/2000/svg", math: "http://www.w3.org/1998/Math/MathML" };
  const create = (tag, ns) => (ns ? document.createElementNS(NS[ns], tag) : document.createElement(tag));
  const EVENT_KEYS = [
    "key", "code", "altKey", "ctrlKey", "shiftKey", "metaKey", "repeat",
    "clientX", "clientY", "offsetX", "offsetY", "button", "buttons", "deltaX", "deltaY",
  ];

  let nodes = new Map([[0, mount]]);
  let socket = null;
  let seq = 0;
  let ack = 0;
  let retry = 0;
  let sid = null; // this session's id on the server, for resuming after a drop
  // While a view transition runs, later messages wait so they apply in order.
  let animating = null;
  const waiting = [];
  const outbox = [];
  let uploads = 0;
  let maxUpload = 0; // set by the server's first message
  // The last event seq each input sent. A server value older than that is stale:
  // the person has typed since, and a newer render is on its way.
  let lastSent = new WeakMap();

  // A <select> can only take its value once its options exist.
  function selectValue(el) {
    if (el.__value === undefined) return;
    el.value = el.__value;
    if (el.__once) {
      el.__once = false;
      el.__value = undefined;
    }
  }

  function setProp(el, name, value) {
    if (name === "#html") {
      // dangerously_set_inner_html: the server's string, as markup.
      el.innerHTML = value === null ? "" : value;
      return;
    }
    if (name === "defaultValue" || name === "defaultChecked") {
      // Uncontrolled: only the starting value; the browser keeps what is typed.
      if (el.localName === "select") {
        if (el.__defaulted) return;
        el.__defaulted = true;
        el.__value = value == null ? "" : String(value);
        el.__once = true; // applied when its options arrive, then left to the person
        el.value = el.__value;
      } else if (name === "defaultChecked") el.defaultChecked = value !== null;
      else el.defaultValue = value == null ? "" : String(value);
      return;
    }
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
    for (const k of ["value", "checked", "name", "id", "type", "selectionStart", "selectionEnd"]) {
      try {
        if (t[k] !== undefined && t[k] !== null) target[k] = t[k];
      } catch (_) {} // selectionStart throws on some input types
    }
    if (t.tagName) target.tagName = t.tagName.toLowerCase();
    if (t.dataset && Object.keys(t.dataset).length) target.dataset = { ...t.dataset };
    if (t.multiple && t.selectedOptions) target.values = Array.from(t.selectedOptions, (o) => o.value);
    const data = { type: e.type, target, value: target.value, checked: target.checked };
    for (const k of EVENT_KEYS) if (e[k] !== undefined) data[k] = e[k];
    if (e.type === "submit" && t.tagName === "FORM") {
      // Pairs, so fields that share a name (checkbox groups) all arrive.
      data.form = Array.from(new FormData(t), ([k, v]) => [k, typeof v === "string" ? v : v.name]);
    }
    return data;
  }

  function send(msg) {
    if (socket && socket.readyState === 1) socket.send(msg instanceof Uint8Array ? msg : JSON.stringify(msg));
    else outbox.push(msg);
  }

  // A file goes to the server in binary frames of at most CHUNK bytes (4-byte header
  // length, JSON header, bytes), ahead of the event that refers to it by id. Small
  // frames keep under servers' WebSocket message limits and let the server write
  // large files to disk as they arrive.
  const CHUNK = 256 * 1024;

  function frame(header, bytes) {
    const head = new TextEncoder().encode(JSON.stringify(header));
    const out = new Uint8Array(4 + head.length + bytes.length);
    new DataView(out.buffer).setUint32(0, head.length);
    out.set(head, 4);
    out.set(bytes, 4 + head.length);
    return out;
  }

  async function upload(fileList) {
    const out = [];
    for (const file of fileList) {
      const meta = { name: file.name, type: file.type, size: file.size };
      if (file.size > maxUpload) {
        out.push({ ...meta, error: `larger than the ${maxUpload} byte limit` });
        continue;
      }
      const id = String(++uploads);
      for (let at = 0, part = 0; at < file.size || part === 0; at += CHUNK, part++) {
        const bytes = new Uint8Array(await file.slice(at, at + CHUNK).arrayBuffer());
        send(frame({ u: id, ...meta, part, last: at + CHUNK >= file.size }, bytes));
      }
      out.push({ ...meta, id });
    }
    return out;
  }

  function listen(el, name, spec) {
    const events = el.__ev || (el.__ev = {});
    // "click:capture" listens to click on the way down, before the target's handlers.
    const [type, phase] = name.split(":");
    const capture = phase === "capture";
    const prev = events[name];
    if (prev) el.removeEventListener(type, prev, capture);
    if (spec === null) {
      delete events[name];
      return;
    }
    const [preventDefault, stopPropagation, debounce, throttle, keys, code, stopImmediate, withForm] = spec;
    let timer = null;
    let last = 0;
    const fire = (data) => {
      const s = ++seq;
      lastSent.set(el, s);
      send([s, el.__id, name, data]);
    };
    // A js() handler runs here and sends nothing.
    const local = code ? new Function("event", "element", code) : null;
    const handler = (e) => {
      // Other keys stay entirely in the browser: not sent, default not prevented.
      if (keys && !keys.includes(e.key)) return;
      if (preventDefault) e.preventDefault();
      if (stopPropagation) e.stopPropagation();
      if (stopImmediate) e.stopImmediatePropagation();
      if (local) {
        try {
          local.call(el, e, el);
        } catch (err) {
          console.error("pyreaction: js() handler failed", err);
        }
        return;
      }
      if (e.target && e.target !== el) lastSent.set(e.target, seq + 1);
      const data = serialize(e);
      if (withForm) {
        // A button's form_action: the form's fields (with this button's), once valid.
        const form = el.form || el.closest("form");
        if (form && !form.checkValidity()) {
          form.reportValidity();
          return;
        }
        if (form) data.form = Array.from(new FormData(form, el), ([k, v]) => [k, typeof v === "string" ? v : v.name]);
      }
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
    el.addEventListener(type, handler, capture);
  }

  // ---- JavaScript components -------------------------------------------------------

  // The box a js_component's Python children render into, which the module places.
  function slotOf(el) {
    if (!el.__slot) {
      el.__slot = el.querySelector(":scope > [data-pyreaction-slot]");
      if (!el.__slot) {
        el.__slot = document.createElement("div");
        el.__slot.setAttribute("data-pyreaction-slot", "");
        el.__slot.style.display = "contents";
        el.appendChild(el.__slot);
      }
    }
    return el.__slot;
  }

  function mountJs(el, module, exportName, props, fallback) {
    const slot = slotOf(el);
    let shown = el.querySelector(":scope > [data-pyreaction-fallback]");
    if (fallback && !shown) {
      shown = document.createElement("div");
      shown.setAttribute("data-pyreaction-fallback", "");
      shown.style.display = "contents";
      shown.innerHTML = fallback;
      el.insertBefore(shown, slot);
    }
    const state = { module, props, handle: null, gone: false, slot };
    el.__js = state;
    const emit = (name, data) => {
      const payload = data !== null && typeof data === "object" && !Array.isArray(data) ? data : { value: data };
      send([++seq, el.__id, name, payload]);
    };
    import(new URL(module, location.href).href)
      .then((mod) => {
        if (state.gone) return;
        const mountFn = mod[exportName];
        if (typeof mountFn !== "function") throw new Error(`${module} has no function export "${exportName}"`);
        if (shown) shown.remove();
        state.handle = mountFn(el, state.props, emit, slot) || {};
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
      // What the module drew is its own; only the Python children are ours.
      const slot = node.__js.slot;
      if (slot) for (let c = slot.firstChild; c; c = c.nextSibling) forget(c);
      return;
    }
    for (let c = node.firstChild; c; c = c.nextSibling) forget(c);
  }

  // Where an element's children go: a js_component's slot, or the element itself.
  const inside = (el) => el.__slot || el;

  // A value the server can receive: JSON as is, DOMRect and friends through toJSON,
  // anything else as text.
  function toJson(value) {
    if (value === undefined) return null;
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      return String(value);
    }
  }

  // ref.current.call/get/set from Python: run it and send the answer back.
  async function callElement(el, method, args, reply) {
    let value = null;
    let error = null;
    try {
      if (!el) throw new Error("the element is no longer on the page");
      if (method === "#get") value = el[args[0]];
      else if (method === "#set") el[args[0]] = args[1];
      else if (typeof el[method] === "function") value = await el[method](...args);
      else throw new Error(`the element has no method ${method}`);
    } catch (err) {
      error = String(err && err.message ? err.message : err);
      if (!reply) console.error(`pyreaction: ${method} failed`, err);
    }
    if (reply) send([++seq, 0, "#reply", error === null ? { r: reply, value: toJson(value) } : { r: reply, error }]);
  }

  function apply(ops) {
    for (const op of ops) {
      switch (op[0]) {
        case "c": {
          const el = create(op[2], op[5]);
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
          mountJs(el, op[3], op[4], op[5], op[6]);
          break;
        }
        case "J":
          updateJs(nodes.get(op[1]), op[2]);
          break;
        case "P": {
          // A portal: a box at the end of another element of the page.
          const el = document.createElement("div");
          el.setAttribute("data-pyreaction-portal", "");
          el.style.display = "contents";
          register(op[1], el);
          (document.querySelector(op[2]) || document.body).appendChild(el);
          break;
        }
        case "A": {
          const parent = nodes.get(op[1]);
          const frag = document.createDocumentFragment();
          for (const id of op[2]) frag.appendChild(nodes.get(id));
          inside(parent).appendChild(frag);
          // A <select> can only take its value once its options exist.
          selectValue(parent);
          break;
        }
        case "a": {
          const parent = nodes.get(op[1]);
          inside(parent).insertBefore(nodes.get(op[2]), op[3] === null ? null : nodes.get(op[3]));
          selectValue(parent);
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
        case "m":
          callElement(nodes.get(op[1]), op[2], op[3], op[4]);
          break;
      }
    }
  }

  // Markup under `root` in document order: the order of the server's first message.
  function domOrder(root) {
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
          if (c.hasAttribute("data-pyreaction-fallback") || c.hasAttribute("data-pyreaction-portal")) continue;
          if (c.hasAttribute("data-pyreaction-slot")) {
            walk(c); // a js_component's Python children
            continue;
          }
          out.push(c);
          if (c.__js) {
            if (c.__js.slot) walk(c.__js.slot);
          } else if (c.localName !== "textarea" && !c.hasAttribute("data-pyreaction-raw")) walk(c);
        }
      }
    };
    walk(root);
    return out;
  }

  const created = (ops) => ops.filter((op) => op[0] === "c" || op[0] === "t" || op[0] === "j");

  function matches(ops, found) {
    const made = created(ops);
    return (
      made.length === found.length &&
      made.every((op, i) => {
        const n = found[i];
        return op[0] === "t" ? n.nodeType === 3 && n.data === op[2] : n.nodeType === 1 && n.localName === op[2];
      })
    );
  }

  function adopt(ops, found) {
    created(ops).forEach((op, i) => {
      const n = found[i];
      register(op[1], n);
      if (op[0] === "j") {
        // Still mounted from the last session: keep it and pass the new props.
        if (n.__js && n.__js.module === op[3]) updateJs(n, op[5]);
        else mountJs(n, op[3], op[4], op[5], op[6]);
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

  // Adopt the nodes already on the page; where they differ, build afresh. The
  // message lists the page's own tree, then each portal (["P", ...] and its
  // content) in the order the server numbered the portal boxes it rendered.
  function hydrate(ops) {
    const segments = [[]];
    for (const op of ops) {
      if (op[0] === "P") segments.push([op]);
      else segments[segments.length - 1].push(op);
    }
    const own = segments[0];
    const found = domOrder(mount);
    if (!matches(own, found)) {
      console.warn("pyreaction: the page's markup differs from this render (clock, randomness?); rebuilding it");
      mount.dataset.hydration = "rebuilt";
      return replace(ops);
    }
    mount.dataset.hydration = "adopted";
    nodes = new Map([[0, mount]]);
    adopt(own, found);
    const kept = new Set();
    segments.slice(1).forEach((seg, k) => {
      const box = document.querySelector(`[data-pyreaction-portal="${k}"]`);
      const inside = box ? domOrder(box) : null;
      if (box && matches(seg, inside)) {
        register(seg[0][1], box);
        kept.add(box);
        adopt(seg, inside);
      } else apply(seg); // a portal whose markup differs is built afresh
    });
    // Boxes nothing adopted: from the server's HTML or the last session.
    for (const box of document.querySelectorAll("[data-pyreaction-portal]")) {
      if (box.__id === undefined || (!kept.has(box) && nodes.get(box.__id) !== box)) box.remove();
    }
  }

  function clearPortals() {
    for (const el of document.querySelectorAll("[data-pyreaction-portal]")) el.remove();
  }

  function replace(ops) {
    clearPortals();
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
    if (nav.reload) {
      // A full load, so the server sees new cookies (after login or logout).
      if (nav.replace) location.replace(nav.to);
      else location.assign(nav.to);
      return;
    }
    if (nav.to === here()) return;
    if (nav.replace) history.replaceState(null, "", nav.to);
    else {
      history.pushState(null, "", nav.to);
      scrollTo(0, 0);
    }
  }

  // ---- <head> tags from Head components ----------------------------------------------

  // Tags already in <head> stay as they are (a stylesheet is not reloaded, a script
  // not run again); new ones are added, and those no longer asked for removed.
  function setHead(markup) {
    const t = document.createElement("template");
    t.innerHTML = markup;
    const old = new Map();
    for (const el of document.head.querySelectorAll("[data-pyreaction-head]")) {
      const same = old.get(el.outerHTML) || [];
      same.push(el);
      old.set(el.outerHTML, same);
    }
    for (const el of Array.from(t.content.children)) {
      const kept = old.get(el.outerHTML);
      if (kept && kept.length) {
        kept.shift();
        continue;
      }
      let node = el;
      if (el.localName === "script") {
        // A script parsed from markup never runs: make a fresh one so it does.
        node = document.createElement("script");
        for (const a of el.attributes) node.setAttribute(a.name, a.value);
        node.text = el.text;
      }
      document.head.appendChild(node);
    }
    for (const same of old.values()) for (const el of same) el.remove();
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

  // Reconnect delays grow to RECONNECT_MAX ms, with jitter so a restarted server is
  // not hit by every page at the same moment.
  const RECONNECT_MAX = Number(script.dataset.reconnectMax || 8000);
  // A message every HEARTBEAT ms keeps proxies from closing an idle connection.
  const HEARTBEAT = 25000;
  let heartbeat = null;

  function connect() {
    const url = new URL(wsPath, location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("p", here());
    // Offer to adopt what is on the page: the server's HTML, or the last session's tree.
    if (mount.firstChild) url.searchParams.set("h", "1");
    // Ask to resume the last session, which the server keeps for a while.
    if (sid) url.searchParams.set("s", sid);
    socket = new WebSocket(url);
    socket.onopen = () => {
      retry = 0;
      document.documentElement.removeAttribute("data-pyreaction-offline");
      clearInterval(heartbeat);
      heartbeat = setInterval(() => send([0, 0, "#ping", {}]), HEARTBEAT);
    };
    const receive = (data) => {
      if (data.vt && !data.init && document.startViewTransition) {
        animating = document
          .startViewTransition(() => handle(data))
          .updateCallbackDone.catch(() => {})
          .finally(() => {
            animating = null;
            while (waiting.length && !animating) receive(waiting.shift());
          });
      } else handle(data);
    };
    socket.onmessage = (msg) => {
      const data = JSON.parse(msg.data);
      if (animating) waiting.push(data);
      else receive(data);
    };
    const handle = (data) => {
      if (data.init) {
        sid = data.sid || null;
        maxUpload = data.maxUpload || 0;
        // A resumed session keeps its node ids and event numbers, so events made
        // while offline still apply. A new one numbers everything afresh: nothing
        // queued for the old one, and no pending input edit, may carry over.
        if (!data.resumed) {
          outbox.length = 0;
          seq = 0;
          lastSent = new WeakMap();
        }
      }
      ack = data.ack;
      if (data.init === "hydrate") {
        hydrate(data.ops);
        if (mount.dataset.hydration === "rebuilt") send([0, 0, "#mismatch", { path: here() }]);
      } else if (data.init === "replace") replace(data.ops);
      else apply(data.ops);
      if (data.title != null) document.title = data.title;
      if (data.head != null) setHead(data.head);
      if (data.nav) followServer(data.nav);
      if (data.errors) showErrors(data.errors);
      // The page may have moved while the socket was down.
      if (data.init && here() !== url.searchParams.get("p")) navigated();
      while (outbox.length && socket.readyState === 1) send(outbox.shift());
      // The page is live: browser tests (and anyone curious) can wait for this.
      if (data.init) document.documentElement.setAttribute("data-pyreaction-ready", "");
    };
    socket.onclose = () => {
      socket = null;
      clearInterval(heartbeat);
      document.documentElement.setAttribute("data-pyreaction-offline", "");
      document.documentElement.removeAttribute("data-pyreaction-ready");
      const delay = Math.min(250 * 2 ** retry++, RECONNECT_MAX);
      setTimeout(connect, delay / 2 + Math.random() * (delay / 2));
    };
  }

  connect();
})();
