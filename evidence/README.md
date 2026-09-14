# Evidence

Every run here is real. Each was produced by the committed code against the
target application, captured after the code was frozen, and none of it has been
edited afterwards. Reproduce the whole set with:

```bash
uv run python -m app.server                      # terminal 1
uv run python scripts/capture_evidence.py        # terminal 2
```

## The discovery run

[`discovery/disc-20260914T042525Z/`](discovery/disc-20260914T042525Z/) — one genuine LLM-driven run
against the live application.

| | |
|---|---|
| model | `claude-opus-5` |
| steps recorded | 12 |
| model calls | 10 |
| tokens | 24,927 in (23,967 cache reads), 1,441 out |
| approximate cost | $0.19 |
| events logged | 35 |

The model was given the goal, the entry URL, and the input values. Nothing else:
no routes, no field names, no hint of the flow. `log.jsonl` is the full trace,
`snapshots/` holds the accessibility inventory it was shown at each step, and
`screenshots/` holds what the page looked like, masked at capture.

**Replay makes no model calls at all.** Discovery is paid once per capability,
not once per invocation.

## The artifact, before and after review

| | |
|---|---|
| [`artifacts/meridian.stop_payment.place.json`](artifacts/meridian.stop_payment.place.json) | as discovered — `draft`, v1.0.0, 13 open review notes |
| [`../capabilities/meridian.stop_payment.place.json`](../capabilities/meridian.stop_payment.place.json) | after review — `approved`, v1.1.0, 6 conditions |

The draft is kept deliberately. The difference between the two is the honest
account of what a successful run can and cannot establish, and
`provenance.human_edits` on the approved version records each change and why.

A run that succeeds never reaches the states it would need to describe, and
watching a click succeed says nothing about whether it was reversible.

## Replay runs

| scenario | status | exit | what it demonstrates |
|---|---|---|---|
| [`01-success-recorded-inputs`](01-success-recorded-inputs/) | `success` | 0 | The inputs the capability was recorded with. |
| [`02-success-different-inputs`](02-success-different-inputs/) | `success` | 0 | A different member, account and reason through the same artifact. |
| [`03-outcome-member-not-found`](03-outcome-member-not-found/) | `business_outcome` | 3 | A legitimate answer: no such member. Not an error. |
| [`04-outcome-check-already-cleared`](04-outcome-check-already-cleared/) | `business_outcome` | 3 | A legitimate answer: the check has cleared and cannot be stopped. |
| [`05-outcome-account-restricted`](05-outcome-account-restricted/) | `business_outcome` | 3 | A legitimate answer: the operator may not service this member. |
| [`06-recovered-session-expired`](06-recovered-session-expired/) | `success` | 0 | The session times out mid-flow; the run signs back in and restarts. |
| [`07-recovered-system-notice`](07-recovered-system-notice/) | `success` | 0 | An interstitial appears over the working pane and is dismissed. |
| [`08-failed-checkpoint`](08-failed-checkpoint/) | `failed` | 1 | The click works and the page settles, but it is not the confirmation screen. Without a checkpoint this would report success. |
| [`09-drift-tolerated-by-fallback`](09-drift-tolerated-by-fallback/) | `success` | 0 | The submit control is renamed. Role and name no longer match; the recorded selector finds the same control. |
| [`10-unrecognised-state`](10-unrecognised-state/) | `failed` | 1 | A blocking dialog no condition declares. Never treated as success. |
| [`11-refused-confirmation-required`](11-refused-confirmation-required/) | `failed` | 1 | Attended, with nobody available: the irreversible step is not taken. |
| [`12-rejected-bad-input`](12-rejected-bad-input/) | `rejected` | 2 | Arguments that do not satisfy the contract, refused before the browser opens. |
| [`13-handover-operator-approves`](13-handover-operator-approves/) | `success` | 0 | The run stops at the irreversible step and waits. A separate operator process takes the live session, approves, and hands it back; the run revalidates and completes. |

Exit codes are distinct on purpose: `0` success, `1` failure, `2` rejected
input, `3` business outcome, `4` escalated. A caller branches without parsing
anything.

Each directory holds `console.txt` (what an operator saw), `result.json` (the
structured result), `log.jsonl` (the event trace) and, where the run failed or
paused, a masked screenshot and accessibility snapshot captured at that moment.

## Handling of regulated data

No credential, social security number, card number or API key appears anywhere
in this directory. Screenshots are masked by the browser as the image is
produced, so an unredacted copy is never written; the member profile screen
shows an SSN and a card number, and
[`discovery/disc-20260914T042525Z/screenshots/`](discovery/disc-20260914T042525Z/screenshots/) shows
what that looks like covered.

Inputs are recorded as a digest rather than as values, so runs can be correlated
without keeping what they were about.
