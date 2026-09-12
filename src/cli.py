"""Command line entry points.

    python -m src.cli discover --brief goals/stop_payment.json
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.artifact import store
from src.artifact.compile import CompileRequest, compile_capability
from src.discovery.agent import DiscoveryConfig, DiscoveryRun
from src.discovery.brief import DiscoveryBrief
from src.evidence.log import RunLog, new_run_id
from src.safety.redaction import Redactor
from src.surface.web_playwright import WebSurface

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env support. Existing environment always wins, so a shell export
    is never silently overridden by a stale file."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def discover(args: argparse.Namespace) -> int:
    load_dotenv()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Discovery needs it; replay and the "
              "test suite do not.", file=sys.stderr)
        return EXIT_USAGE

    brief = DiscoveryBrief.load(args.brief)
    base_url = os.environ.get("BASE_URL", args.base_url)
    entry = brief.app_profile.entry.replace("${BASE_URL}", base_url)

    run_id = new_run_id("disc")
    log = RunLog(args.evidence_root, run_id, Redactor())

    from anthropic import Anthropic
    from playwright.sync_api import sync_playwright

    client = Anthropic()
    config = DiscoveryConfig(model=args.model, max_steps=args.max_steps,
                             max_seconds=args.max_seconds)

    print(f"run {run_id}: {brief.capability_id}")
    print(f"  entry   {entry}")
    print(f"  model   {config.model}  (max {config.max_steps} steps, "
          f"{config.max_seconds}s)")
    print(f"  evidence {log.dir}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        surface = WebSurface(page)
        try:
            result = DiscoveryRun(
                surface, client, log, goal=brief.goal, entry=entry,
                inputs=dict(brief.values), secrets=brief.secret_refs,
                config=config,
            ).run()
        finally:
            browser.close()

    usage = result.usage.as_dict()
    print(f"\n{result.status}: {result.detail}")
    print(f"  steps recorded {len(result.steps)}")
    print(f"  model calls    {usage['model_calls']}")
    print(f"  tokens         in {usage['input_tokens']} "
          f"(cache read {usage['cache_read_tokens']}), "
          f"out {usage['output_tokens']}")

    log.write("usage.json", usage)

    if not result.ok:
        print("\nNo capability was produced. The run log holds what happened:")
        print(f"  {log.dir / 'log.jsonl'}")
        return EXIT_FAILED

    capability = compile_capability(CompileRequest(
        capability_id=brief.capability_id, result=result, inputs=brief.inputs,
        secrets=brief.secrets, app_profile=brief.app_profile, base_url=base_url,
        model=config.model, trace_ref=log.relative(log.dir / "log.jsonl"),
    ))

    out = Path(args.out or f"evidence/artifacts/{brief.capability_id}.json")
    store.save(capability, out)
    print(f"\ncapability -> {out}  ({capability.approval_state})")

    if capability.review:
        print("\nBefore approving, settle:")
        for note in capability.review:
            print(f"  - {note}")

    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="src.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="run an LLM-driven discovery run")
    d.add_argument("--brief", required=True, help="path to a discovery brief")
    d.add_argument("--base-url", default="http://127.0.0.1:5000")
    d.add_argument("--model", default="claude-opus-5")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--max-seconds", type=int, default=360)
    d.add_argument("--evidence-root", default="evidence/discovery")
    d.add_argument("--out", default=None)
    d.add_argument("--headless", action="store_true",
                   help="run without a visible browser (default is visible)")
    d.set_defaults(func=discover)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
