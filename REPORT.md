# REPORT

A goal in natural language becomes a genuine LLM-driven run against a live UI; that run is
compiled into a typed, versioned capability; the capability replays deterministically with no
model in the decision loop, returns typed outputs, distinguishes business outcomes from
recoverable conditions from hard failures, enforces policy, and can hand the live session to a
person and take it back.

Real runs for all of it are in [`evidence/`](evidence/).

## 1. Architecture

Five layers, one process, no services, no database, no queue.

```
discovery  ─┐                                     ┌─ replay
            ├─→  capability artifact (JSON)  ─────┤
  model     ┘         typed, versioned            └─ no model, ever
             \                                   /
              \────────  surface  ──────────────/
                    roles, names, containment
                         ↑ policy gate
                         ↑ control lease
```

**The surface is the seam.** It is the only module that knows a browser exists. Everything
above it works in accessibility roles, names and containment. A desktop implementation of the
same protocol would reuse the artifacts and the replay engine unchanged.

**Perception is the accessibility tree, not the DOM.** Three reasons, in the order they
matter. Coordinates do not survive serialisation — a recorded click at (412, 288) is worthless
next month, and the artifact is the product, so the observation channel has to produce
something the artifact can hold. It is also the honest seam to desktop, since Windows UI
Automation is the same abstraction. And it is one to two orders of magnitude smaller than the
equivalent markup: the genuine discovery run cost **$0.19**, against my own estimate of
$0.50–1.50.

**The model decides what to do; it never decides how to find it.** It acts on a ref from the
inventory it was shown, and the surface derives the targeting. That split is what makes a
recording reproducible — the artifact holds locators computed from what was on screen, not
from what a model believed about it.

**Two chokepoints, both unavoidable by construction.** Every action passes the policy gate and
the control lease before reaching the browser, so there is no second path to guard and no way
to act while a person holds the session.

Trade-offs taken deliberately: a single process because nothing here needs more; a file for
the control lease because the interesting design is the state machine, not the transport; and
Python because the target app, the automation and the tests then share one toolchain, and
Pydantic generates the published schema from the models that enforce it.

## 2. Artifact schema

[`schema/capability.schema.json`](schema/capability.schema.json) is generated from the models;
a test fails if the committed copy drifts.
[`capabilities/meridian.stop_payment.place.json`](capabilities/meridian.stop_payment.place.json)
is a real one.

It is a **contract, not a transcript**. The model's reasoning is referenced through
`provenance.trace_ref` and never embedded, so a capability can be read, diffed and approved on
its own terms.

**Runtime conditions are declared with their class.** Whether "no such member" is an answer or
a crash is a fact about the business, decided when the flow is recorded and reviewed by a
person — never inferred at runtime. The validators enforce what follows: a business outcome
must be terminal, must name an outcome for the caller to branch on, and **cannot** carry a
recovery; a recoverable without a recovery is refused as "a hard failure with a friendlier
name". Anything matching no declared condition is unknown, and unknown is never success.

**Detectors come from a closed set of six**, all evaluable against a snapshot with no model. A
closed set is auditable; an open one would smuggle judgement back into replay.

**Nothing regulated can enter an artifact.** Values bind by reference (`from_input`,
`from_secret`); secrets resolve only through `env:`; and a literal shaped like a social
security or card number is refused at validation rather than trusting each future author to
notice.

**Targets are parameterised too, not just values.** Choosing *which* record to act on is
targeting, not typing — the account row is `scope_anchor: "${account_kind}"`. Substitution is
deliberately dumb: exact placeholder into a name matched exactly, no expression language,
validated at load rather than partway through a flow that has already acted.

**Conditions carry `verified`, and an unverified one blocks approval.** This came from a real
failure. The first discovery run anticipated the not-found state and wrote its wording as
`"No members found"`; the screen says `"No records match"`. It had only run the happy path, so
it invented something plausible. A detector that never fires is **worse than none** — the
state falls through as unknown while the capability appears to have coverage.

Separating `app_profile.product` from `app_profile.tenant` is what makes cross-tenant reuse
expressible at all; without it, every institution owns a private copy of the same flow.

## 3. Determinism & error handling

**Determinism is enforced, not promised.** The engine holds an object that raises on any use:

```
ReplayPurityError: replay attempted to use a model ('messages'). Replay makes no model
calls; if a decision cannot be made from the artifact, it is an escalation, not an inference.
```

The first person to add "let the model summarise this error" gets a failing test rather than a
quietly non-deterministic system. `llm_calls` is printed on every run and asserted zero.

**Targeting is a ranked list, and ambiguity is a failure rather than a tie to break.** A
strategy matching two nodes has identified nothing, so it is discarded for the next one —
picking the first match is how automation does the right thing to the wrong account.
Positional resolution exists, must be asked for explicitly, and appears exactly once in the
artifact with a note explaining why; a test asserts it stays the only one.

