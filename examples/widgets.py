"""Key filters, a JavaScript component and file uploads.

    uv run python examples/widgets.py
"""

from pathlib import Path

from pyreaction import component, event, html, js_component, run, use_state

Bars = js_component("/static/bars.js", "mount")


@component
def App():
    values, set_values = use_state([3, 7, 4])
    draft, set_draft = use_state("")
    picked, set_picked = use_state(None)
    files, set_files = use_state([])

    def add():
        if draft.strip().isdigit():
            set_values(values + [int(draft)])
        set_draft("")

    return html.main(
        html.h1("Widgets"),
        html.p("Type a number and press Enter (other keys stay in the browser):"),
        html.input(
            value=draft,
            on_change=lambda e: set_draft(e.value),
            on_key_down=event(add, keys=["Enter"], prevent_default=True),
        ),
        Bars(values=values, color="teal", on_pick=lambda e: set_picked(e)),
        html.p(f"Picked bar {picked['index']}: {picked['value']}" if picked else "Click a bar.", id="picked"),
        html.h2("Upload"),
        html.input(type="file", multiple=True, on_change=lambda e: set_files(e.files)),
        html.ul([
            html.li(f"{f.name}: {f.error}" if f.error else f"{f.name}: {f.size} bytes, starts {f.data[:20]!r}")
            for f in files
        ], id="files"),
    )


if __name__ == "__main__":
    run(App, title="pyreaction widgets", static_dir=Path(__file__).parent / "static", debug=True)
