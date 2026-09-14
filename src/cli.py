"""Command line entry points.

    python -m src.cli discover --brief goals/stop_payment.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from src.artifact import store
from src.artifact.compile import CompileRequest, compile_capability
from src.discovery.agent import DiscoveryConfig, DiscoveryRun
from src.discovery.brief import DiscoveryBrief
from src.evidence.log import RunLog, new_run_id
from src.replay.engine import InputError, ReplayEngine
from src.safety.policy import Policy, PolicyGate
from src.safety.redaction import DEFAULT_PATTERNS, Redactor
from src.surface.web_playwright import WebSurface


def guarded(page, policy: Policy) -> WebSurface:
    """A surface that cannot act outside the policy, and cannot write an
    unmasked screenshot."""
    return WebSurface(page, gate=PolicyGate(policy),
                      mask_rules=policy.redaction.screenshot_mask)


def redactor_for(policy: Policy) -> Redactor:
    patterns = tuple((p, "policy") for p in policy.redaction.patterns)
    return Redactor(patterns or DEFAULT_PATTERNS)

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

    policy = Policy.load(args.policy)
    run_id = new_run_id("disc")
    log = RunLog(args.evidence_root, run_id, redactor_for(policy))

    from anthropic import Anthropic
    from playwright.sync_api import sync_playwright

    client = Anthropic()
    config = DiscoveryConfig(model=args.model, max_steps=args.max_steps,
                             max_seconds=args.max_seconds)

    print(f"run {run_id}: {brief.capability_id}")
    print(f"  entry   {entry}")
    print(f"  model   {config.model}  (max {config.max_steps} steps, "
          f"{config.max_seconds}s)")
    print(f"  policy  {policy.name}")
    print(f"  evidence {log.dir}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless,
                                    slow_mo=args.slow_mo)
        page = browser.new_page()
        surface = guarded(page, policy)
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


def collect_inputs(args: argparse.Namespace) -> dict:
    """Gather invocation inputs from whichever form the caller used.

    `--inputs` takes JSON, which is the right shape for a caller passing a whole
    object through. It is also miserable to type at a Windows shell, which
    rewrites the quoting before the process ever sees it and produces a JSON
    error that says nothing about the real cause. `--input name=value` and
    `--inputs-file` exist so that is never the only way in.
    """
    inputs: dict = {}

    if getattr(args, "inputs_file", None):
        inputs.update(json.loads(Path(args.inputs_file).read_text(encoding="utf-8")))

    raw = (args.inputs or "").strip()
    if raw and raw != "{}":
        try:
            inputs.update(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"--inputs is not valid JSON ({exc}). Some shells strip the inner "
                f"quotes; --input name=value avoids the question entirely:\n"
                f"    --input member_id=100482 --input check_number=1043"
            ) from exc

    for pair in getattr(args, "input", None) or []:
        if "=" not in pair:
            raise ValueError(f"--input expects name=value, got {pair!r}")
        name, value = pair.split("=", 1)
        inputs[name.strip()] = value

    return inputs


def replay(args: argparse.Namespace) -> int:
    load_dotenv()

    capability = store.load(args.artifact)
    try:
        inputs = collect_inputs(args)
    except ValueError as exc:
        print(f"input rejected: {exc}", file=sys.stderr)
        return EXIT_USAGE
    base_url = os.environ.get("BASE_URL", args.base_url)

    if capability.approval_state != "approved" and not args.allow_draft:
        print(f"{capability.id} is a draft. Replaying one unattended means "
              f"trusting targeting nobody has reviewed.", file=sys.stderr)
        for note in capability.review:
            print(f"  - {note}", file=sys.stderr)
        print("Pass --allow-draft to run it anyway.", file=sys.stderr)
        return EXIT_USAGE

    # The profile is the single knob. `--unattended` selects one rather than
    # overriding whichever profile happens to be loaded.
    policy = Policy.load(
        args.policy or ("policy/unattended.yaml" if args.unattended
                        else "policy/attended.yaml"))
    run_id = new_run_id("replay")
    log = RunLog(args.evidence_root, run_id, redactor_for(policy))

    from playwright.sync_api import sync_playwright

    print(f"run {run_id}: {capability.id}@{capability.version} "
          f"({capability.approval_state})  policy {policy.name}")

    lease = supervisor = None
    if args.attended:
        from src.escalation.control import ControlLease
        from src.escalation.supervisor import HumanSupervisor

        lease = ControlLease(log.dir / "control.json", run_id=run_id)
        supervisor = HumanSupervisor(lease, log, timeout_s=args.handoff_timeout)
        print("  attended: a run that cannot continue alone will wait for "
              "`src.cli operator`")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless,
                                    slow_mo=args.slow_mo)
        page = browser.new_page()
        try:
            surface = guarded(page, policy)
            surface.lease = lease
            engine = ReplayEngine(
                surface, capability, log,
                runtime={"BASE_URL": base_url},
                auto_confirm=policy.confirmations == "permit",
                supervisor=supervisor,
            )
            result = engine.run(inputs)
        except InputError as exc:
            print(f"input rejected: {exc}", file=sys.stderr)
            return EXIT_USAGE
        finally:
            browser.close()

    print(f"\n{result.status}  ({result.elapsed_ms} ms, "
          f"{result.llm_calls} model calls)")

    if result.outputs:
        for name, value in result.outputs.items():
            print(f"  {name} = {value!r}")
    if result.outcome:
        print(f"  outcome  {result.outcome.outcome}  ({result.outcome.code})")
        print(f"           {result.outcome.message}")
    if result.recoveries:
        for r in result.recoveries:
            print(f"  recovered {r.code} at {r.step_id} "
                  f"(attempt {r.attempt}, {r.then})" if r.resolved
                  else f"  unrecovered {r.code} at {r.step_id}")
    if result.control.get("human_intervened"):
        c = result.control
        print(f"  handover {c['intervention_id']} -> {c['verdict']}"
              + (f" by {c['operator']}" if c.get("operator") else ""))
        print(f"           {c['human_action_count']} recorded operator action(s)")
        if c.get("note"):
            print(f"           note: {c['note']}")
    if result.error:
        e = result.error
        print(f"  {e.code} at step {e.step_id} ({e.action})")
        print(f"    expected  {e.expected}")
        print(f"    observed  {e.observed}")
        for attempt in e.locator_attempts:
            print(f"    tried     {attempt}")
        for key, value in e.evidence.items():
            print(f"    {key:<9} {value}")

    print(f"\nevidence -> {log.dir}")
    return result.exit_code


def operator(args: argparse.Namespace) -> int:
    """The operator surface.

    A command line rather than a console, deliberately and documented: the
    handoff mechanism is what matters, and the person works the live browser
    window the automation was already using. This just moves the lease.
    """
    from src.escalation.control import ControlLease, ControlState, InvalidTransition
    from src.escalation.intervention import Intervention

    root = Path(args.evidence_root)

    def runs():
        for control in sorted(root.glob("*/control.json")):
            yield control.parent, ControlLease(control).read()

    if args.operator_command == "list":
        found = False
        for directory, lease in runs():
            if args.all or lease.state in (ControlState.HANDOFF_REQUESTED.value,
                                           ControlState.HUMAN.value):
                found = True
                print(f"{directory.name:<34} {lease.state:<18} "
                      f"{lease.reason or '-':<28} step {lease.step_id or '-'}")
        if not found:
            print("nothing waiting on a person." if not args.all else "no runs.")
        return EXIT_OK

    directory = root / args.run_id
    if not directory.exists():
        print(f"no run {args.run_id!r} under {root}", file=sys.stderr)
        return EXIT_USAGE
    lease = ControlLease(directory / "control.json")

    operator_name = getattr(args, "operator", "operator")

    if args.operator_command == "show":
        intervention = Intervention.read(directory)
        print(intervention.describe() if intervention else "no intervention recorded.")
        current = lease.read()
        print(f"\n  control     {current.state} (held by {current.holder}, "
              f"seq {current.seq})")
        return EXIT_OK

    try:
        if args.operator_command == "take":
            state = lease.read()
            if state.control is ControlState.HANDOFF_REQUESTED:
                lease.transition(ControlState.HUMAN, holder="human",
                                 operator=operator_name)
            print(f"control is with {lease.read().holder}. The browser window is "
                  f"live; do what you need to, then resume.")
            return EXIT_OK

        if args.operator_command == "resume":
            mode = "completed" if args.completed else "approve"
            lease.transition(ControlState.RESUME_REQUESTED, holder="human",
                             operator=operator_name, resume_mode=mode,
                             note=args.note)
            print(f"handed back as {mode!r}."
                  + ("" if mode == "approve" else
                     " automation will not repeat the step."))
            return EXIT_OK

        if args.operator_command == "abort":
            lease.transition(ControlState.ABORTED, holder="human",
                             operator=operator_name, note=args.note)
            print("run aborted.")
            return EXIT_OK
    except InvalidTransition as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_USAGE

    return EXIT_USAGE


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
    d.add_argument("--policy", default="policy/discovery.yaml")
    d.add_argument("--slow-mo", type=int, default=0, metavar="MS",
                   help="pause between browser operations so a person can follow along. Demonstration only; it changes nothing about what runs.")
    d.add_argument("--headless", action="store_true",
                   help="run without a visible browser (default is visible)")
    d.set_defaults(func=discover)

    r = sub.add_parser("replay", help="replay a capability; makes no model calls")
    r.add_argument("--artifact", required=True)
    r.add_argument("--inputs", default="{}", help="JSON object of input values")
    r.add_argument("--input", action="append", metavar="NAME=VALUE",
                   help="one input value; repeatable. Avoids shell quoting.")
    r.add_argument("--inputs-file", default=None,
                   help="path to a JSON file of input values")
    r.add_argument("--base-url", default="http://127.0.0.1:5000")
    r.add_argument("--evidence-root", default="evidence/replay")
    r.add_argument("--policy", default=None,
                   help="defaults to policy/attended.yaml, or "
                        "policy/unattended.yaml with --unattended")
    r.add_argument("--unattended", action="store_true",
                   help="use the unattended profile, which permits steps the "
                        "capability marks as needing confirmation")
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--attended", action="store_true",
                   help="pause and hand the live session to an operator when the "
                        "run cannot safely continue on its own")
    r.add_argument("--handoff-timeout", type=float, default=900)
    r.add_argument("--slow-mo", type=int, default=0, metavar="MS",
                   help="pause between browser operations so a person can follow along. Demonstration only; it changes nothing about what runs.")
    r.set_defaults(func=replay)

    o = sub.add_parser("operator", help="take and hand back control of a live run")
    o.add_argument("--evidence-root", default="evidence/replay")
    osub = o.add_subparsers(dest="operator_command", required=True)

    def who(parser):
        parser.add_argument("--operator", default=os.environ.get("USERNAME", "operator"),
                            help="who is taking responsibility for this decision")
        return parser

    ol = osub.add_parser("list", help="runs waiting on a person")
    ol.add_argument("--all", action="store_true")

    osh = osub.add_parser("show", help="why a run stopped, and what it looked like")
    osh.add_argument("run_id")

    ot = who(osub.add_parser("take", help="take control of the live session"))
    ot.add_argument("run_id")

    ores = who(osub.add_parser("resume", help="hand control back"))
    ores.add_argument("run_id")
    ores.add_argument("--completed", action="store_true",
                      help="the step was done by hand; automation must not repeat it")
    ores.add_argument("--approve", action="store_true",
                      help="automation performs the step (the default)")
    ores.add_argument("--note", default="")

    oa = who(osub.add_parser("abort", help="end the run"))
    oa.add_argument("run_id")
    oa.add_argument("--note", default="")

    o.set_defaults(func=operator)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
