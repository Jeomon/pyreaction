"""Streaming: the shell goes out at once, Suspense content follows as its data arrives."""

import asyncio
import re
import time

from starlette.testclient import TestClient

from reaction import Suspense, component, create_app, html, use_async, use_data


@component
def Slow(delay, text):
    async def load():
        await asyncio.sleep(delay)
        return text

    return html.p(use_data(load), class_name="loaded")


@component
def Page():
    return html.main(
        html.h1("Dashboard"),
        Suspense(Slow(0.3, "sales: 42"), fallback=html.p("Loading sales...")),
        Suspense(Slow(0.6, "users: 7"), fallback=html.p("Loading users...")),
    )


def read_chunks(client, path="/"):
    chunks = []
    start = time.monotonic()
    with client.stream("GET", path) as response:
        assert response.status_code == 200
        for text in response.iter_text():
            chunks.append((time.monotonic() - start, text))
    return chunks


def test_shell_arrives_before_the_data_and_chunks_follow():
    chunks = read_chunks(TestClient(create_app(Page)))
    first_at, first = chunks[0]
    assert first_at < 0.25, f"shell took {first_at:.2f}s"
    assert "<h1>Dashboard</h1>" in first
    assert "Loading sales..." in first and "Loading users..." in first
    assert "<!--pr-s:1-->" in first and "<!--/pr-s:1-->" in first
    assert "client.js" not in first, "the client script comes last, once the page is complete"

    whole = "".join(text for _, text in chunks)
    assert '<template id="pr-c1"><div style="display:contents"><p class="loaded">sales: 42</p></div></template>' in whole
    assert "users: 7" in whole
    assert whole.index("pr-c1") < whole.index("pr-c2"), "the quicker boundary is sent first"
    assert whole.rstrip().endswith("</html>") and "client.js" in whole.split("pr-c2")[-1]
    sales_at = next(t for t, text in chunks if "sales: 42" in text)
    assert 0.25 < sales_at, "the chunk waited for its data"


def test_swapped_page_equals_the_full_render():
    """Applying the chunks the way the inline script does gives the page that a
    non-streamed response renders, which is what the live page hydrates against."""
    client = TestClient(create_app(Page))
    whole = "".join(text for _, text in read_chunks(client))
    for n in (1, 2):
        chunk = re.search(rf'<template id="pr-c{n}">(.*?)</template>', whole, re.S).group(1)
        whole = re.sub(rf"(<!--pr-s:{n}-->).*?(<!--/pr-s:{n}-->)", lambda m: m.group(1) + chunk + m.group(2),
                       whole, count=1, flags=re.S)
    streamed_body = re.search(r'<div id="pyreaction-root">(.*?)</div>\n', whole, re.S).group(1)
    streamed_body = re.sub(r"<!--/?pr-s:\d+-->", "", streamed_body)

    unstreamed = TestClient(create_app(Page, stream=False)).get("/").text
    full_body = re.search(r'<div id="pyreaction-root">(.*?)</div>\n', unstreamed, re.S).group(1)
    assert streamed_body == full_body


def test_without_suspense_or_with_stream_off_the_page_is_one_piece():
    @component
    def Plain():
        async def load():
            await asyncio.sleep(0.05)
            return "ready"

        data = use_async(load)
        return html.p("..." if data.loading else data.value)

    chunks = read_chunks(TestClient(create_app(Plain)))
    assert len(chunks) == 1 and "<p>ready</p>" in chunks[0][1], "use_async outside Suspense is waited for"
    chunks = read_chunks(TestClient(create_app(Page, stream=False)))
    whole = "".join(text for _, text in chunks)
    assert "sales: 42" in whole and "<template" not in whole and "Loading sales" not in whole


def test_streaming_stops_at_data_timeout():
    @component
    def Never():
        return Suspense(Slow(10, "late"), fallback=html.p("still loading"))

    start = time.monotonic()
    chunks = read_chunks(TestClient(create_app(Never, data_timeout=0.2)))
    whole = "".join(text for _, text in chunks)
    assert time.monotonic() - start < 2
    assert "still loading" in whole and "late" not in whole and "client.js" in whole
