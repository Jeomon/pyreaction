# Hooks

State, effects and the rest of React's hooks, with Python names.

| Hook | |
| --- | --- |
| `use_state(initial)` | `value, set_value`; the setter takes a value or `lambda prev: ...` |
| `use_reducer(reducer, initial)` | `state, dispatch` |
| `use_effect(fn, deps)` | runs after the update reaches the browser; return a cleanup, or make it `async` (cancelled on cleanup). `deps=None` runs after every render, `[]` once, a list when a value in it changes |
| `use_layout_effect(fn, deps)` | the same as `use_effect`, for code written for React |
| `use_memo(fn, deps)` / `use_callback(fn, deps)` | cached until `deps` change |
| `use_ref(initial)` | `ref.current` survives re-renders; pass it as `ref=` to an element to act on it |
| `create_context(default)` / `use_context(ctx)` | provide with `ctx(*children, value=...)` |
| `use(ctx_or_task)` | a context (allowed inside `if`), or the result of an `asyncio` task |
| `use_async(fn, deps)` / `use_data(fn, deps)` | load data without blocking; see [Data](data-and-forms.md#data) |
| `use_transition()` / `use_action_state(action, initial)` / `use_optimistic(state, update)` | pending and optimistic state; see [Forms](data-and-forms.md#forms-and-pending-state) |
| `use_deferred_value(value)` | `value` one render behind, so a slow part updates after the fast part |
| `use_location()` / `use_navigate()` | the page URL (`.path`, `.search`, `.query`), and `navigate(url, replace=, reload=)` |
| `use_title(text)` / `use_status(code)` | the page title, and the first response's HTTP status (e.g. 404) |
| `use_request()` | the Starlette `Request`, then `WebSocket`, serving the page: `.headers`, `.cookies`, `.client`, `.scope` |
| `use_id()` | an id that matches between the server's HTML and the live page |
| `use_store(store)` / `use_sync_external_store(subscribe, get)` | state shared between pages; see [Shared state](state.md) |
| `use_effect_event(fn)` | a stable function that always calls the latest `fn` |
| `use_imperative_handle(ref, create, deps)` | expose an API to a parent holding `ref` |
| `use_linked_inputs(attrs_list, on_change, cast)` | inputs that show one value (a slider and a number box) |
| `use_debug_value(value)` | logs the value when it changes, in debug mode |

`@memo` instead of `@component` skips re-rendering while the arguments are
unchanged. A function of your own whose name starts with `use_` and calls hooks
is a custom hook.

The rules of hooks are enforced as in React: a hook inside an `if` raises
"Rendered fewer (or more) hooks than during the previous render", a hook outside
rendering raises "Invalid hook call", and a component that sets state on every
render stops after 50 renders with "Too many re-renders".

Also available, as in React: `StrictMode` (double rendering in debug mode),
`Profiler`, `ViewTransition` with `add_transition_type`, `lazy`, `forward_ref`,
`Children.*`, `clone_element`, `is_valid_element`, `flush_sync` and
`request_form_reset`. Updates in the same event-loop tick are batched into one
render, and setters are safe to call from other threads.
