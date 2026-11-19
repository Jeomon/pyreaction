# Contributing to pyreaction

Thanks for helping. Bug reports, fixes, docs and new features are all welcome.
For anything larger than a fix, open an issue first so we can agree on the
approach before you spend time on it.

## Setting up

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/Jeomon/pyreaction
cd pyreaction
uv sync                                  # the package and its dev dependencies
```

The browser tests use the Chrome installed on your machine; without it, run
`uv run playwright install chromium` once.

## Checking a change

```sh
uv run pytest                            # all tests, browser tests included
uv run mypy                              # the package is fully typed
```

Run the tests on the oldest supported Python too:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/pyr311 uv sync --python 3.11
UV_PROJECT_ENVIRONMENT=/tmp/pyr311 uv run --python 3.11 pytest -q -p no:cacheprovider
```

The browser tests skip when no browser is available, and the shared-store tests
run against an in-process fake Redis server, so no services are needed.

To see a change in a real app, run an example: `uv run python examples/todo.py`
(also `pages.py`, `widgets.py`, `login.py`, `lunch_roulette.py` and
`fastapi_app.py`). For changes to rendering or the update protocol, compare
`uv run python benchmarks/bench.py` before and after.

## What a change needs

- **Tests.** A fix comes with a test that fails without it. New features are tested
  through `reaction.testing.render` where possible, and in a real browser
  (`tests/test_browser.py`) when they depend on the browser.
- **Types.** `uv run mypy` stays clean; public functions have type hints.
- **Docs.** Anything a user can see is described in `docs/`, and new exports are
  listed in `docs/api.md`. Code examples in the docs should run.
- **Behaviour like React's.** Where pyreaction has a React API, it should behave as
  React does; say so in the pull request when it cannot.
- **Small, focused commits** with [Conventional Commits](https://www.conventionalcommits.org/)
  messages: `fix: ...`, `feat: ...`, `perf: ...`, `docs: ...`, `test: ...`.

## Where things are

```
src/reaction/
  core/       elements, hooks, components, the virtual DOM (vdom.py) and HTML rendering
  server/     the ASGI app and integrations: run, mount, page, wrap, embed, jinja_components, prerender
  client/     client.js, the page template (index.html) and its style.css
  cli/        the reaction command: init, dev and run, and the project template
  testing.py  render() with a fake browser, serve() and poll() for real browsers
tests/        the test suite; fakedom.py applies the update operations without a browser
examples/     runnable apps
benchmarks/   bench.py, timings for a 10,000-row page
docs/         the documentation
```

`src/reaction/core/vdom.py` documents the operations the server sends to the
browser, at the top of the file. `client.js` has no build step and no
dependencies: edit it directly.

## Reporting a bug

Include the pyreaction version (`reaction --version`), the Python version, the
smallest component that shows the problem, what you expected, and what happened
(with the traceback, or the browser console's errors).

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
