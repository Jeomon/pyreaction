# Routing and the page head

URLs, links and redirects, and the tags in `<head>`.

## Routing

```python
from reaction import Link, Navigate, Router

@component
def App():
    return html.main(
        html.nav(Link("Home", to="/"), Link("Users", to="/users")),
        Router({"/": Home, "/users": Users, "/users/{id}": User, "/files/{rest:path}": File,
                "/old": lambda: Navigate("/users")},
               fallback=NotFound),
    )
```

`Router` renders the first matching route and passes the `{params}` as keyword
arguments (`User(id="7")`). When nothing matches it renders `fallback` and the
first response has status 404. `Navigate(to)` redirects: an HTTP 307 on first
load, a URL change once live. `Link` changes the URL without a reload, Back and
Forward work, and every URL also renders on the server when loaded directly.

## The page head

```python
html.title(post.title)                                   # anywhere in the tree
html.meta(name="description", content=post.summary)
html.link(rel="canonical", href=url)
preload("/fonts/inter.woff2", as_="font", cross_origin="anonymous")
```

As in React 19, `<title>`, `<meta>` and `<link>` written anywhere go into
`<head>`, in the first HTML too, and leave it when they unmount. `Head(...)` and
`use_title` do the same. The resource hints `prefetch_dns`, `preconnect`,
`preload`, `preload_module`, `preinit` and `preinit_module` add each resource once.

Images in the page are preloaded from `<head>` as React does: the first ten, plus
any with `fetch_priority="high"` (lazy and low-priority images, `data:` URLs and
images inside `<picture>` are not). In a streamed page, the hints and `<head>`
tags a `Suspense` boundary adds arrive with its content, and a stylesheet it
preinits loads before that content shows. Each page response also lists its hints
in a `Link` header (up to about 2 kB).
