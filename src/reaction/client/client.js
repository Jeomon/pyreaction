// pyreaction browser client: applies DOM operations from the server and sends events back.
// No build step and no dependencies. The op format is documented in core/vdom.py.
(() => {
  "use strict";

  const script = document.currentScript;
  const mount = document.getElementById(script.dataset.mount || "pyreaction-root");
  const wsPath = script.dataset.ws;
  // Where the app is mounted ("/ui"), or "" at the root of the site. The server
  // works with paths inside the app; the browser shows them with this in front.
  const base = (script.dataset.base || "").replace(/\/$/, "");
  // Set when the page is a FastAPI @page route: it shows that one path, and other
  // paths are other pages, which the browser loads itself.
  const pagePath = script.dataset.page || "";
  const leavesPage = (pathname) =>
    pagePath !== "" && pathname.replace(/\/$/, "") !== pagePath.replace(/\/$/, "");

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
  let handoff = script.dataset.handoff || ""; // single use: the page's first render
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

  // "2.50" typed and 2.5 from the server are the same number: what was typed stays.
  const sameNumber = (el, value) =>
    el.type === "number" && el.value !== "" && value !== "" && Number(el.value) === Number(value);
  // Controlled inputs that sent an edit. Once the server has handled it, the field
  // shows the server's value even when that did not change (a rejected keystroke,
  // which no "p" op would undo), as React's restoreControlledState does.
  const edited = new Set();
  function markEdited(target) {
    if (target && target.__ctl !== undefined) edited.add(target);
  }
  function restoreControlled() {
    for (const el of edited) {
      if ((lastSent.get(el) || 0) > ack) continue; // its edit is not handled yet
      edited.delete(el);
      if (el.isConnected && el.value !== el.__ctl && !sameNumber(el, el.__ctl)) el.value = el.__ctl;
    }
  }

  function setProp(el, name, value) {
    if (name === "#html") {
      // dangerously_set_inner_html: the server's string, as markup.
      el.innerHTML = value === null ? "" : value;
      return;
    }
    if (name === "#text") {
      // An element whose only child is text holds it itself, as React does.
      const text = value === null ? "" : value;
      if (el.textContent !== text) el.textContent = text;
      return;
    }
    if (name === "style" && value !== null && typeof value === "object") {
      // Only the properties that changed (null: removed); the rest stay, including
      // any a script set on the element.
      // Removals first, as React does: removing a shorthand (margin) must not clear
      // a part of it (margin-top) set in the same update.
      for (const k in value) if (value[k] === null) el.style.removeProperty(k);
      for (const k in value) {
        const v = value[k];
        if (v === null) continue;
        const important = /\s*!important\s*$/i;
        if (important.test(v)) el.style.setProperty(k, v.replace(important, ""), "important");
        else el.style.setProperty(k, v);
      }
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
        el.__ctl = value; // the server's value, restored after an edit it did not take
      }
      if ((lastSent.get(el) || 0) > ack) return;
      if (name === "value" && sameNumber(el, value)) return;
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
    if (e.type === "scroll") {
      // Read only for scroll events: reading layout on every event would be slow.
      const box = t === document ? document.scrollingElement : t;
      if (box) {
        target.scrollTop = box.scrollTop;
        target.scrollLeft = box.scrollLeft;
      }
    }
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

  // Events whose listeners can block scrolling until they return.
  const PASSIVE = new Set(["wheel", "touchstart", "touchmove"]);
  // js() handler code, compiled once however many elements share it.
  const compiled = new Map();

  // Bubbling events are listened to once per root (the page, and each portal box), as
  // React does: one native event that passes several elements with handlers sends one
  // message naming them all, and the server runs them in order, then renders once.
  // Events that do not bubble, and wheel/touch (kept passive per element), listen on
  // the element itself.
  const DELEGATED = new Set([
    "click", "dblclick", "auxclick", "contextmenu", "mousedown", "mouseup", "mousemove", "mouseover",
    "mouseout", "pointerdown", "pointerup", "pointermove", "pointerover", "pointerout", "pointercancel",
    "keydown", "keyup", "keypress", "input", "change", "beforeinput", "submit", "reset", "focusin",
    "focusout", "dragstart", "drag", "dragend", "dragenter", "dragover", "dragleave", "drop", "copy",
    "cut", "paste", "compositionstart", "compositionupdate", "compositionend", "touchend", "touchcancel",
    "animationstart", "animationend", "animationiteration", "transitionstart", "transitionend",
    "transitionrun", "transitioncancel",
  ]);
  const delegatedNames = new Set(); // "click", "click:capture"... listened to at the roots
  const roots = new Set([mount]);

  function delegate(name) {
    if (delegatedNames.has(name)) return;
    delegatedNames.add(name);
    for (const root of roots) listenAtRoot(root, name);
  }

  // A portal box: events inside it bubble to it, not to the page.
  function addRoot(box) {
    if (roots.has(box)) return;
    roots.add(box);
    for (const name of delegatedNames) listenAtRoot(box, name);
  }

  function listenAtRoot(root, name) {
    const [type, phase] = name.split(":");
    const capture = phase === "capture";
    root.addEventListener(type, (e) => dispatchFromRoot(e, root, name, capture), capture);
  }

  const hasFiles = (t) => t && t.localName === "input" && t.type === "file" && t.files && t.files.length;

  // While a message is being applied: focus events the browser fires because the
  // client moved the focused element are not the person's (React turns its event
  // system off during a commit).
  let applying = false;
  const FOCUS_EVENTS = new Set(["focus", "blur", "focusin", "focusout"]);

  function dispatchFromRoot(e, root, name, capture) {
    if (applying && FOCUS_EVENTS.has(e.type)) return;
    // A portal box inside the page is a root inside a root: handle the event once.
    const mark = "__pr " + name;
    if (e[mark]) return;
    e[mark] = true;
    const path = [];
    for (let n = e.target; n; n = n.parentNode) {
      if (n.__ev && n.__ev[name]) path.push(n);
      if (n === root) break;
    }
    if (capture) path.reverse(); // capture handlers run from the outside in
    const batch = [];
    for (const el of path) {
      const record = el.__ev[name];
      const [preventDefault, stopPropagation, , , keys, , stopImmediate] = record.spec;
      // Other keys stay entirely in the browser: not sent, default not prevented.
      if (keys && !keys.includes(e.key)) continue;
      if (preventDefault) e.preventDefault();
      if (record.local) record.runLocal(e);
      else if (record.deferred || hasFiles(e.target)) record.deliver(e); // its own message, later
      else batch.push(el);
      if (stopImmediate) {
        e.stopImmediatePropagation();
        break;
      }
      if (stopPropagation) {
        e.stopPropagation();
        break;
      }
    }
    if (!batch.length) return;
    const s = ++seq;
    for (const el of batch) lastSent.set(el, s);
    if (e.target) lastSent.set(e.target, s);
    markEdited(e.target);
    remember(s, e);
    send([s, batch.length === 1 ? batch[0].__id : batch.map((el) => el.__id), name, serialize(e)]);
  }

  function listen(el, name, spec) {
    const events = el.__ev || (el.__ev = {});
    // "click:capture" listens to click on the way down, before the target's handlers.
    const [type, phase] = name.split(":");
    const capture = phase === "capture";
    const prev = events[name];
    if (prev && prev.native) el.removeEventListener(type, prev.native, capture);
    if (spec === null) {
      delete events[name];
      return;
    }
    const [preventDefault, stopPropagation, debounce, throttle, keys, code, stopImmediate, withForm] = spec;
    let timer = null;
    let last = 0;
    const fire = (data, e) => {
      const s = ++seq;
      lastSent.set(el, s);
      markEdited(e && e.target);
      remember(s, e);
      send([s, el.__id, name, data]);
    };
    // A js() handler runs here and sends nothing; one function per distinct code.
    let local = null;
    if (code) {
      local = compiled.get(code);
      if (!local) compiled.set(code, (local = new Function("event", "element", code)));
    }
    const runLocal = (e) => {
      try {
        local.call(el, e, el);
      } catch (err) {
        console.error("pyreaction: js() handler failed", err);
      }
    };
    // Sends this element's event in a message of its own: after its debounce or
    // throttle, with its form's fields, or once its files have uploaded.
    const deliver = (e) => {
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
      if (hasFiles(t)) {
        upload(t.files).then((files) => fire({ ...data, files }, e));
      } else if (debounce) {
        clearTimeout(timer);
        timer = setTimeout(() => fire(data, e), debounce);
      } else if (throttle) {
        // At most one event per window, and the last one of a burst always goes,
        // so the server ends with the final state (where scrolling stopped).
        const now = Date.now();
        clearTimeout(timer);
        if (now - last >= throttle) {
          last = now;
          fire(data, e);
        } else {
          timer = setTimeout(() => {
            last = Date.now();
            fire(data, e);
          }, throttle - (now - last));
        }
      } else fire(data, e);
    };
    const record = { spec, local, runLocal, deliver, deferred: !!(debounce || throttle || withForm), native: null };
    events[name] = record;
    if (DELEGATED.has(type)) {
      delegate(name);
      return;
    }
    record.native = (e) => {
      if (applying && FOCUS_EVENTS.has(e.type)) return;
      if (keys && !keys.includes(e.key)) return;
      if (preventDefault) e.preventDefault();
      if (stopPropagation) e.stopPropagation();
      if (stopImmediate) e.stopImmediatePropagation();
      if (local) runLocal(e);
      else deliver(e);
    };
    // A wheel or touch listener that cannot cancel the event is passive, so the browser
    // scrolls at once instead of waiting for it (as React does). One that prevents the
    // default, or runs js() code that might, stays active.
    const passive = PASSIVE.has(type) && !preventDefault && !code;
    el.addEventListener(type, record.native, passive ? { capture, passive } : capture);
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
    import(new URL(toBrowser(module), location.href).href) // "/static/x.js" is inside the app
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

  // Removed subtrees' ids are dropped once the browser is idle, as React detaches
  // deleted fibers after the commit: the update that removed them paints first, and
  // the server never names a removed node again.
  const graveyard = [];
  const later = typeof requestIdleCallback === "function"
    ? (f) => requestIdleCallback(f, { timeout: 200 })
    : (f) => setTimeout(f, 50);
  function bury() {
    for (const node of graveyard.splice(0)) forget(node);
  }

  function forget(node) {
    roots.delete(node); // a portal box: its delegated listeners go with it
    // Only its own entry: a new session (or a rebuild) may reuse the id by now.
    if (node.__id !== undefined && nodes.get(node.__id) === node) nodes.delete(node.__id);
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
    if (typeof File !== "undefined" && value instanceof File) {
      return { name: value.name, type: value.type, size: value.size, lastModified: value.lastModified };
    }
    // FileList, classList, DataTransfer types and other lists: their items.
    if (value && typeof value === "object" && !Array.isArray(value) && typeof value.length === "number" &&
        typeof value[Symbol.iterator] === "function") {
      return Array.from(value, toJson);
    }
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      return String(value);
    }
  }

  // Recent events, so Python can read more of one than was sent (e.read / e.call).
  const EVENT_KEEP = 50;
  const EVENT_MS = 60000;
  const recentEvents = new Map();

  function remember(s, e) {
    const now = Date.now();
    recentEvents.set(s, { e, at: now });
    for (const [k, v] of recentEvents) {
      if (recentEvents.size <= EVENT_KEEP && now - v.at < EVENT_MS) break;
      recentEvents.delete(k);
    }
  }

  async function readEvent([s, path, args, isCall]) {
    const kept = recentEvents.get(s);
    if (!kept || Date.now() - kept.at > EVENT_MS) throw new Error("the event is no longer available");
    let parent = null;
    let value = kept.e;
    for (const part of path ? path.split(".") : []) {
      parent = value;
      value = value == null ? undefined : value[part];
    }
    if (!isCall) return value;
    if (typeof value !== "function") throw new Error(`${path} is not a method of the event`);
    return value.apply(parent, args);
  }

  // ref.current.call/get/set from Python: run it and send the answer back.
  async function callElement(el, method, args, reply) {
    let value = null;
    let error = null;
    try {
      if (!el) throw new Error("the element is no longer on the page");
      if (method === "#event") value = await readEvent(args);
      else if (method === "#get") value = el[args[0]];
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
          const attrs = op[3] || {};
          const events = op[4] || {};
          for (const k in attrs) setProp(el, k, attrs[k]);
          for (const k in events) listen(el, k, events[k]);
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
          addRoot(el);
          (document.querySelector(op[2]) || document.body).appendChild(el);
          break;
        }
        case "A": {
          const parent = nodes.get(op[1]);
          const box = inside(parent);
          if (box.isConnected) {
            // On the page: one insertion, so the page is laid out once.
            const frag = document.createDocumentFragment();
            for (const id of op[2]) frag.appendChild(nodes.get(id));
            box.appendChild(frag);
          } else for (const id of op[2]) box.appendChild(nodes.get(id)); // a new element: nothing to lay out yet
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
            nodes.delete(op[1]);
            if (node.__js) forget(node); // a module's own unmount runs at once
            else {
              graveyard.push(node);
              if (graveyard.length === 1) later(bury);
            }
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

  // The nodes the operations create, in document order. An element holding its own
  // text ("#text") has that text node in the HTML too: it is listed after the
  // element, without an id, so the two lists line up.
  function created(ops) {
    const out = [];
    for (const op of ops) {
      if (op[0] !== "c" && op[0] !== "t" && op[0] !== "j") continue;
      out.push(op);
      if (op[0] === "c" && op[3] && op[3]["#text"]) out.push(["t", null, op[3]["#text"]]);
    }
    return out;
  }

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
      if (op[1] === null) return; // an element's own text, set with the element
      const n = found[i];
      register(op[1], n);
      if (op[0] === "j") {
        // Still mounted from the last session: keep it and pass the new props.
        if (n.__js && n.__js.module === op[3]) updateJs(n, op[5]);
        else mountJs(n, op[3], op[4], op[5], op[6]);
        return;
      }
      if (op[0] !== "c") return;
      const attrs = op[3] || {};
      const events = op[4] || {};
      for (const a of Array.from(n.attributes)) if (!(a.name in attrs)) n.removeAttribute(a.name);
      for (const k in attrs) setProp(n, k, attrs[k]);
      // Listeners left from a previous session would send stale node ids.
      for (const k in n.__ev || {}) if (!(k in events)) listen(n, k, null);
      for (const k in events) listen(n, k, events[k]);
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
    const boxes = segments.slice(1).map((seg, k) => {
      const box = document.querySelector(`[data-pyreaction-portal="${k}"]`);
      return { seg, box, inside: box ? domOrder(box) : null };
    });
    // A hydrate message has no structure (the HTML has it), so a page whose markup
    // differs cannot be built from it: the caller asks the server for the whole tree.
    if (!matches(own, found) || boxes.some((b) => !b.box || !matches(b.seg, b.inside))) {
      console.warn("pyreaction: the page's markup differs from this render (clock, randomness?); rebuilding it");
      mount.dataset.hydration = "rebuilt";
      return false;
    }
    mount.dataset.hydration = "adopted";
    nodes = new Map([[0, mount]]);
    adopt(own, found);
    const kept = new Set();
    for (const { seg, box, inside } of boxes) {
      register(seg[0][1], box);
      addRoot(box);
      kept.add(box);
      adopt(seg, inside);
    }
    // Boxes nothing adopted: from the server's HTML or the last session.
    for (const box of document.querySelectorAll("[data-pyreaction-portal]")) {
      if (box.__id === undefined || (!kept.has(box) && nodes.get(box.__id) !== box)) box.remove();
    }
  }

  // CRC-32, as Python's zlib.crc32: the server's checksum of the page's shape.
  let crcTable = null;
  function crc32(text) {
    if (!crcTable) {
      crcTable = new Int32Array(256);
      for (let n = 0; n < 256; n++) {
        let c = n;
        for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
        crcTable[n] = c;
      }
    }
    const bytes = new TextEncoder().encode(text);
    let crc = -1;
    for (let i = 0; i < bytes.length; i++) crc = crcTable[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
    return (crc ^ -1) >>> 0;
  }

  // Hydration when the HTML came from the server's very tree (a kept render): the
  // message holds only ids in document order, a checksum of the tags, and what the
  // HTML cannot carry (listeners, live properties, js_component mounts). The DOM is
  // checked against the checksum first; false sends the caller back for the whole
  // tree, as hydrate() does. Attributes are left as the HTML set them.
  function adoptCompact(segments) {
    const parts = [{ seg: segments[0], box: mount, found: domOrder(mount) }];
    for (let k = 1; k < segments.length; k++) {
      const box = document.querySelector(`[data-pyreaction-portal="${k - 1}"]`);
      if (!box) return false;
      parts.push({ seg: segments[k], box, found: domOrder(box) });
    }
    for (const { seg, found } of parts) {
      let expected = seg.ids.length;
      for (const v of seg.ids) expected += v & 1;
      const shape = found.map((n) => (n.nodeType === 3 ? "#" : n.localName)).join(",");
      if (found.length !== expected || crc32(shape) !== seg.sig) {
        console.warn("pyreaction: the page's markup differs from this render (an HTML fix-up?); rebuilding it");
        mount.dataset.hydration = "rebuilt";
        return false;
      }
    }
    mount.dataset.hydration = "adopted";
    nodes = new Map([[0, mount]]);
    const kept = new Set();
    for (const { seg, box, found } of parts) {
      if (seg.box !== undefined) {
        register(seg.box, box);
        addRoot(box);
        kept.add(box);
      }
      // Each entry: the step from the last id, doubled; odd for an element holding
      // its own text, whose text node is next in the DOM and has no id.
      let id = 0;
      let i = 0;
      for (const v of seg.ids) {
        const holdsText = v & 1;
        id += (v - holdsText) / 2;
        register(id, found[i]);
        i += 1 + holdsText;
      }
      for (const [nid, props] of seg.pr || []) {
        const el = nodes.get(nid);
        for (const k in props) setProp(el, k, props[k]);
      }
      for (const [nid, events] of seg.ev || []) {
        const el = nodes.get(nid);
        for (const k in events) listen(el, k, events[k]);
      }
      for (const op of seg.js || []) {
        const el = nodes.get(op[1]);
        if (el.__js && el.__js.module === op[3]) updateJs(el, op[5]);
        else mountJs(el, op[3], op[4], op[5], op[6]);
      }
    }
    for (const box of document.querySelectorAll("[data-pyreaction-portal]")) {
      if (!kept.has(box)) box.remove();
    }
    return true;
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

  // The page's URL as the browser shows it, and as the app sees it (without base).
  const browserHere = () => location.pathname + location.search;
  const here = () => {
    const path = location.pathname;
    const inside = base && (path === base || path.startsWith(base + "/")) ? path.slice(base.length) || "/" : path;
    return inside + location.search;
  };
  // An app path from the server ("/users/7"), as the browser needs it.
  const toBrowser = (to) => (base && to.startsWith("/") && !to.startsWith("//") ? base + to : to);

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
    if (leavesPage(url.pathname)) return; // another @page: the browser loads it
    if (url.pathname + url.search === browserHere() && url.hash) return; // in-page anchor
    e.preventDefault();
    if (a.dataset.pyreactionLink === "replace") history.replaceState(null, "", url);
    else {
      history.pushState(null, "", url);
      scrollTo(0, 0);
    }
    navigated();
  });

  addEventListener("popstate", () => {
    if (leavesPage(location.pathname)) location.reload();
    else navigated();
  });

  function followServer(nav) {
    if (nav.reload) {
      // A full load, so the server sees new cookies (after login or logout).
      if (nav.replace) location.replace(toBrowser(nav.to));
      else location.assign(toBrowser(nav.to));
      return;
    }
    if (nav.to === here()) return;
    if (leavesPage(new URL(toBrowser(nav.to), location.href).pathname)) {
      // A @page shows one route; another URL is another page, loaded afresh.
      if (nav.replace) location.replace(toBrowser(nav.to));
      else location.assign(toBrowser(nav.to));
      return;
    }
    if (nav.replace) history.replaceState(null, "", toBrowser(nav.to));
    else {
      history.pushState(null, "", toBrowser(nav.to));
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
  // Failed attempts in a row before giving up; 0 retries for as long as the page is open.
  const RECONNECT_ATTEMPTS = Number(script.dataset.reconnectAttempts || 0);
  // A message every HEARTBEAT ms keeps proxies from closing an idle connection.
  const HEARTBEAT = 25000;
  let heartbeat = null;

  function connect() {
    // A streamed page opened its socket from the shell (to save the handshake): take
    // it over if it is for this page's render and still usable, with what it got.
    const early = handoff && window.__prWS && window.__prWS.k === handoff ? window.__prWS : null;
    window.__prWS = null;
    let url;
    if (early && early.ws.readyState <= 1) {
      socket = early.ws;
      url = new URL(socket.url);
    } else {
      url = new URL(wsPath, location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      url.searchParams.set("p", here());
      if (base) url.searchParams.set("b", base);
      // Offer to adopt what is on the page: the server's HTML, or the last session's tree.
      if (mount.firstChild) url.searchParams.set("h", "1");
      // Ask to resume the last session, which the server keeps for a while.
      if (sid) url.searchParams.set("s", sid);
      // The first connection takes over the render that made this page's HTML.
      if (handoff) url.searchParams.set("k", handoff);
      socket = new WebSocket(url);
    }
    handoff = "";
    socket.onopen = () => {
      retry = 0;
      document.documentElement.removeAttribute("data-pyreaction-offline");
      clearInterval(heartbeat);
      heartbeat = setInterval(() => send([0, 0, "#ping", {}]), HEARTBEAT);
    };
    if (socket.readyState === 1) socket.onopen();
    const receive = (data) => {
      if (data.vt && !data.init && document.startViewTransition) {
        // Types (add_transition_type) need the object form, where the browser has it.
        const types = Array.isArray(data.vt) ? data.vt : [];
        const withTypes = types.length && typeof ViewTransition !== "undefined" && "types" in ViewTransition.prototype;
        animating = document
          .startViewTransition(withTypes ? { update: () => handle(data), types } : () => handle(data))
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
    // Applies a message keeping the focused element and its text selection: moving a
    // node in the DOM takes the focus away (React's prepareForCommit/resetAfterCommit).
    const handle = (data) => {
      const focused = document.activeElement;
      let range = null;
      try {
        if (focused && typeof focused.selectionStart === "number") {
          range = [focused.selectionStart, focused.selectionEnd, focused.selectionDirection];
        }
      } catch (_) {} // selectionStart throws on some input types
      applying = true;
      try {
        handleMessage(data);
      } finally {
        applying = false;
        if (focused && focused !== document.body && focused.isConnected && document.activeElement !== focused) {
          focused.focus({ preventScroll: true });
          if (range) {
            try {
              focused.setSelectionRange(range[0], range[1], range[2]);
            } catch (_) {}
          }
        }
      }
    };
    const handleMessage = (data) => {
      if (data.init) {
        sid = data.sid || null;
        maxUpload = data.maxUpload || 0;
        // A resumed session keeps its node ids and event numbers, so events made
        // while offline still apply. A new one numbers everything afresh: nothing
        // queued for the old one, and no pending input edit, may carry over.
        // data.rebuild: the same session, sending the whole tree after a mismatch.
        if (!data.resumed && !data.rebuild) {
          recentEvents.clear(); // their numbers belong to the old session
          outbox.length = 0;
          seq = 0;
          lastSent = new WeakMap();
        }
      }
      ack = data.ack;
      if (data.init === "hydrate") {
        // data.adopt: the HTML is this very render, so the message says only what it
        // cannot; otherwise the full creation ops, checked node by node.
        const adopted = data.adopt ? adoptCompact(data.adopt) && (apply(data.ops), true) : hydrate(data.ops);
        if (adopted === false) {
          send([0, 0, "#mismatch", { path: here() }]); // the server answers with the whole tree
          return;
        }
      } else if (data.init === "replace") replace(data.ops);
      else apply(data.ops);
      restoreControlled();
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
      if (RECONNECT_ATTEMPTS && retry >= RECONNECT_ATTEMPTS) {
        connectionLost();
        return;
      }
      const delay = Math.min(250 * 2 ** retry++, RECONNECT_MAX);
      setTimeout(connect, delay / 2 + Math.random() * (delay / 2));
    };
    // What the early socket received before this script ran, in order.
    if (early) for (const msg of early.queue.splice(0)) socket.onmessage(msg);
    if (early && socket.readyState === 3) socket.onclose();
  }

  // Out of attempts: say so, and offer a reload (which starts a fresh session).
  function connectionLost() {
    document.documentElement.setAttribute("data-pyreaction-lost", "");
    if (document.getElementById("pyreaction-lost")) return;
    const box = document.createElement("div");
    box.id = "pyreaction-lost";
    box.setAttribute("role", "alert");
    const text = document.createElement("span");
    text.textContent = "Connection to the server was lost.";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Reload";
    button.onclick = () => location.reload();
    box.append(text, button);
    document.body.append(box);
  }

  // A streamed page may still be revealing a boundary (waiting for its stylesheet):
  // adopt the page once it has, so the DOM is the render the server keeps.
  if (window.__prL) window.__prL.then(connect, connect);
  else connect();
})();
