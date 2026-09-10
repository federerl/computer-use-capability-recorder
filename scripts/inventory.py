"""Print what the model sees, and how each control would be targeted.

The strategy ranking decides whether a replay is robust or merely lucky, so it
should be readable without running a discovery session.

    uv run python scripts/inventory.py --path "/members?f3=100482"
    uv run python scripts/inventory.py --path "/members/100482" \
        --frame-url "/stoppay/new?m=100482&a=****4821"
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.serving import make_server  # noqa: E402

from app import server as target  # noqa: E402
from src.surface.snapshot import render  # noqa: E402
from src.surface.web_playwright import WebSurface  # noqa: E402

PORT = 5097
BASE = f"http://127.0.0.1:{PORT}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default="/members?f3=100482")
    ap.add_argument("--frame-url", default=None,
                    help="path to load into the detail frame first")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    srv = make_server("127.0.0.1", PORT, target.create_app(), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()

        page.goto(f"{BASE}/login")
        page.fill("input[name=f1]", "roper")
        page.fill("input[name=f2]", "meridian-demo-pw")
        page.click("input[type=submit]")
        page.wait_for_load_state()

        surface = WebSurface(page)
        page.goto(f"{BASE}{args.path}")
        surface._settle()

        if args.frame_url:
            surface.frame("detail").goto(f"{BASE}{args.frame_url}")
            surface._settle()

        obs = surface.snapshot()

        print(f"url: {obs.url}")
        print(f"frames: {obs.frames}\n")
        print(render(obs))

        print("\n" + "=" * 72)
        print("TARGETING (ranked; first unique match wins)")
        print("=" * 72)

        for node in obs.actionable():
            try:
                t = surface.target_for(node, obs)
            except ValueError as exc:
                print(f"\n[{node.ref}] {node.role} {node.name!r}\n     NOT ADDRESSABLE: {exc}")
                continue

            label = f"{node.role} {node.name!r}" if node.name else node.role
            print(f"\n[{node.ref}] {label}  (frame:{node.frame})")
            for i, s in enumerate(t.ranked()):
                fields = s.model_dump(exclude_defaults=True)
                fields.pop("by", None)
                matched = "?"
                try:
                    from src.surface.locators import build_locator
                    matched = build_locator(surface.frame(t.scope.frame), s).count()
                except Exception:
                    matched = "err"
                mark = "->" if i == 0 else "  "
                flag = "" if matched == 1 else f"   <-- {matched} matches, not usable alone"
                print(f"  {mark} {s.by:<12} {fields}{flag}")

        browser.close()

    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