Two refinements came from running it. Containers are anchored on a **stable label cell** rather
than their own accessible name, because a row's name is everything inside it concatenated —
`row "Reason -- select --"` becomes `row "Reason Lost check"` the moment the field is used, and
stops matching the row it was recorded from. And extraction targets are **positional**, because
the cell holding a confirmation number is named by that number, so the ordinary ranking would
record this run's answer as the way to find the next one.

**Three result channels with distinct exit codes**, so a caller branches without parsing:

| | | |
|---|---|---|
| `success` | 0 | typed outputs |
| `business_outcome` | 3 | an answer, with **no error object** — there is a test asserting that |
| `escalated` | 4 | a person must decide |
| `failed` | 1 | step, expected, observed, every locator tried with its match count, plus a masked screenshot and snapshot captured at that moment |

**Checkpoints are what stop a click being mistaken for an outcome.**
[`08-failed-checkpoint`](evidence/replay/08-failed-checkpoint/) is the case: the click worked,
the page settled, and it is not the confirmation screen. Without the checkpoint that run
reports success.

**Recovery is bounded and does not repeat work.** A condition found *before* a step retries it;
one found *after* clears the obstruction and continues — because clearing an interstitial and
then repeating the click that opened it is how one instruction becomes two. Restarting the flow
is **refused once an irreversible step has run**, escalating instead of placing a second stop
payment.

Drift is secondary here but real: [`09-drift-tolerated-by-fallback`](evidence/replay/09-drift-tolerated-by-fallback/)
renames the submit control, role+name stops matching, and a lower-ranked strategy finds the same
control.

## 4. Heterogeneity & multi-tenant

**The surface seam is real in code, not asserted.** `Surface` requires `snapshot`, `resolve`,
`act`, `url`, `text`. A desktop implementation over UI Automation provides those; the artifact
schema and the replay engine are untouched. The accessibility tree is precisely the abstraction
that carries across — it is what a screen reader consumes on both.

What would not carry unchanged: the CSS fallback strategy is web-only and would be dropped or
replaced by an automation-id equivalent; `FrameScope` becomes window or pane scope; and the
declared waits need surface-specific implementations. All of that lives below the seam.

A legacy web app is the easier case, and the target application here is one: framesets, table
layout, no test IDs, controls with no accessible name at all. Those are handled today — the
stop-payment fields are reachable only through their label cell.

**Multi-tenant reuse rests on separating the product from the instance.**
`app_profile.product` plus `product_version` identifies the vendor application; `tenant`
identifies one institution's deployment. A capability is recorded against the product, and a
per-tenant override layer keyed on `product@version` + tenant supplies what differs: relabelled
controls, an extra confirmation step, a route prefix. The placeholder mechanism already in the
targeting is the same machinery an override would use.

Drift detection falls out of the failure contract rather than needing a new system: every
resolution records each strategy tried with its match count, so a tenant where the preferred
strategy has started missing and a fallback is carrying the flow is visible in the logs before
it breaks. Turning that into an actual signal — aggregate strategy-degradation across tenants,
gating unattended replay on it — is designed, not built.

I did not build tenant plumbing, a registry, or a drift service. The brief is explicit that
prematurely building scaling infrastructure is not rewarded; the abstractions are shaped not to
need rebuilding.

## 5. Escalation & handoff

Real, and demonstrated across two processes in
[`13-handover-operator-approves`](evidence/replay/13-handover-operator-approves/).

**Detecting stuck** has three sources: a step the capability marks as needing confirmation; a
policy gate that recognises an irreversible control regardless of what the capability says; and
a state no declared condition matches, which escalates rather than being guessed through.

**Control is a lease** — a state, a holder, and a monotonic sequence number in a file. Every
action asserts the lease still holds, so automation and a person cannot both be acting. Illegal
transitions are refused rather than recorded. The sequence number is a fencing token: in one
process it backs an assertion rather than a lock, and it is in the model now so that making it a
real lock later is a deployment change, not a redesign.

**The intervention carries enough to act on** — capability, step, why, the live URL, inputs as a
digest, a masked screenshot and accessibility snapshot captured at that moment, and the tail of
the log.

**Handing back distinguishes approving from having already done it.** An approved irreversible
step performed twice is two stop payments, and nothing can infer which happened — only the
person knows — so it is part of `resume --approve` versus `resume --completed`.

**State is revalidated before automation moves again.** A person with a live browser can go
anywhere; continuing because the lease says it is our turn would act on an assumption that was
true several minutes and several clicks ago. A step about to run must still have its control
resolvable; a step reported as done must show the state that proves it. Either failing ends the
run with `POST_HANDOFF_STATE_MISMATCH`.

**What the person did is recorded**, redacted like everything else. A handover with no record of
it is a hole in the audit trail exactly where a regulated environment needs one.

