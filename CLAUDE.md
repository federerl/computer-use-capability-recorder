# Project conventions

## Repository writing

Everything written into the repository — commit messages, PR titles and bodies, code comments,
README, REPORT — contains **technical content only**.

Nothing from the working conversation goes in. Specifically, do not write:

- references to planning discussions, decisions "we agreed", or anything framed as a reply
- internal milestone numbers, time or token budgets, effort levels, or scheduling
- references to the assignment brief, the reviewer, the interview, or the submission process
- process chatter, apologies, status narration, or meta-commentary about the work

Do write: what the code does, why it is shaped that way, what breaks if it changes, and how to
verify it. State a scope decision as a fact about the code ("state is in-memory; there is no
database") rather than as a reference to why it was chosen elsewhere.

The test: a reader who has never seen the conversation should find nothing that assumes they did.

## Invariants

These are load-bearing. Changing one is a design change, not a refactor.

- **Replay makes zero LLM calls.** Not for decisions, not for error summarization, not for
  convenience. The engine is constructed so a model client cannot reach it.
- **The LLM never emits a selector.** It acts on snapshot refs; code computes targeting from
  them. Discovery declares intent, code computes locators.
- **Targets are accessibility-vocabulary strategies**, ranked, with recorded CSS demoted and
  labelled brittle. Never coordinates. Fallback is bounded: first unique match wins, and a
  strategy matching more than one node under `expect_unique` fails rather than taking `nth=0`.
- **The policy gate sits at the surface boundary**, so no code path reaches the browser without
  passing through it. Discovery and replay share it unmodified.
- **`/_control/` is on the policy deny list.** The agent must never reach the fault controller.
- **Secrets and task literals never enter artifacts** — only `env:` and `from_input` references.
- **Screenshots are masked at capture time**, never redacted afterwards.

## Layout

- `src/` — the system. `app/` — the target application it drives; a fixture, not a deliverable.
- `evidence/` is committed; `runs/` is scratch and ignored.
- Tests run without `ANTHROPIC_API_KEY`. Only discovery needs one.

## Workflow

Branch and PR per milestone; squash merge. The PR body explains the decisions in the diff that
look arbitrary but are not.
