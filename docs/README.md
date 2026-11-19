# pyreaction documentation

pyreaction is React's model in Python: components run on the server, the browser
gets server-rendered HTML, then small DOM updates over a WebSocket. If you know
React, most of it will be familiar; [How it works](how-it-works.md) explains what
is different.

## Start here

- [Getting started](getting-started.md): install, a single-file app, a project
- [Elements and events](elements-and-events.md): describing the page, handlers, file uploads
- [Hooks](hooks.md): state, effects and the rest

## Guides

- [Data, forms and pending state](data-and-forms.md): loading data, Suspense, form actions, optimistic updates
- [Routing and the page head](routing.md): URLs, links, redirects, `<title>` and `<meta>`
- [Built-in components and the DOM](components.md): refs, portals, long lists, error boundaries, JavaScript widgets
- [Shared state](state.md): stores shared between pages, processes, or within one page
- [Integrations](integrations.md): FastAPI, Django, Flask, Jinja templates, static pages
- [Testing](testing.md): a fake browser for unit tests, a live server for Playwright

## Running it

- [Configuration](configuration.md): every option, in code, `pyproject.toml` or the environment
- [Deployment and security](deployment.md): workers, proxies, compression, origins, limits

## Reference

- [API reference](api.md): everything `reaction` exports
- [How it works](how-it-works.md): the request, the live connection, and the update protocol
