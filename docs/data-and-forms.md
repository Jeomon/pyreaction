# Data, forms and pending state

Loading data without blocking the page, and showing that work is in progress.

## Data

```python
@component
def Team(team_id):
    members = use_async(lambda: db.members(team_id), [team_id])   # sync: runs in a thread
    if members.loading:
        return html.p("Loading...")
    if members.error:
        return html.p(f"Failed: {members.error}", html.button("Retry", on_click=members.reload))
    return html.ul([html.li(m.name, key=m.id) for m in members.value])
```

`use_async(fn, deps)` calls `fn` (a plain function in a worker thread, or an
`async def`) whenever `deps` change and returns `.loading`, `.value`, `.error`
and `.reload()`. `use_data(fn, deps)` returns the value itself: while it loads,
the nearest `Suspense(*children, fallback=...)` shows its fallback, and an error
goes to the nearest `ErrorBoundary`.

The server waits for data (up to `data_timeout`, 5 seconds by default) before
sending a page's first HTML, so the page arrives complete. Data inside a
`Suspense` boundary does not hold the page back: the HTML goes out with the
fallback, and the content follows in the same response when ready.

`@cache` memoises a function for one page, as React's `cache` does for one
request; `cache_signal()` says when the page has closed. `browser()` at the top
of a component leaves it to the browser: the first HTML shows the nearest
`Suspense` fallback, and the component renders once the page is live.

## Forms and pending state

Every event is a round trip, so slow actions should say they are working:

```python
@component
def SaveButton():
    return html.button("Saving..." if use_form_status().pending else "Save", type="submit")

@component
def Contact():
    def send(fields):                 # FormFields; plain functions run in a thread
        mail.send(fields["email"], fields["message"])
    return Form(html.input(name="email"), html.textarea(name="message"), SaveButton(), action=send)
```

`Form(*children, action=...)` runs `action(fields)` in the background, gives
components inside it `use_form_status()` (`.pending`, `.data`), and clears the
fields on success (`reset=False` keeps them). `html.form(..., action=fn)` is the
same, as React 19 writes it, and `html.button("Draft", form_action=fn)` submits
its form to another action.

`use_transition()` returns `pending, start`; `start(fn, *args)` runs `fn` in the
background with `pending` true meanwhile. `use_action_state(action, initial)`
returns `state, run, pending`. `use_optimistic` shows a change at once, until the
action behind it finishes:

```python
shown, add = use_optimistic(messages, lambda cur, text: cur + [text + " (sending)"])

def send(fields):
    add(fields["text"])                         # shown at once
    set_messages(api.post(fields["text"]))      # the real list replaces it when done
```
