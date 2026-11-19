# Testing

Unit tests with a fake browser, and tests in a real one.

```python
from reaction.testing import render

def test_todo():
    with render(Todo()) as page:
        page.type("input[name=item]", "milk")
        page.click("button.primary")
        assert [li.text for li in page.find_all("#list li")] == ["milk"]
```

`render` mounts a component on a real event loop with a small fake browser, so
handlers, effects, data loading and forms run as on a live page; each action
waits until the page has settled. `find` and `find_all` take CSS selectors and
`text=`; actions are `click`, `type`, `check`, `key`, `submit`, `navigate` and
`dispatch`. Errors raised by components fail the test (`raise_errors=False`
collects them in `page.errors`). `to_vdom(page.root)` gives the page as plain
dicts, for snapshot tests.

For a real browser, `serve` runs the app on a free local port, and `poll` waits
for a condition that the page reaches in its own time:

```python
from reaction.testing import poll, serve

def test_counter(page):                      # Playwright's page (pytest-playwright)
    with serve(Counter) as server:           # create_app options go here too
        page.goto(server.url)
        page.wait_for_selector(server.READY)
        page.click("button")
        poll(lambda: page.text_content("button") == "Clicked 1 times")
```

`poll(check, timeout=5)` returns the first truthy result of `check`, retrying
`AssertionError`s until the timeout. `serve(asgi_app, asgi=True)` serves an app
you built yourself.
