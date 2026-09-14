# computer-use-capability-recorder

An LLM works out how to do a task in a legacy back-office UI. That run is compiled into a
typed, versioned **capability artifact**. The artifact then replays **deterministically, with
no model in the decision loop** — which is how an agent invokes it in production.

> The model discovers. The artifact is the reusable capability. Deterministic replay is how
> it gets called.

Design rationale and trade-offs are in [`REPORT.md`](REPORT.md). Real runs are in
[`evidence/`](evidence/).

---

## Setup

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run playwright install chromium
cp .env.example .env          # Windows: copy .env.example .env
uv run python scripts/doctor.py
```

`doctor.py` checks the things every later step assumes: a headed Chromium that launches, the
accessibility tree, the port, and your configuration.

### Configuration

`.env` (never committed):

| | |
|---|---|
| `ANTHROPIC_API_KEY` | **discovery only.** Replay and the tests do not use it. |
| `BACKOFFICE_USER` / `BACKOFFICE_PASSWORD` | credentials for the local target app — `roper` / `meridian-demo-pw` |
| `BASE_URL` | defaults to `http://127.0.0.1:5000` |

The target app is a local stand-in. There are no real credentials and no real data anywhere in
this repository.

---

## Run it without an API key

Everything except discovery works with no key at all. This is not a convenience — it is the
determinism claim being checkable rather than asserted.

```bash
uv run pytest                 # 156 tests, no key
```

Start the target application in one terminal:

```bash
uv run python -m app.server   # http://127.0.0.1:5000, sign on as roper / meridian-demo-pw
```

### Replay a capability

```bash
uv run python -m src.cli replay \
  --artifact capabilities/meridian.stop_payment.place.json --unattended \
  --input member_id=100482 --input account_kind=Checking \
  --input check_number=1043 --input reason=lost_check
```

A browser drives itself through sign-on, member lookup, the account pane and the stop-payment
form, and reports the confirmation details. Note `0 model calls`.

Add `--slow-mo 600` to watch it, or `--headless` to hide it.

### Replay with different inputs

The same artifact, a different member and account — which is the point of recording a
capability rather than a script:

```bash
uv run python -m src.cli replay \
  --artifact capabilities/meridian.stop_payment.place.json --unattended \
  --input member_id=100517 --input account_kind=Checking \
  --input check_number=2210 --input reason=stolen_check
```

> `--input name=value` is repeatable and avoids shell quoting. `--inputs '{...}'` takes JSON,
> and `--inputs-file` reads it from a file. On Windows, prefer `--input`: PowerShell rewrites
> the quoting inside a JSON argument before the process sees it.

### Outcomes that are answers, not failures

```bash
# no such member
--input member_id=999999   ...    # business_outcome: member_not_found   (exit 3)
# the check already cleared
--input check_number=1009  ...    # business_outcome: check_already_cleared
# the operator may not service this member
--input member_id=100633 --input check_number=3001 ...
```

Exit codes are distinct so a caller branches without parsing text: `0` success, `1` failure,
`2` rejected input, `3` business outcome, `4` escalated.

### Runtime conditions

The target application can produce real runtime states on demand. Faults are injected into
**the application**, never into the automation, so nothing here fakes its own failure.

```bash
curl -X POST localhost:5000/_control/fault -H "Content-Type: application/json" \
     -d '{"kind":"session_expired"}'
```

```powershell
# PowerShell
Invoke-RestMethod -Uri http://localhost:5000/_control/fault -Method Post `
  -ContentType application/json -Body '{"kind":"session_expired"}'
```

Then replay again.

| fault | what replay does |
|---|---|
| `session_expired` | signs back in and restarts the flow — still succeeds |
| `system_notice` | dismisses the interstitial and carries on |
| `slow_load` | waits, within the step's declared timeout |
| `wrong_confirmation` | `CHECKPOINT_FAILED` — the click worked, the page settled, and it is not the confirmation screen |
| `relabel_submit` | the control is renamed; a lower-ranked strategy still finds it |
| `security_challenge` | a state nothing declares — never reported as success |

`curl -X POST localhost:5000/_control/reset` clears them.

### Human takeover of the live session

Two terminals. **One:**

```bash
uv run python -m src.cli replay \
  --artifact capabilities/meridian.stop_payment.place.json --attended \
  --input member_id=100482 --input account_kind=Checking \
  --input check_number=1043 --input reason=lost_check
```

It drives to the stop-payment form and stops. The step is marked irreversible, and the browser
window stays open holding the live session. **Two:**

```bash
uv run python -m src.cli operator list
uv run python -m src.cli operator show <run-id>
```

Now either approve it, or do it yourself in that browser window and say so:

```bash
uv run python -m src.cli operator resume <run-id> --approve   --operator you
uv run python -m src.cli operator resume <run-id> --completed --operator you
```

The distinction matters: an approved irreversible step performed twice is two stop payments.
Nothing can infer which happened, so it is part of how control is handed back. On resume the
run revalidates the session before acting — if you navigated elsewhere it stops with
`POST_HANDOFF_STATE_MISMATCH` rather than continuing.

---

## Discovery — the part that needs a key

```bash
uv run python -m src.cli discover --brief goals/stop_payment.json
```

The model is given a goal in natural language, an entry URL, and the input values. It is told
nothing about the application: no routes, no field names, no hint of the flow. It sees an
accessibility inventory each turn and answers with one action.

A run costs roughly $0.19 and takes about a dozen steps. It writes evidence to
`evidence/discovery/` and a **draft** capability to `evidence/artifacts/`.

A draft is not approved, and the CLI refuses to replay one unattended. `review` lists what the
run could not settle — targeting that will not generalise, waits that carry this record's
details, conditions it never reached. Approval is a human act; the difference between
[the draft](evidence/artifacts/meridian.stop_payment.place.json) and
[the approved capability](capabilities/meridian.stop_payment.place.json) is recorded in
`provenance.human_edits`.

---

## Inspecting things

```bash
# what the model sees, and how each control would be targeted
uv run python scripts/inventory.py --path "/members?f3=100482"

# the capability's agent-facing input contract
uv run python -c "from src.artifact.store import load; \
  print(load('capabilities/meridian.stop_payment.place.json').input_schema())"

# regenerate every evidence run (app must be running)
uv run python scripts/capture_evidence.py
```

---

## Layout

```
src/surface/      perceiving and acting on a surface — the only part that knows about a browser
src/artifact/     the capability schema, binding, and the compiler
src/discovery/    the observe → decide → act loop
src/replay/       deterministic execution, conditions, results
src/safety/       the policy gate and redaction
src/escalation/   the control lease, interventions, and the operator seam
app/              the target application — a fixture, not part of the system
policy/           discovery / attended / unattended profiles
capabilities/     approved, callable capabilities
evidence/         real runs; see evidence/README.md
schema/           the published JSON Schema, generated from the models that enforce it
```
