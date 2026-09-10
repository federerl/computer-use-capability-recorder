# computer-use-capability-recorder

An LLM discovers how to accomplish a task in a legacy back-office UI. That run is compiled into a
typed, versioned **capability artifact**. The artifact then replays **deterministically, with no
model in the decision loop** — which is how an agent would invoke it in production.

Design rationale lives in `REPORT.md`.

> **Status: in progress.** The toolchain and the target application are in place. The discovery
> loop, artifact schema, replay engine, safety model, and human-handoff path are still being
> built. This README is a placeholder and will be replaced with setup and demo instructions.

## Running what exists today

```bash
uv sync --extra dev
uv run playwright install chromium
uv run python scripts/doctor.py     # preflight: browser, deps, port, API key

uv run python -m app.server         # the target app on http://127.0.0.1:5000
uv run pytest                       # no ANTHROPIC_API_KEY required
```

Copy `.env.example` to `.env` and fill it in. `.env` is never committed.