Mocked deliberately: the operator surface is a command line. The person works the live browser
window the automation was already using; the commands only move the lease. A real deployment
needs a co-browsing console and a routed queue — neither changes the control-transfer model.

## 6. Safety

One gate at the surface boundary, shared unmodified by discovery and replay, answering three
separate questions.

**Where** — an allowlist of origins and paths, checked on the way in for a navigation and again
on **where an action landed**, because a click cannot be vetted in advance and a link is
perfectly capable of leaving the application. **What** — which action types are permitted.
**How dangerous** — whether an action is reversible.

**Risk classification lives in policy, not in the recording.** The first discovery run recorded
the stop-payment submit as `risk: safe`. It watched a click succeed; it had no way to see that
the click placed a fee-bearing hold. The policy that permitted the action does know, so the
classification is made there and carried into the recording.

**The gate and the artifact stay independent.** A capability that forgets to mark a step is
stopped anyway — that is the case a guardrail exists for, and there is a test that strips the
marking and confirms replay still refuses.

**Three profiles** — discovery flags consequential actions (a flow that halts before its final
step is not worth recording), attended asks a person, unattended permits but still records.
None relaxes the allowlist: being trusted to act alone is not being trusted to act anywhere.
All three deny the fault controller, so the lever that makes injected states appear is not
reachable by the thing those states exist to test.

**Redaction happens at the point of writing.** Screenshots are masked by the browser as the
image is produced, so an unredacted copy never exists — scrubbing a file afterwards is deletion
after disclosure, not redaction. Secrets are matched by exact value, patterns by shape, and the
patterns stay narrow: a rule broad enough to catch six-digit member numbers would gut the logs
it is meant to protect. Inputs are recorded as a digest so runs correlate without keeping what
they were about.

**The limits, stated plainly.** Risk classification is regex matching on a control's visible
label: English-only, defeatable by relabelling, and crude. It is defence in depth, not proof —
the authoritative control is the per-step `risk` field plus human review before approval. The
allowlist is path-based and would not catch an application that tunnels navigation through
query parameters. And a masking rule is only as good as the roles it names; a sensitive value
rendered as plain text rather than in a field would not be covered by the rules shipped here.

One defect worth recording because of how it failed. Masking rules are handed to the browser and
evaluated as **JavaScript** regexes, where Python's inline `(?i)` is a parse error rather than a
flag. Playwright swallowed it, the rule matched nothing, and redaction silently stopped — while
looking exactly like it was working. A guardrail that fails open is worse than none, because it
buys false confidence. The test now captures with and without the mask and asserts the images
differ.

## 7. Cuts

**Deliberately not built**

- *A desktop surface.* The seam is real and the protocol is small; the work is a UI Automation
  implementation of it, not a redesign.
- *A co-browsing operator console.* Explicitly permitted as a mock. The control-transfer model
  is the part that had to be real, and is.
- *Tenant plumbing, a capability registry, a drift service.* Designed in §4, not built. The
  brief says prematurely building scaling infrastructure is not rewarded.
- *The stretch goals* — a callable capability catalog, a second tenant variant, code generation,
  confidence scoring, bounded LLM repair. Each was reachable; none was worth taking budget from
  the load-bearing pieces given the brief asks for depth over breadth.

**Honest limitations**

- The target application is mine, so its UI is stable by construction. Real drift is harder than
  anything demonstrated here.
- One capability, one surface, one tenant. The abstractions are designed not to paint me into a
  corner; they are not proven across tenants.
- A successful discovery run cannot validate conditions it never reached. The system is honest
  about this — unverified conditions block approval — but it means the interesting half of the
  error taxonomy is authored by a person, not discovered.
- Quiescence between steps polls for a stable frame-URL set. That is a floor, not a wait
  strategy; the per-step declared waits are the real mechanism, and a navigation that has not
  begun within the poll interval can still be observed early.
- The control lease is a file. Legible, and adequate for one automation process and one
  operator, but a file is not an atomic channel — both sides retry around Windows' refusal to
  replace an open file. A real multi-process deployment wants the lease somewhere with a
  compare-and-swap.

**What I would build next, in order**

1. **Condition verification as a first-class run.** Replay a capability against inputs known to
   trigger each declared condition and record that the detector fired. That turns `verified`
   from an assertion by a reviewer into evidence, and it is the natural home for a confidence
   score gating unattended replay.
2. **The capability catalog.** Artifacts are already typed and versioned with generated input
   and output schemas; exposing them as callable tools is a thin layer, and it is what makes
   this a product rather than a demonstration.
3. **A second tenant, for real.** The override mechanism is designed and the placeholder
   machinery exists. Recording against one instance and replaying against a re-skinned second
   is the experiment that would tell me whether the product/tenant split holds.
4. **A desktop surface**, to find out what the seam got wrong. It is the claim in this report I
   have the least evidence for.
