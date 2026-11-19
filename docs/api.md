# API reference

Everything `reaction` exports, grouped, with a line each and a link to where it is
explained. Docstrings in the source have the details (`help(reaction.use_state)`).

## Components and elements

| Name | |
| --- | --- |
| `component` | decorator: a function returning elements becomes a component ([Elements](elements-and-events.md#elements)) |
| `memo` | like `component`, skipping re-renders while the arguments are unchanged ([Hooks](hooks.md)) |
| `html` | tag factories: `html.div(...)`, typed for every standard tag; `html.my_widget` is `<my-widget>` |
| `h(tag, props, *children)` | the call form, for tags chosen at runtime |
| `fragment(*children)` | children without a wrapper element |
| `Element`, `Component` | the classes behind elements and components |
| `Children`, `clone_element`, `is_valid_element` | working on children, as in React |
| `lazy(load)`, `forward_ref(fn)` | a component loaded on first use; `forward_ref` for code ported from React |
| `parse_html(text)` | HTML as elements, with scripts and event attributes removed ([Elements](elements-and-events.md#elements)) |
| `image(format, data)` | an `<img>` of bytes held in memory |

## Events

| Name | |
| --- | --- |
| `event(fn, prevent_default=, debounce=, throttle=, keys=, ...)` | browser-side options for a handler ([Events](elements-and-events.md#events)) |
| `js(code)` | a handler that runs in the browser, with no round trip |
| `Event` | what handlers receive: `e.value`, `e.target`, `e.form`, `e.files`; `e.read(path)` and `e.call(path)` ask the browser for more |
| `EventHandler` | the class `event()` returns |
| `FormFields` | `e.form`: `fields["name"]` and `fields.getlist("name")` |
| `UploadedFile` | an entry of `e.files` ([File uploads](elements-and-events.md#file-uploads)) |

## Hooks

`use_state`, `use_reducer`, `use_effect`, `use_layout_effect`, `use_memo`,
`use_callback`, `use_ref`, `use_context`, `use`, `use_id`, `use_deferred_value`,
`use_transition`, `use_action_state`, `use_optimistic`, `use_effect_event`,
`use_imperative_handle`, `use_debug_value`, `use_async`, `use_data`,
`use_location`, `use_navigate`, `use_title`, `use_status`, `use_request`,
`use_store`, `use_page_store`, `use_sync_external_store`, `use_form_status`,
`use_linked_inputs`: see [Hooks](hooks.md).

| Name | |
| --- | --- |
| `create_context(default)`, `Context` | a context and its class; provide it with `ctx(*children, value=...)` |
| `create_ref(initial)`, `Ref` | a ref object outside a component, and its class |
| `AsyncState` | what `use_async` returns: `.loading`, `.value`, `.error`, `.reload()` |
| `FormStatus` | what `use_form_status` returns: `.pending`, `.data` |
| `Location` | what `use_location` returns: `.path`, `.search`, `.query`, `.base`, `.href(path)` |
| `start_transition(fn, *args)` | background work from a handler, without a pending flag |
| `flush_sync(fn)` | apply and send an update at once ([Forms](data-and-forms.md#forms-and-pending-state)) |
| `request_form_reset(ref)` | reset a form once the current action is done |
| `add_transition_type(name)` | name the view transition this update starts |
| `cache(fn)`, `cache_signal()` | memoise for one page, and know when it closed ([Data](data-and-forms.md#data)) |
| `browser()` | leave a component to the browser: rendered once the page is live |

## Built-in components

| Name | |
| --- | --- |
| `Suspense` | a fallback while children wait for data ([Data](data-and-forms.md#data)) |
| `ErrorBoundary` | a fallback when children fail to render ([Errors](components.md#errors)) |
| `Form` | a form whose action runs in the background ([Forms](data-and-forms.md#forms-and-pending-state)) |
| `Router`, `Link`, `Navigate` | routing ([Routing](routing.md#routing)) |
| `Head` | tags for `<head>` ([The page head](routing.md#the-page-head)) |
| `VirtualList` | a long list that renders only the rows in view ([Long lists](components.md#long-lists)) |
| `portal(*children, target=)` | children rendered elsewhere in the page ([Portals](components.md#portals-and-hidden-content)) |
| `Activity` | hide children without unmounting them |
| `StrictMode`, `Profiler`, `ViewTransition` | as in React ([Hooks](hooks.md)) |

## The browser

| Name | |
| --- | --- |
| `ElementHandle` | what a `ref` on an element holds: `focus()`, `call()`, `get()`, `set()`... ([Acting on elements](components.md#acting-on-elements)) |
| `Reply` | the browser's answer to an element call: `await` it or `.result()` |
| `js_component(module, export)`, `js_component_from_string(code)`, `JsComponent` | JavaScript widgets ([JavaScript components](components.md#javascript-components)) |
| `prefetch_dns`, `preconnect`, `preload`, `preload_module`, `preinit`, `preinit_module` | resource hints ([The page head](routing.md#the-page-head)) |

## Shared state

| Name | |
| --- | --- |
| `Store(value, name=, backend=)` | a value shared by every page, or every process with a backend ([Shared state](state.md)) |
| `RedisBackend(url)` | shares named stores through Redis or Valkey |
| `StoreBackend` | the protocol a backend implements: `load`, `write`, `listen` |

## Serving

| Name | |
| --- | --- |
| `run(App, host=, port=, **options)` | serve with uvicorn ([Getting started](getting-started.md)) |
| `create_app(App, **options)` | the ASGI app ([Configuration](configuration.md)) |
| `mount(api, App, path)`, `page(api, path)` | inside FastAPI ([Integrations](integrations.md#fastapi)) |
| `wrap(app, App, path)` | in front of Django, Flask or any ASGI/WSGI app |
| `embed(App, mount_path=)`, `jinja_components(env, components)` | inside server-rendered templates |
| `prerender(App, paths, folder)` | static pages ([Static prerendering](integrations.md#static-prerendering)) |
| `render_to_string`, `render_to_static_markup`, `to_vdom` | an element as HTML, as HTML that never goes live, or as plain dicts |
| `Root` | one live page: the mounted tree and its updates (used by the server and tests) |

## Testing (`reaction.testing`)

| Name | |
| --- | --- |
| `render(element)`, `Page`, `TestNode` | a component in a fake browser ([Testing](testing.md)) |
| `serve(App)`, `LiveServer` | a live server for tests in a real browser |
| `poll(check, timeout=)` | wait for a condition, retrying assertions |
