# Configuration

Every option, where to set it, and its environment variable.

Options are passed to `create_app` (and so to `run`, `mount`, `page`, `wrap` and
`serve`), set in a project's `[tool.pyreaction]` table, or read from environment
variables. Code wins over `[tool.pyreaction]`, which wins over the environment.

| Option | Default | Environment variable | |
| --- | --- | --- | --- |
| `title` | `"pyreaction"` | | the page title before `use_title` sets one |
| `head` | `""` | | extra raw HTML for `<head>` |
| `lang` | `"en"` | | the `<html lang>` attribute |
| `static_dir` | none | | a folder of files to serve (stylesheets, images, JavaScript modules) |
| `static_path` | `"/static"` | | the URL path `static_dir` is served at |
| `stylesheets` | `()` | | files in the static folder linked from the page |
| `routes` | `()` | | extra Starlette routes, matched before the pages |
| `prefix` | `"/_pyreaction"` | | the path of the client script and WebSocket; change it only if it clashes with your routes |
| `on_error` | none | | `fn(exc, where, connection)` for uncaught errors |
| `debug` | `False` | `PYREACTION_DEBUG` | errors and warnings shown in the page |
| `allowed_origins` | `()` | `PYREACTION_ALLOWED_ORIGINS` (comma-separated) | extra origins allowed to open the WebSocket |
| `max_upload_size` | 10 MiB | `PYREACTION_MAX_UPLOAD_SIZE` | bytes per uploaded file |
| `max_pending_events` | `1000` | `PYREACTION_MAX_PENDING_EVENTS` | events a page may queue before it is disconnected |
| `max_pending_ops` | `100000` | `PYREACTION_MAX_PENDING_OPS` | updates a slow browser may fall behind (queued behind the one being sent, which goes however large) before it starts over |
| `data_timeout` | `5.0` | `PYREACTION_DATA_TIMEOUT` | seconds the first HTML waits for data |
| `stream` | `True` | `PYREACTION_STREAM` | stream Suspense content into the first response |
| `threaded` | `True` | `PYREACTION_THREADED` | run plain handlers and effects in worker threads |
| `reconnect_max` | `8000` | `PYREACTION_RECONNECT_MAX` | longest delay between reconnect attempts, in ms |
| `reconnect_attempts` | `0` (no limit) | `PYREACTION_RECONNECT_ATTEMPTS` | failed attempts in a row before the page gives up |
| `resume_timeout` | `30.0` | `PYREACTION_RESUME_TIMEOUT` | seconds a dropped page's state is kept for a reconnect |
| `handoff_timeout` | `5.0` | `PYREACTION_HANDOFF_TIMEOUT` | seconds a first-HTML render waits for its page's live connection (0: render again) |
| `max_handoffs` | `64` | `PYREACTION_MAX_HANDOFFS` | first-HTML renders that may wait at once; the oldest makes way |
| `compress` | `True` | `PYREACTION_COMPRESS` | gzip pages and static files for browsers that accept it |

A project's settings:

```toml
[tool.pyreaction]
app = "app:App"              # module:attribute of the root component
title = "my-app"
static = "public"            # served at /static
stylesheets = ["style.css"]
resume_timeout = 60          # any option from the table above
```

`reaction run` and `reaction dev` listen on `--host` and `--port`, which default
to `PYREACTION_HOST` (else `127.0.0.1`) and `PYREACTION_PORT`, then `PORT`
(else `8000`). Boolean variables accept `1`/`true`/`yes`/`on` and
`0`/`false`/`no`/`off`; an invalid value stops the app at startup.
