"""Capture the replay evidence, one directory per scenario.

Run after the code is frozen so the logs correspond to what is shipped. Each
scenario is a real invocation; nothing here is staged or edited afterwards.

    uv run python -m app.server            # terminal 1
    uv run python scripts/capture_evidence.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = "capabilities/meridian.stop_payment.place.json"
ROOT_DIR = ROOT / "evidence" / "replay"
BASE = "http://127.0.0.1:5000"

RECORDED = ["member_id=100482", "account_kind=Checking",
            "check_number=1043", "reason=lost_check"]


def inputs(**overrides) -> list[str]:
    values = dict(pair.split("=", 1) for pair in RECORDED)
    values.update(overrides)
    return [arg for name, value in values.items()
            for arg in ("--input", f"{name}={value}")]


#: name, what it demonstrates, fault to arm first, extra CLI arguments
SCENARIOS = [
    ("01-success-recorded-inputs",
     "The inputs the capability was recorded with.",
     None, ["--unattended", *inputs()]),

    ("02-success-different-inputs",
     "A different member, account and reason through the same artifact.",
     None, ["--unattended", *inputs(member_id="100517", check_number="2210",
                                    reason="stolen_check")]),

    ("03-outcome-member-not-found",
     "A legitimate answer: no such member. Not an error.",
     None, ["--unattended", *inputs(member_id="999999")]),

    ("04-outcome-check-already-cleared",
     "A legitimate answer: the check has cleared and cannot be stopped.",
     None, ["--unattended", *inputs(check_number="1009")]),

    ("05-outcome-account-restricted",
     "A legitimate answer: the operator may not service this member.",
     None, ["--unattended", *inputs(member_id="100633", check_number="3001")]),

    ("06-recovered-session-expired",
     "The session times out mid-flow; the run signs back in and restarts.",
     "session_expired", ["--unattended", *inputs()]),

    ("07-recovered-system-notice",
     "An interstitial appears over the working pane and is dismissed.",
     "system_notice", ["--unattended", *inputs()]),

    ("08-failed-checkpoint",
     "The click works and the page settles, but it is not the confirmation "
     "screen. Without a checkpoint this would report success.",
     "wrong_confirmation", ["--unattended", *inputs()]),

    ("09-drift-tolerated-by-fallback",
     "The submit control is renamed. Role and name no longer match; the "
     "recorded selector finds the same control.",
     "relabel_submit", ["--unattended", *inputs()]),

    ("10-unrecognised-state",
     "A blocking dialog no condition declares. Never treated as success.",
     "security_challenge", ["--unattended", *inputs()]),

    ("11-refused-confirmation-required",
     "Attended, with nobody available: the irreversible step is not taken.",
     None, [*inputs()]),

    ("12-rejected-bad-input",
     "Arguments that do not satisfy the contract, refused before the browser "
     "opens.",
     None, ["--unattended", *inputs(member_id="12")]),
]


def settle_paths(staged: Path, destination: Path) -> None:
    """Point the recorded paths at where the evidence actually ended up.

    Each run writes into a staging directory named after the run id, then the
    whole thing is renamed to the scenario name. Paths recorded during the run
    still name the staging directory - which no longer exists - so a reader
    following a screenshot reference would find nothing.
    """
    was = str(staged.relative_to(ROOT)).replace("\\", "/")
    now = str(destination.relative_to(ROOT)).replace("\\", "/")

    for path in destination.rglob("*"):
        if path.suffix not in (".json", ".jsonl", ".txt") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if was in text:
            path.write_text(text.replace(was, now), encoding="utf-8")


def control(action: str, payload: dict | None = None) -> None:
    import urllib.request

    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(
        f"{BASE}/_control/{action}", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except Exception as exc:  # a refused reset is worth knowing about
        print(f"    ! control/{action} failed: {exc}")


def run(name: str, description: str, fault: str | None, args: list[str]) -> dict:
    control("reset")
    if fault:
        control("fault", {"kind": fault})

    destination = ROOT_DIR / name
    shutil.rmtree(destination, ignore_errors=True)
    staging = ROOT_DIR / f".staging-{name}"
    shutil.rmtree(staging, ignore_errors=True)

    completed = subprocess.run(
        [sys.executable, "-m", "src.cli", "replay", "--artifact", ARTIFACT,
         "--headless", "--evidence-root", str(staging.relative_to(ROOT)), *args],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = completed.stdout + completed.stderr

    produced = sorted(staging.glob("replay-*"))
    if produced:
        staged = produced[0]
        staged.rename(destination)
        settle_paths(staged, destination)
    else:
        destination.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(staging, ignore_errors=True)

    (destination / "console.txt").write_text(output, encoding="utf-8")
    (destination / "scenario.json").write_text(json.dumps({
        "name": name, "demonstrates": description, "fault_injected": fault,
        "command": " ".join(["src.cli", "replay", "--artifact", ARTIFACT, *args]),
        "exit_code": completed.returncode,
    }, indent=2) + "\n", encoding="utf-8")

    status = "?"
    result = destination / "result.json"
    if result.exists():
        status = json.loads(result.read_text(encoding="utf-8"))["status"]
    elif completed.returncode == 2:
        status = "rejected"

    print(f"  {name:<36} exit {completed.returncode}  {status}")
    return {"name": name, "status": status, "exit_code": completed.returncode,
            "demonstrates": description, "fault": fault}


def capture_handoff(name: str, resume_args: list[str], description: str) -> dict:
    """A handover, captured the way it actually happens: two processes.

    The run blocks holding the live session; a separate operator process moves
    the lease. Doing it in one process would demonstrate something easier than
    the thing being claimed.
    """
    import time

    control("reset")
    destination = ROOT_DIR / name
    shutil.rmtree(destination, ignore_errors=True)
    staging = ROOT_DIR / f".staging-{name}"
    shutil.rmtree(staging, ignore_errors=True)

    run_process = subprocess.Popen(
        [sys.executable, "-m", "src.cli", "replay", "--artifact", ARTIFACT,
         "--attended", "--headless", "--handoff-timeout", "120",
         "--evidence-root", str(staging.relative_to(ROOT)), *inputs()],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    run_dir = None
    for _ in range(200):
        found = list(staging.glob("replay-*/intervention.json"))
        if found:
            run_dir = found[0].parent
            break
        if run_process.poll() is not None:
            break
        time.sleep(0.25)

    operator_output = ""
    if run_dir is not None:
        for command in (["operator", "--evidence-root", str(staging.relative_to(ROOT)), "list"],
                        ["operator", "--evidence-root", str(staging.relative_to(ROOT)), "show", run_dir.name],
                        ["operator", "--evidence-root", str(staging.relative_to(ROOT)), "resume",
                         run_dir.name, *resume_args]):
            done = subprocess.run([sys.executable, "-m", "src.cli", *command],
                                  cwd=ROOT, capture_output=True, text=True)
            operator_output += (f"$ src.cli {' '.join(command)}\n"
                                f"{done.stdout}{done.stderr}\n")

    output = run_process.communicate(timeout=180)[0] or ""

    produced = sorted(staging.glob("replay-*"))
    if produced:
        staged = produced[0]
        staged.rename(destination)
        settle_paths(staged, destination)
    else:
        destination.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(staging, ignore_errors=True)

    (destination / "console.txt").write_text(output, encoding="utf-8")
    (destination / "operator-console.txt").write_text(operator_output, encoding="utf-8")
    (destination / "scenario.json").write_text(json.dumps({
        "name": name, "demonstrates": description, "fault_injected": None,
        "command": "src.cli replay --attended ...  +  src.cli operator resume "
                   + " ".join(resume_args),
        "exit_code": run_process.returncode,
    }, indent=2) + "\n", encoding="utf-8")

    status = "?"
    result = destination / "result.json"
    if result.exists():
        status = json.loads(result.read_text(encoding="utf-8"))["status"]

    print(f"  {name:<36} exit {run_process.returncode}  {status}")
    return {"name": name, "status": status, "exit_code": run_process.returncode,
            "demonstrates": description, "fault": None}


def main() -> int:
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"capturing {len(SCENARIOS)} scenarios into {ROOT_DIR}\n")

    summary = [run(*scenario) for scenario in SCENARIOS]

    summary.append(capture_handoff(
        "13-handover-operator-approves",
        ["--approve", "--operator", "dana", "--note", "fee verified against the schedule"],
        "The run stops at the irreversible step and waits. A separate operator "
        "process takes the live session, approves, and hands it back; the run "
        "revalidates and completes."))

    control("reset")

    (ROOT_DIR / "index.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nindex -> {ROOT_DIR / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
