# Getting started

Install pyreaction, run a single file, or start a project.

## Installation

Requires Python 3.11 or newer. pyreaction is not on PyPI yet; install it from
the Git repository:

```sh
uv add "pyreaction @ git+https://github.com/Jeomon/pyreaction"
# or
pip install "pyreaction @ git+https://github.com/Jeomon/pyreaction"
```

Optional extras, added the same way (`"pyreaction[redis,jinja] @ git+..."`):

| Extra | For |
| --- | --- |
| `redis` | stores shared between worker processes (`RedisBackend`) |
| `wsgi` | putting pages in front of a WSGI app such as Flask (`wrap`) |
| `jinja` | the `{% component %}` template tag (`jinja_components`) |
| `browser` | Playwright, for tests in a real browser |

## Quick start

### A single file

Save the counter above as `app.py` and run `python app.py`. `run()` starts
uvicorn; pass `host=`, `port=` and any [configuration option](configuration.md).

### A project

```sh
uvx --from git+https://github.com/Jeomon/pyreaction reaction init my-app
cd my-app
uv run dev        # http://127.0.0.1:8000, restarts on every save
uv run pro        # production server
```

```
my-app/
  pyproject.toml     settings under [tool.pyreaction]
  app.py             the App component
  components/        your components
  public/            served at /static; style.css is linked from the page
```

`init` creates the project, runs `uv sync` and `git init` (`--no-install`,
`--no-git` skip them). `dev` and `pro` are commands the project declares in
`[project.scripts]`; they are the same as `uv run reaction dev` and
`uv run reaction run`, which work in any project.

`dev` restarts the server when a `.py` file changes, and open pages reconnect and
redraw by themselves (component state starts over). It also turns on debug mode:
errors appear in the page with their traceback, and lists without keys are
reported. `pro` serves without reloading or debug output.
