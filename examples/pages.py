"""Routing, links, page titles, element refs and error boundaries.

    uv run python examples/pages.py
"""

from pyreaction import (
    ErrorBoundary,
    Link,
    Router,
    component,
    html,
    run,
    use_location,
    use_navigate,
    use_ref,
    use_state,
    use_title,
)

CSS = """
<style>
  body { font-family: system-ui, sans-serif; max-width: 36rem; margin: 3rem auto; }
  nav { display: flex; gap: 1rem; margin-bottom: 2rem; }
  nav a.active { font-weight: bold; }
</style>
"""

USERS = {"1": "Ada", "2": "Grace", "3": "Linus"}


@component
def NavLink(label, to):
    active = use_location().path == to
    return Link(label, to=to, class_name="active" if active else None)


@component
def Home():
    use_title("Home")
    return html.p("Pick a page above. Links change the URL without reloading the page.")


@component
def Users():
    use_title("Users")
    return html.ul([html.li(Link(name, to=f"/users/{uid}")) for uid, name in USERS.items()])


@component
def User(id):
    name = USERS.get(id)
    if name is None:
        raise LookupError(f"no user {id}")
    use_title(name)
    return html.h2(name)


@component
def Search():
    use_title("Search")
    query = use_location().query.get("q", "")
    draft, set_draft = use_state(query)
    box = use_ref()
    navigate = use_navigate()
    return html.div(
        html.form(
            html.input(ref=box, value=draft, on_change=lambda e: set_draft(e.value), placeholder="name"),
            html.button("Search", type="submit"),
            html.button("Focus box", type="button", on_click=lambda: box.current.focus()),
            on_submit=lambda: navigate(f"/search?q={draft}"),
        ),
        html.ul([html.li(n) for n in USERS.values() if query and query.lower() in n.lower()]),
    )


@component
def NotFound():
    use_title("Not found")
    return html.p("Nothing here.")


@component
def App():
    path = use_location().path
    return html.main(
        html.nav(NavLink("Home", "/"), NavLink("Users", "/users"), NavLink("Search", "/search"),
                 Link("Broken user", to="/users/99")),
        ErrorBoundary(
            Router({"/": Home, "/users": Users, "/users/{id}": User, "/search": Search}, fallback=NotFound),
            fallback=lambda error, reset: html.p(f"This page failed: {error}", role="alert"),
            reset_keys=[path],  # leaving a broken page clears the error
        ),
    )


if __name__ == "__main__":
    run(App, title="pyreaction pages", head=CSS, debug=True)
