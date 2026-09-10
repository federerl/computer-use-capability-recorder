"""Preflight check: everything M0 promises must be green before any milestone starts.

Hard checks exit non-zero. Soft checks (marked WARN) do not - discovery needs an API
key, but replay and the whole test suite are meant to run without one, and that
separation is itself part of the determinism claim.
"""

from __future__ import annotations

import importlib
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "
_failures = 0


def check(label: str, fn, *, hard: bool = True) -> None:
    global _failures
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        tag = BAD if hard else WARN
        if hard:
            _failures += 1
        print(f"[{tag}] {label}: {type(exc).__name__}: {exc}")
        return
    print(f"[{OK}] {label}" + (f": {detail}" if detail else ""))


def python_version() -> str:
    if sys.version_info < (3, 11):
        raise RuntimeError(f"need >=3.11, have {sys.version.split()[0]}")
    return sys.version.split()[0]


def imports() -> str:
    mods = ["anthropic", "playwright.sync_api", "pydantic", "flask", "yaml"]
    for m in mods:
        importlib.import_module(m)
    return f"{len(mods)} modules"


def chromium_headed() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.set_content("<h1>doctor</h1>")
        title = page.evaluate("document.querySelector('h1').textContent")
        # CDP accessibility snapshot is the observation channel the whole design rests on.
        cdp = page.context.new_cdp_session(page)
        tree = cdp.send("Accessibility.getFullAXTree")
        browser.close()
    return f"headed launch ok, AX tree {len(tree.get('nodes', []))} nodes ({title})"


def port_free() -> str:
    port = 5000
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"port {port} already in use - stop whatever is on it")
    return f"port {port} free"


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY unset (discovery will not run; replay and tests will)")
    return f"present ({key[:7]}...{key[-4:]})"


def dotenv() -> str:
    if not (ROOT / ".env").exists():
        raise RuntimeError("no .env - copy .env.example and fill it in")
    return ".env present"


def main() -> int:
    print(f"capability-recorder doctor  ({ROOT})\n")
    check("python version", python_version)
    check("dependencies importable", imports)
    check("playwright chromium (headed) + CDP AX tree", chromium_headed)
    check("target app port", port_free)
    check("local .env", dotenv, hard=False)
    check("ANTHROPIC_API_KEY", api_key, hard=False)

    print()
    if _failures:
        print(f"{_failures} hard check(s) failed.")
        return 1
    print("all hard checks green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
