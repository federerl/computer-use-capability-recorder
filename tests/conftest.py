"""Shared fixtures.

The target app runs in-process on its own port so browser tests do not depend on
a server someone started by hand, and do not collide with one on the default
port.
"""

from __future__ import annotations

import threading

import pytest
from werkzeug.serving import make_server

from app import server as target
from app.seed import MEMBERS

PORT = 5099
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="session")
def live_app():
    app = target.create_app()
    srv = make_server("127.0.0.1", PORT, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield BASE
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def clean_target_state():
    """Faults and stopped checks are process-global; reset around every test."""
    target.FAULTS.clear()
    yield
    target.FAULTS.clear()
    for m in MEMBERS.values():
        for a in m.accounts:
            for c in a.checks:
                if c.status == "stopped":
                    c.status = "open"


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser, live_app):
    pg = browser.new_page()
    yield pg
    pg.close()


@pytest.fixture()
def signed_in(page, live_app):
    """A page past the sign-on screen, sitting on member search."""
    page.goto(f"{live_app}/login")
    page.fill("input[name=f1]", "roper")
    page.fill("input[name=f2]", "meridian-demo-pw")
    page.click("input[type=submit]")
    page.wait_for_load_state()
    return page
