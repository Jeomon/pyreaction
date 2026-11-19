"""Shared fixtures. ``page`` drives a real browser: Playwright with the installed
Google Chrome, or Playwright's own Chromium; tests using it skip when neither is
available."""

import pytest


# Module scope, not session: sync Playwright keeps an event loop running on the main
# thread while started, which would break asyncio.run() in every later test.
@pytest.fixture(scope="module")
def _browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    playwright = sync_api.sync_playwright().start()
    browser = None
    for options in ({"channel": "chrome"}, {}):
        try:
            browser = playwright.chromium.launch(headless=True, **options)
            break
        except Exception:
            continue
    if browser is None:
        playwright.stop()
        pytest.skip("no browser for Playwright (install Chrome, or run: playwright install chromium)")
    yield browser
    browser.close()
    playwright.stop()


@pytest.fixture
def page(_browser):
    context = _browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(exc))
    yield page
    context.close()
    assert not errors, f"JavaScript errors on the page: {errors}"
