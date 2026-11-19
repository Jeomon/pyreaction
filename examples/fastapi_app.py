"""A FastAPI app with a JSON API and pyreaction pages, in one server.

    uv run --with fastapi --with uvicorn uvicorn examples.fastapi_app:api --reload

Then open http://127.0.0.1:8000/ (pages) and http://127.0.0.1:8000/docs (the API).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from reaction import Link, Router, component, html, mount, use_request, use_state

TODOS = ["Write the API", "Write the pages"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Whatever the lifespan yields is request.state in API routes and in components.
    yield {"greeting": "Hello from the lifespan"}


api = FastAPI(lifespan=lifespan)
api.state.todos = TODOS  # shared with the components through request.app.state


@api.get("/api/todos")
def list_todos() -> list[str]:
    return api.state.todos


@api.post("/api/todos")
def add_todo(text: str) -> list[str]:
    api.state.todos.append(text)
    return api.state.todos


@component
def Todos():
    request = use_request()  # the Request, then the WebSocket: same app, same state
    todos, set_todos = use_state(list(request.app.state.todos))
    draft, set_draft = use_state("")

    def add(e):
        if draft.strip():
            request.app.state.todos.append(draft.strip())  # the same list the API serves
            set_todos(list(request.app.state.todos))
            set_draft("")

    return html.main(
        html.h1("Todos"),
        html.p(request.state.greeting),
        html.form(
            html.input(value=draft, placeholder="New todo", on_change=lambda e: set_draft(e.value)),
            html.button("Add"),
            on_submit=add,
        ),
        html.ul([html.li(t, key=i) for i, t in enumerate(todos)]),
        html.p(Link("About", to="/about"), " · ", html.a("API docs", href="/docs")),
    )


@component
def About():
    return html.main(html.h1("About"), html.p("Pages by pyreaction, API by FastAPI."), Link("Back", to="/"))


@component
def App():
    return Router({"/": Todos, "/about": About})


# At "/", the pages answer every URL the API routes do not: /api/... and /docs stay
# FastAPI's. mount(api, App, "/ui") would put them under /ui instead.
mount(api, App, title="FastAPI + pyreaction")
