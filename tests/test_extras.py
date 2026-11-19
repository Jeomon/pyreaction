"""Key filters, JavaScript components and file uploads."""

import json

from starlette.testclient import TestClient

from pyreaction import (
    UploadedFile,
    component,
    create_app,
    event,
    html,
    js_component,
    render_to_string,
    use_ref,
    use_state,
)
from pyreaction.server.app import _parse_upload, ws_max_size

from .test_reconciler import Harness

Chart = js_component("/static/chart.js", "mount")


def test_keys_filter_is_part_of_the_event_spec():
    @component
    def App():
        return html.input(on_key_down=event(lambda e: None, keys=["Enter", "Escape"], prevent_default=True))

    h = Harness(App())
    spec = next(iter(h.root.hosts.values())).events["keydown"].spec()
    assert spec == [True, False, None, None, ["Enter", "Escape"]]


def test_js_component_mounts_updates_and_dispatches():
    picked = []
    setters = {}

    @component
    def App():
        data, set_data = use_state([1, 2, 3])
        setters["data"] = set_data
        return html.div(
            html.h2("Sales"),
            Chart(data=data, color="teal", on_select=lambda e: picked.append(e.index)),
        )

    h = Harness(App())
    js_node = next(n for n in h.root.hosts.values() if n.type is Chart)
    create = next(op for op in h.root.snapshot() if op[0] == "j")
    assert create == ["j", js_node.id, "div", "/static/chart.js", "mount", {"data": [1, 2, 3], "color": "teal"}]
    assert h.html() == "<div><h2>Sales</h2><div></div></div>"
    assert render_to_string(h.root) == "<div><h2>Sales</h2><div></div></div>"

    setters["data"]([4, 5])
    assert h.flush() == [["J", js_node.id, {"data": [4, 5], "color": "teal"}]]
    h.root.dispatch(1, js_node.id, "select", {"index": 1})
    assert picked == [1]

    setters["data"]([4, 5])
    assert h.flush() == [], "unchanged props send nothing"


def test_js_component_ref_and_removal():
    refs = {}
    setters = {}

    @component
    def App():
        show, set_show = use_state(True)
        setters["show"] = set_show
        refs["chart"] = use_ref()
        return html.div(Chart(ref=refs["chart"], data=[]) if show else None)

    h = Harness(App())
    refs["chart"].current.call("resize")
    node_id = refs["chart"].current.id
    assert h.flush() == [["m", node_id, "resize", []]]
    setters["show"](False)
    assert h.flush() == [["r", node_id]]
    assert refs["chart"].current is None


def _frame(upload_id, name, data, type_="text/plain"):
    header = json.dumps({"u": upload_id, "name": name, "type": type_, "size": len(data)}).encode()
    return len(header).to_bytes(4, "big") + header + data


def test_upload_frames_are_parsed_and_checked():
    upload_id, f = _parse_upload(_frame("7", "a.txt", b"hello"), max_size=10)
    assert (upload_id, f.name, f.type, f.size, f.text()) == ("7", "a.txt", "text/plain", 5, "hello")
    assert _parse_upload(_frame("8", "big.bin", b"x" * 11), max_size=10) is None
    assert _parse_upload(b"\x00\x00", max_size=10) is None
    assert _parse_upload(b"\x00\x00\x00\x05nope!", max_size=10) is None
    assert _parse_upload((10**6).to_bytes(4, "big") + b"{}", max_size=10) is None
    assert ws_max_size(10) == 16 * 1024 * 1024
    assert ws_max_size(50 * 1024 * 1024) > 50 * 1024 * 1024


def test_dispatch_turns_file_ids_into_uploaded_files():
    received = []

    @component
    def App():
        return html.input(type="file", on_change=lambda e: received.extend(e.files))

    h = Harness(App())
    h.root.uploads["1"] = UploadedFile("notes.txt", "text/plain", b"abc")
    node_id = next(iter(h.root.hosts))
    h.root.dispatch(
        1,
        node_id,
        "input",
        {"files": [
            {"id": "1", "name": "notes.txt", "type": "text/plain", "size": 3},
            {"name": "huge.iso", "type": "", "size": 99, "error": "larger than the limit"},
            {"id": "missing", "name": "lost.txt", "type": "", "size": 4},
        ]},
    )
    assert [(f.name, f.data, f.error) for f in received] == [
        ("notes.txt", b"abc", None),
        ("huge.iso", b"", "larger than the limit"),
        ("lost.txt", b"", "not received"),
    ]
    assert h.root.uploads == {}, "taken out of storage"


def test_upload_over_the_socket():
    received = []

    @component
    def App():
        return html.input(type="file", on_change=lambda e: received.extend(e.files))

    client = TestClient(create_app(App, max_upload_size=100))
    with client.websocket_connect("/_pyreaction/ws?p=/") as ws:
        first = json.loads(ws.receive_text())
        assert first["maxUpload"] == 100
        node_id = next(op[1] for op in first["ops"] if op[0] == "c")
        ws.send_bytes(_frame("1", "a.csv", b"x,y\n1,2", "text/csv"))
        ws.send_bytes(_frame("2", "big.bin", b"z" * 101))  # over the limit: refused
        ws.send_text(json.dumps([1, node_id, "input", {"files": [
            {"id": "1", "name": "a.csv", "type": "text/csv", "size": 7},
            {"id": "2", "name": "big.bin", "type": "", "size": 101},
        ]}]))
        ws.send_text(json.dumps([2, 999, "click", {}]))  # an event round trip to wait on
        ws.send_text(json.dumps([3, 0, "#navigate", {"to": "/x"}]))
    assert [(f.name, f.text() if not f.error else f.error) for f in received] == [
        ("a.csv", "x,y\n1,2"),
        ("big.bin", "not received"),
    ]
